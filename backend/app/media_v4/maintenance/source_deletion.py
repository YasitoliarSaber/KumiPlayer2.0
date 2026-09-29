"""按来源卡片 / 按导入删除媒体库条目。

历史教训：旧版"按来源删除"会长时间占用识别、甚至卡住。因此本模块的红线是：

- **绝不参与识别**：不调用 resolver / parser / scanner，只读已确认的事实表；
- **先预览后执行**：预览只做计数与少量样本（有界查询，不水合对象）；
- **按来源引用计数**：只删除"不再被任何其他来源 revision 绑定"的作品，
  多来源共享的作品必须保留（否则删一个来源会打穿另一个来源的媒体库）；
- **路径防护**：只删除镜像根之内的文件，真实来源媒体永不触碰；
- **先退役再回收**：物理清理前先让来源退出活动库；清理可重复执行，
  中途失败不能留下仍可见却缺少镜像的空卡片。只在退役前接受取消。

外键约束决定了执行顺序（已用 PRAGMA 实测）：

- ``revision_bindings.work_id`` 是 **RESTRICT**：必须先删绑定行，才能删作品行；
- ``artifacts`` 与 ``works`` 之间**没有外键**：产物行与文件必须显式清理，不会级联；
- ``seasons`` / ``episodes`` / ``provider_bindings`` 从 ``works`` 级联，无需手工删。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class SourceDeletionPlan:
    """删除影响范围预览（只含计数，绝不水合全量对象，也不列作品示例）。

    ``works_*`` 是**当前活动媒体库的离库影响**；历史 revision 遗留的 work_id 只作为
    维护集合参与产物回收，不再冒充“被删除的作品数”（见 D2-02）。
    """

    root_id: str
    works_total: int = 0
    works_removable: int = 0
    works_shared: int = 0
    artifacts_total: int = 0
    artifact_files: int = 0
    artifact_bytes: int = 0
    files_outside_mirror: list[str] = field(default_factory=list)
    # 用户 2026-09-28 决定：删除确认框不再列出被删除/共享的作品示例。
    removable_samples: list[str] = field(default_factory=list)
    shared_samples: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    # 只为日志/诊断保留的内部数量：历史维护集合（含已被取代的 revision 遗留 work_id）。
    historical_work_ids_examined: int = 0

    def to_dict(self) -> dict:
        return {
            "root_id": self.root_id,
            # 当前活动媒体库的离库影响（UI 只应读这三个口径）。
            "works_total": self.works_total,
            "works_removable": self.works_removable,
            "works_shared": self.works_shared,
            "current_works_total": self.works_total,
            "current_works_leaving": self.works_removable,
            "current_works_shared": self.works_shared,
            "artifacts_total": self.artifacts_total,
            "artifact_files": self.artifact_files,
            "artifact_bytes": self.artifact_bytes,
            "files_outside_mirror_count": len(self.files_outside_mirror),
            "removable_samples": self.removable_samples,
            "shared_samples": self.shared_samples,
            "blockers": self.blockers,
            "historical_work_ids_examined": self.historical_work_ids_examined,
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
              -- 阶段 2：只有**当前有效**的引用才算"别人还需要它"。缺这条会把已被取代/
              -- 草稿 revision 的历史引用也算成共享，应该回收的产物永远留着。
              AND ir.status = 'confirmed'
            """,
            (root_id,),
        )
    }
    removable = [work_id for work_id in own if work_id not in others]
    shared = [work_id for work_id in own if work_id in others]
    return removable, shared


