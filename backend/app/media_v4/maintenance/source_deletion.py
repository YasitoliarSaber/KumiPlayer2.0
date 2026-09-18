"""按来源卡片 / 按导入删除媒体库条目。

历史教训：旧版"按来源删除"会长时间占用识别、甚至卡住。因此本模块的红线是：

- **绝不参与识别**：不调用 resolver / parser / scanner，只读已确认的事实表；
- **先预览后执行**：预览只做计数与少量样本（有界查询，不水合对象）；
- **按来源引用计数**：只删除"不再被任何其他来源 revision 绑定"的作品，
  多来源共享的作品必须保留（否则删一个来源会打穿另一个来源的媒体库）；
- **路径防护**：只删除镜像根之内的文件，真实来源媒体永不触碰；
- **批量 + 可取消**：每批独立事务，进度可回报、可中断，投影只在最后刷新一次。

外键约束决定了执行顺序（已用 PRAGMA 实测）：

- ``revision_bindings.work_id`` 是 **RESTRICT**：必须先删绑定行，才能删作品行；
- ``artifacts`` 与 ``works`` 之间**没有外键**：产物行与文件必须显式清理，不会级联；
- ``seasons`` / ``episodes`` / ``provider_bindings`` 从 ``works`` 级联，无需手工删。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


class SourceDeletionCancelled(Exception):
    """删除被取消（批次之间检查）。

    刻意**不用** ``KeyboardInterrupt``：它是 ``BaseException``，会穿透调用方
    ``except Exception`` 的保护，把后台 worker 线程直接打死，并让作业永远停在
    running。用普通异常在删除函数内收口成"已取消"结果，由 runner 标记 cancelled。
    """


@dataclass(slots=True)
class SourceDeletionPlan:
    """删除影响范围预览（只含计数与少量样本，绝不水合全量对象）。"""

    root_id: str
    works_total: int = 0
    works_removable: int = 0
    works_shared: int = 0
    artifacts_total: int = 0
    artifact_files: int = 0
    artifact_bytes: int = 0
    files_outside_mirror: list[str] = field(default_factory=list)
    removable_samples: list[str] = field(default_factory=list)
    shared_samples: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "root_id": self.root_id,
            "works_total": self.works_total,
            "works_removable": self.works_removable,
            "works_shared": self.works_shared,
            "artifacts_total": self.artifacts_total,
            "artifact_files": self.artifact_files,
            "artifact_bytes": self.artifact_bytes,
            "files_outside_mirror_count": len(self.files_outside_mirror),
            "removable_samples": self.removable_samples,
            "shared_samples": self.shared_samples,
            "blockers": self.blockers,
        }


def _removable_work_ids(conn: sqlite3.Connection, root_id: str) -> tuple[list[str], list[str]]:
    """(可删除的作品, 与其他来源共享而必须保留的作品)。

    判定只依赖"该作品是否还被**其他来源**的 revision 绑定"，因此是引用计数而不是
    先到先得；同来源自己的多个 revision 不构成保留理由。
    """

    rows = conn.execute(
        """
        SELECT work_id FROM revision_bindings
        WHERE revision_id IN (SELECT revision_id FROM import_revisions WHERE root_id = ?)
          AND work_id != ''
        GROUP BY work_id
        """,
        (root_id,),
    ).fetchall()
    own = [str(row["work_id"]) for row in rows]
    if not own:
        return [], []
    others = {
        str(row["work_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT rb.work_id FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE ir.root_id != ? AND rb.work_id != '' AND sr.retired_at = ''
            """,
            (root_id,),
        )
    }
    removable = [work_id for work_id in own if work_id not in others]
    shared = [work_id for work_id in own if work_id in others]
    return removable, shared


def _blockers(conn: sqlite3.Connection, root_id: str, *, ignore_job_id: str = "") -> list[str]:
    """删除前置条件：来源自己的扫描/作业必须已经停下。

    ``ignore_job_id`` 排除**删除作业自己**：作业执行时必然处于 running，否则它会
    把自己判成阻断条件而永远无法完成（实测被测试抓到过）。
    """

    blockers: list[str] = []
    # 僵尸扫描（心跳失联）不得永久阻断删除：没有执行者会再刷新它的心跳，
    # 它永远到不了终态。与 hide_source_card 使用同一判定。
    from app.media_v4.sources.scan_state import scan_is_stale

    active_scan = conn.execute(
        "SELECT status, heartbeat_at, started_at FROM source_scans WHERE root_id = ?",
        (root_id,),
    ).fetchall()
    has_active_scan = any(
        str(row["status"]) == "queued"
        or (
            str(row["status"]) in {"running", "cancelling"}
            and not scan_is_stale(str(row["heartbeat_at"] or row["started_at"] or ""))
        )
        for row in active_scan
    )
    if has_active_scan:
        blockers.append("该来源仍有进行中的扫描，请先取消或等待结束")
    active_job = conn.execute(
        """
        SELECT job_type FROM jobs job
        JOIN import_revisions revision ON revision.revision_id = job.revision_id
        WHERE revision.root_id = ? AND job.status IN ('queued', 'running')
          AND job.job_id != ?
        LIMIT 1
        """,
        (root_id, ignore_job_id),
    ).fetchone()
    if active_job is not None:
        blockers.append(f"该来源仍有进行中的后台任务（{active_job['job_type']}），请稍后重试")
    return blockers


def plan_source_deletion(
    database,
    root_id: str,
    *,
    mirror_root: str | Path = "",
    sample_limit: int = 8,
) -> dict:
    """只读预览：会删掉哪些作品/文件，哪些作品因为被其他来源共享而保留。"""

    mirror = Path(mirror_root).resolve(strict=False) if mirror_root else None
    plan = SourceDeletionPlan(root_id=root_id)
    with database.connect() as conn:
        root = conn.execute(
            "SELECT root_id FROM source_roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        if root is None:
            raise KeyError(root_id)
        plan.blockers = _blockers(conn, root_id)
        removable, shared = _removable_work_ids(conn, root_id)
        plan.works_removable = len(removable)
        plan.works_shared = len(shared)
        plan.works_total = len(removable) + len(shared)

        if removable:
            placeholders = ",".join("?" for _ in removable)
            plan.removable_samples = [
                str(row["preferred_title"] or row["work_id"])
                for row in conn.execute(
                    f"SELECT work_id, preferred_title FROM works WHERE work_id IN ({placeholders}) "
                    "ORDER BY preferred_title LIMIT ?",
                    (*removable, sample_limit),
                )
            ]
        if shared:
            placeholders = ",".join("?" for _ in shared)
            plan.shared_samples = [
                str(row["preferred_title"] or row["work_id"])
                for row in conn.execute(
                    f"SELECT work_id, preferred_title FROM works WHERE work_id IN ({placeholders}) "
                    "ORDER BY preferred_title LIMIT ?",
                    (*shared, sample_limit),
                )
            ]

        if removable:
            placeholders = ",".join("?" for _ in removable)
            for row in conn.execute(
                f"SELECT target_path FROM artifacts WHERE work_id IN ({placeholders})",
                removable,
            ):
                target = str(row["target_path"] or "")
                if not target:
                    continue
                plan.artifacts_total += 1
                path = Path(target)
                if mirror is not None:
                    try:
                        resolved = path.resolve(strict=False)
                    except OSError:
                        continue
                    if mirror != resolved and mirror not in resolved.parents:
                        # 镜像根之外的文件（例如来源真实媒体）绝不删除，只如实报告。
                        plan.files_outside_mirror.append(str(resolved))
                        continue
                plan.artifact_files += 1
                try:
                    plan.artifact_bytes += path.stat().st_size
                except OSError:
                    pass
    return plan.to_dict()


def delete_source_library(
    database,
    root_id: str,
    *,
    mirror_root: str | Path,
    batch_size: int = 200,
    progress=None,
    should_cancel=None,
    ignore_job_id: str = "",
) -> dict:
    """执行删除：退役该来源（其作品退出活动库）并回收它们的镜像产物。

    **为什么不是"删作品行"**（这是实现时必须遵守的项目不变量，实测确认）：

    - 数据库触发器 ``v4_confirmed_binding_delete_guard`` 禁止删除已确认 revision 的
      绑定行，``revision_bindings.work_id`` 又是 ``RESTRICT`` —— 只要绑定还在，
      作品行就删不掉；
    - 强行绕开就等于改写已确认事实，违反"confirmed revision 不可反向改写"。

    因此本实现走项目既有的 ``retired_at`` 语义（来源退役即让作品、播放与追更查询
    退出活动库），并**物理删除可再生的镜像产物**（artifacts 行 + 镜像根之内的文件）
    来真正回收磁盘：

    - 只处理**不与其它来源共享**的作品：仍被别的来源绑定的作品必须保留其产物，
      否则会出现"作品还在媒体墙上、文件却被删掉"的悬挂状态；
    - 事实行（evidence / parsed_facts / bindings / revision）一律保留，作为审计；
    - 绝不触碰镜像根之外的任何文件（来源真实媒体）。纯 SQL + 文件操作，不重跑识别。
    """

    mirror = Path(mirror_root).resolve(strict=False)
    removed_files = 0
    removed_artifacts = 0
    skipped_outside = 0
    cancelled = False

    with database.connect() as conn:
        blockers = _blockers(conn, root_id, ignore_job_id=ignore_job_id)
        if blockers:
            return {"ok": False, "reason": blockers[0], "removed_works": 0, "removed_files": 0}
        leaving, shared = _removable_work_ids(conn, root_id)

    def _guard() -> None:
        nonlocal cancelled
        if should_cancel is not None and should_cancel():
            cancelled = True
            raise SourceDeletionCancelled

    for offset in range(0, len(leaving), max(1, batch_size)):
        batch = leaving[offset : offset + max(1, batch_size)]
        try:
            _guard()
        except SourceDeletionCancelled:
            break
        placeholders = ",".join("?" for _ in batch)
        with database.connect() as conn:
            rows = conn.execute(
                f"SELECT artifact_id, target_path FROM artifacts WHERE work_id IN ({placeholders})",
                batch,
            ).fetchall()
            portable_ids: list[str] = []
            for row in rows:
                target = str(row["target_path"] or "")
                artifact_id = str(row["artifact_id"])
                if not target:
                    portable_ids.append(artifact_id)
                    continue
                path = Path(target)
                try:
                    resolved = path.resolve(strict=False)
                except OSError:
                    portable_ids.append(artifact_id)
                    continue
                if mirror != resolved and mirror not in resolved.parents:
                    # 镜像根之外：绝不删除，保留产物行以便用户看见这条异常。
                    skipped_outside += 1
                    portable_ids.append(artifact_id)
                    continue
                try:
                    path.unlink(missing_ok=True)
                    removed_files += 1
                    removed_artifacts += 1
                except OSError:
                    portable_ids.append(artifact_id)
            if portable_ids:
                kept_placeholders = ",".join("?" for _ in portable_ids)
                conn.execute(
                    f"DELETE FROM artifacts WHERE work_id IN ({placeholders}) "
                    f"AND artifact_id NOT IN ({kept_placeholders})",
                    (*batch, *portable_ids),
                )
            else:
                conn.execute(f"DELETE FROM artifacts WHERE work_id IN ({placeholders})", batch)
        if progress is not None:
            progress(min(offset + len(batch), len(leaving)), len(leaving))

    if cancelled:
        return {
            "ok": False,
            "cancelled": True,
            "reason": "已取消",
            "removed_works": 0,
            "removed_files": removed_files,
        }

    now = _now()
    with database.connect() as conn:
        # 退役来源：作品、播放与追更查询据此退出活动库（项目既有的按来源清理语义）。
        conn.execute(
            "UPDATE source_roots SET retired_at = COALESCE(NULLIF(retired_at, ''), ?), "
            "enabled = 0, retired_reason = '用户按来源删除媒体库', updated_at = ? WHERE root_id = ?",
            (now, now, root_id),
        )
    return {
        "ok": True,
        "works_leaving_library": len(leaving),
        "removed_artifacts": removed_artifacts,
        "removed_files": removed_files,
        "files_outside_mirror_skipped": skipped_outside,
        "shared_works_kept": len(shared),
    }


def enqueue_source_deletion(database, root_id: str) -> str:
    """把"按来源删除媒体库"排队为一个异步作业，返回 job_id。

    排队前先做阻断检查：该来源仍有进行中的扫描/后台任务时拒绝（返回 ValueError，
    由 API 映射为 409），避免删除与扫描互相踩。作业挂在**该来源最新的已确认
    revision** 上（jobs 表以 revision 为轴，且执行器只允许 confirmed revision 的任务），
    root_id 由 revision 反查，因此不需要给 jobs 增列。
    """

    import uuid
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        root = conn.execute(
            "SELECT root_id FROM source_roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        if root is None:
            raise KeyError(root_id)
        blockers = _blockers(conn, root_id)
        if blockers:
            raise ValueError(blockers[0])
        revision = conn.execute(
            "SELECT revision_id FROM import_revisions WHERE root_id = ? AND status = 'confirmed' "
            "ORDER BY created_at DESC, revision_id DESC LIMIT 1",
            (root_id,),
        ).fetchone()
        if revision is None:
            raise ValueError("该来源还没有已确认的导入，无需按来源删除媒体库")
        idempotency_key = f"delete_source_library:{root_id}"
        existing = conn.execute(
            "SELECT job_id FROM jobs WHERE idempotency_key = ? AND status IN ('queued', 'running')",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return str(existing["job_id"])
        job_id = "job_" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, status, "
            "created_at, updated_at) VALUES (?, 'delete_source_library', ?, '', ?, 'queued', ?, ?)",
            (job_id, str(revision["revision_id"]), idempotency_key, now, now),
        )
    return job_id


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