def _current_work_impact(conn: sqlite3.Connection, root_id: str) -> tuple[int, int, int]:
    """(该来源当前作品数, 离库作品数, 被其他来源共享数)。

    与历史维护集合（``_removable_work_ids``）不同：这里只看**当前活动**的
    confirmed revision 与投影的活动库语义（非退役来源 + confirmed + 参与 EXISTS）。
    已被取代的 revision 遗留 work_id 不影响用户看到的“多少部作品离开媒体库”。
    """

    own = {
        str(row["work_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT rb.work_id FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE ir.root_id = ? AND ir.status = 'confirmed' AND rb.work_id != ''
              AND sr.retired_at = ''
            """,
            (root_id,),
        ).fetchall()
    }
    if not own:
        return (0, 0, 0)
    others = {
        str(row["work_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT rb.work_id FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE ir.root_id != ? AND ir.status = 'confirmed' AND rb.work_id != ''
              AND sr.retired_at = ''
            """,
            (root_id,),
        ).fetchall()
    }
    candidates = own - others
    if not candidates:
        return (len(own), 0, len(own & others))
    placeholders = ",".join("?" for _ in candidates)
    # 与 projection/library.py 的活动库 EXISTS 语义一致：仍有非退役来源的
    # confirmed 绑定才算在媒体墙上，而“当前影响”只能统计真的会离库的作品。
    visible = {
        str(row["work_id"])
        for row in conn.execute(
            f"""
            SELECT DISTINCT rb.work_id FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id IN ({placeholders}) AND ir.status = 'confirmed'
              AND sr.retired_at = ''
            """,
            tuple(sorted(candidates)),
        ).fetchall()
    }
    return (len(own), len(candidates & visible), len(own & others))


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
    """只读预览：当前媒体库会失去多少作品、可能回收多少镜像内产物。

    ``sample_limit`` 保留仅为向后兼容；用户已明确去除作品示例，本函数不再查询任何
    标题样本，避免长标题串和重复标题进入确认框。
    """

    mirror = Path(mirror_root).resolve(strict=False) if mirror_root else None
    plan = SourceDeletionPlan(root_id=root_id)
    with database.connect() as conn:
        root = conn.execute(
            "SELECT root_id FROM source_roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        if root is None:
            raise KeyError(root_id)
        plan.blockers = _blockers(conn, root_id)
        # 产物回收仍按**历史维护集合**计算：被取代 revision 遗留的 work_id 产生的
        # 镜像产物也在本次回收范围内。它不等于用户可见的离库作品数。
        removable, shared = _removable_work_ids(conn, root_id)
        plan.historical_work_ids_examined = len(removable) + len(shared)
        current_total, current_leaving, current_shared = _current_work_impact(conn, root_id)
        plan.works_total = current_total
        plan.works_removable = current_leaving
        plan.works_shared = current_shared

        if removable:
            placeholders = ",".join("?" for _ in removable)
            for row in conn.execute(
                f"SELECT target_path FROM artifacts "
                f"WHERE work_id IN ({placeholders}) AND status != 'removed'",
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
                try:
                    if path.is_file():
                        plan.artifact_files += 1
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
    退出活动库），并物理删除可再生的镜像文件来回收磁盘。被历史 revision 引用的
    artifacts 行保留并标记 removed，不能删除后触发 RESTRICT 外键回滚：

    - 只处理**不与其它来源共享**的作品：仍被别的来源绑定的作品必须保留其产物，
      否则会出现"作品还在媒体墙上、文件却被删掉"的悬挂状态；
    - 事实行（evidence / parsed_facts / bindings / revision）一律保留，作为审计；
    - 绝不触碰镜像根之外的任何文件（来源真实媒体）。纯 SQL + 文件操作，不重跑识别。
    """

    mirror = Path(mirror_root).resolve(strict=False)
    removed_files = 0
    removed_artifacts = 0
    skipped_outside = 0
    failed_files = 0

    with database.connect() as conn:
        blockers = _blockers(conn, root_id, ignore_job_id=ignore_job_id)
        if blockers:
            return {"ok": False, "reason": blockers[0], "removed_works": 0, "removed_files": 0}
        # 产物回收按历史维护集合（含被取代 revision 遗留的 work_id）；
        # 对外报告则用当前活动媒体库的离库口径（见 D2-02）。
        leaving, shared = _removable_work_ids(conn, root_id)
        historical_examined = len(leaving) + len(shared)
        _current_total, current_leaving, current_shared = _current_work_impact(conn, root_id)

    # 取消只在仍可无损退出的阶段有效。此前在批次间取消会留下“来源仍可见、
    # 部分镜像已删除”的半成品；退役后必须继续完成可重复的物理清理。
    if should_cancel is not None and should_cancel():
        return {"ok": False, "cancelled": True, "reason": "已取消", "removed_works": 0, "removed_files": 0}

    now = _now()
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_roots SET retired_at = COALESCE(NULLIF(retired_at, ''), ?), "
            "enabled = 0, retired_reason = '用户按来源删除媒体库', updated_at = ? WHERE root_id = ?",
            (now, now, root_id),
        )

    for offset in range(0, len(leaving), max(1, batch_size)):
        batch = leaving[offset : offset + max(1, batch_size)]
        placeholders = ",".join("?" for _ in batch)
        with database.connect() as conn:
            rows = conn.execute(
                f"SELECT artifact_id, target_path FROM artifacts "
                f"WHERE work_id IN ({placeholders}) AND status != 'removed'",
                batch,
            ).fetchall()
            cleaned_ids: list[str] = []
            for row in rows:
                target = str(row["target_path"] or "")
                artifact_id = str(row["artifact_id"])
                if not target:
                    continue
                path = Path(target)
                try:
                    resolved = path.resolve(strict=False)
                except OSError:
                    continue
                if mirror != resolved and mirror not in resolved.parents:
                    # 镜像根之外：绝不删除，保留产物行以便用户看见这条异常。
                    skipped_outside += 1
                    continue
                try:
                    existed = path.exists()
                    path.unlink(missing_ok=True)
                    if existed:
                        removed_files += 1
                    cleaned_ids.append(artifact_id)
                except OSError:
                    failed_files += 1
            if cleaned_ids:
                cleaned_placeholders = ",".join("?" for _ in cleaned_ids)
                # artifact_references 对产物行有 RESTRICT 外键；成功资料和
                # confirmed revision 的历史引用必须保留，状态与物理文件分离。
                conn.execute(
                    f"UPDATE artifacts SET status = 'removed', updated_at = ? "
                    f"WHERE artifact_id IN ({cleaned_placeholders}) "
                    "AND EXISTS (SELECT 1 FROM artifact_references ar "
                    "WHERE ar.artifact_id = artifacts.artifact_id)",
                    (now, *cleaned_ids),
                )
                conn.execute(
                    f"DELETE FROM artifacts WHERE artifact_id IN ({cleaned_placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM artifact_references ar "
                    "WHERE ar.artifact_id = artifacts.artifact_id)",
                    cleaned_ids,
                )
                removed_artifacts += len(cleaned_ids)
        if progress is not None:
            progress(min(offset + len(batch), len(leaving)), len(leaving))

    return {
        "ok": failed_files == 0,
        "retired": True,
        "reason": "部分生成文件暂时无法清理，将自动重试" if failed_files else "",
        # 当前活动媒体库的离库作品数（不是历史维护集合的大小）。
        "works_leaving_library": current_leaving,
        "removed_artifacts": removed_artifacts,
        "removed_files": removed_files,
        "files_outside_mirror_skipped": skipped_outside,
        "shared_works_kept": current_shared,
        # 仅为日志/诊断：本次实际检查的历史 work_id 数量。
        "historical_work_ids_examined": historical_examined,
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
            # 用户主动"按来源删除媒体库"时，**排队中（queued）**的同源作业必须让路：
            # 它们还没开始执行，而删除会把该来源的整个媒体库带走，继续跑没有意义。
            # 实测（用户真实库）：来源卡停在"0/8 待处理"，该来源的排队作业长期不动，
            # 于是每次点"确认删除媒体库"都被 409 拒绝、**根本没有入队**
            # （jobs 表里 delete_source_library 只有 2 条且都是 succeeded）。
            # 仍在 **running** 的作业不在此列：删除必须继续拒绝，避免与在途任务互相踩。
            queued_jobs = conn.execute(
                """
                SELECT job.job_id FROM jobs job
                JOIN import_revisions revision ON revision.revision_id = job.revision_id
                WHERE revision.root_id = ? AND job.status = 'queued' AND job.job_id != ?
                """,
                (root_id, ""),
            ).fetchall()
            for row in queued_jobs:
                conn.execute(
                    "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE job_id = ?",
                    (now, str(row["job_id"])),
                )
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
        # 幂等键必须**每次尝试都唯一**：jobs.idempotency_key 有 UNIQUE 约束，而历史作业行
        # （包括已 succeeded 的删除）会一直留着。此前键只含 root_id（后又试过 root+revision），
        # 重新删除时都会直接撞 UNIQUE → 500，用户看到的就是"点了没反应"
        # （实测 backend-stderr.log: sqlite3.IntegrityError: UNIQUE constraint failed:
        # jobs.idempotency_key）。
        # "在途复用"改为按 revision 查询在途作业，不再依赖键字符串比较。
        existing = conn.execute(
            "SELECT job_id FROM jobs WHERE job_type = 'delete_source_library' "
            "AND revision_id = ? AND status IN ('queued', 'running') LIMIT 1",
            (str(revision["revision_id"]),),
        ).fetchone()
        if existing is not None:
            return str(existing["job_id"])
        job_id = "job_" + uuid.uuid4().hex
        idempotency_key = f"delete_source_library:{root_id}:{revision['revision_id']}:{job_id}"
        conn.execute(
            "INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, status, "
            "created_at, updated_at) VALUES (?, 'delete_source_library', ?, '', ?, 'queued', ?, ?)",
            (job_id, str(revision["revision_id"]), idempotency_key, now, now),
        )
    return job_id


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
