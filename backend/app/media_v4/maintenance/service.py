"""P-005：V4 媒体库维护——按来源清理预览与确认。

删除采用“活动退役 + 精确受控清理 + 投影重建”：
- 选中的来源根立即退役（retired_at），从活动媒体库/来源卡/播放/追更候选退出；
- 仅清理 preview 中登记、位于受管镜像根内且通过 canonical path 校验的
  KumiPlayer 生成物；源视频、挂载盘媒体、外部 TXT、OpenList 远端对象、
  配置与凭据绝不触碰；
- 确认 revision / SourceEvidence / ParsedFacts 作为审计事实保留；
- 混合来源 Work 完整保留，其个人状态不受影响；孤儿 Work 的播放/追更状态
  随媒体库退出。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.media_v4.persistence.database import V4Database
from app.media_v4.projection.library import V4LibraryProjection

PREVIEW_TTL = timedelta(minutes=10)

PROVIDER_SCOPES = {"local", "pan115", "baidu", "quark", "all"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _read_conn(database: V4Database, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """调用方事务内复用同一连接；事务外自建短连接。"""

    if conn is not None:
        yield conn
    else:
        with database.connect() as c:
            yield c


def _activity_roots(database: V4Database, provider: str, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _read_conn(database, conn) as c:
        if provider == "all":
            rows = c.execute(
                "SELECT root_id, provider, source_locator FROM source_roots WHERE retired_at = ''"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT root_id, provider, source_locator FROM source_roots WHERE retired_at = '' AND provider = ?",
                (provider,),
            ).fetchall()
    return [dict(row) for row in rows]


def _latest_confirmed_revision(database: V4Database, root_id: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    with _read_conn(database, conn) as c:
        row = c.execute(
            """
            SELECT revision_id, confirmed_at FROM import_revisions
            WHERE root_id = ? AND status = 'confirmed'
            ORDER BY confirmed_at DESC, created_at DESC, revision_id DESC LIMIT 1
            """,
            (root_id,),
        ).fetchone()
    return dict(row) if row else None


def _active_jobs(database: V4Database, revision_id: str, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _read_conn(database, conn) as c:
        rows = c.execute(
            "SELECT job_id, job_type, status FROM jobs WHERE revision_id = ? AND status IN ('queued', 'running')",
            (revision_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _revision_work_ids(database: V4Database, revision_id: str, *, conn: sqlite3.Connection | None = None) -> set[str]:
    with _read_conn(database, conn) as c:
        rows = c.execute(
            "SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id = ? AND work_id != ''",
            (revision_id,),
        ).fetchall()
    return {str(row["work_id"]) for row in rows}


def _work_has_other_active_source(database: V4Database, work_id: str, excluded_root_ids: set[str], *, conn: sqlite3.Connection | None = None) -> bool:
    """11.13-3：删除后是否仍有活动来源，按本次将同时退役的 root 集合判定。"""

    if not excluded_root_ids:
        return False
    placeholders = ",".join("?" for _ in excluded_root_ids)
    with _read_conn(database, conn) as c:
        row = c.execute(
            f"""
            SELECT 1 FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed'
              AND sr.retired_at = '' AND sr.root_id NOT IN ({placeholders})
            LIMIT 1
            """,
            (work_id, *excluded_root_ids),
        ).fetchone()
    return row is not None


def _artifact_rows(database: V4Database, revision_id: str, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    with _read_conn(database, conn) as c:
        rows = c.execute(
            "SELECT artifact_id, artifact_type, target_path, revision_id FROM artifacts WHERE revision_id = ? ORDER BY target_path",
            (revision_id,),
        ).fetchall()
    return [dict(row) for row in rows]
def _work_playback_and_tracking(conn, work_id: str) -> None:
    """孤儿 Work 个人状态清理（播放历史/进度/追更）；必须在调用方事务 conn 内执行。"""

    conn.execute("DELETE FROM playback_history WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM playback_progress WHERE work_id = ?", (work_id,))
    conn.execute("DELETE FROM tracking_states WHERE work_id = ?", (work_id,))


def _mirror_identity(mirror_root: Path | None) -> str:
    if mirror_root is None:
        return ""
    try:
        return str(mirror_root.resolve(strict=False))
    except OSError:
        return str(mirror_root)


def _whitelisted_artifact_rows(database: V4Database, revision_id: str, mirror_root: Path | None, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """统一白名单：返回真实 artifact row（artifact_id/revision_id + canonical target）。

    只包含数据库登记、位于受管镜像根内、通过 canonical path 校验且非目录的
    生成物；canonical target 使用 resolve 后的绝对路径，供 digest 与执行使用。
    """

    rows: list[dict] = []
    for artifact in _artifact_rows(database, revision_id, conn=conn):
        path = Path(artifact["target_path"])
        if mirror_root is None:
            continue
        try:
            resolved = path.resolve(strict=False)
            root_resolved = mirror_root.resolve(strict=False)
            if resolved == root_resolved or root_resolved not in resolved.parents:
                continue
            if resolved.is_dir() and not resolved.is_symlink():
                continue
        except OSError:
            continue
        rows.append({
            "artifact_id": str(artifact["artifact_id"]),
            "revision_id": str(artifact["revision_id"]),
            "target_path": str(resolved),
        })
    return rows


def _digest(
    provider: str,
    roots: list[dict],
    works: list[dict],
    artifact_paths: list[str],
    mirror_identity: str,
    jobs: list[dict] | None = None,
) -> str:
    """§11.15.3-3：digest 覆盖完整 canonical item 集合、selected root+revision、
    mirror identity 与活动 job 快照；preview 与 confirm 复用同一函数。"""

    payload = {
        "provider": provider,
        "mirror_root_identity": mirror_identity,
        "roots": sorted({(item["root_id"], item["revision_id"]) for item in roots}),
        "works": sorted({item["work_id"] for item in works}),
        "artifacts": sorted(artifact_paths),
        "jobs": sorted(
            {(item["revision_id"], item["job_id"], item["job_type"], item["status"]) for item in (jobs or [])}
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _validate_requested_roots(database: V4Database, provider: str, root_ids: list[str] | None) -> list[dict]:
    """空、未知、与 scope/provider 不一致的 root 显式拒绝，不静默空操作。"""

    if provider not in PROVIDER_SCOPES:
        raise ValueError(f"不支持的来源范围: {provider}")
    roots = _activity_roots(database, provider)
    if root_ids is None:
        if not roots:
            raise ValueError("当前范围没有活动来源根，无需清理")
        return roots
    if not root_ids:
        raise ValueError("未选择任何来源根，请先选择要清理的媒体库")
    requested = set(root_ids)
    if len(requested) != len(root_ids):
        raise ValueError("来源根选择存在重复，请重新选择")
    selected = [root for root in roots if root["root_id"] in requested]
    if len(selected) != len(requested):
        missing = sorted(requested - {root["root_id"] for root in selected})
        raise ValueError(f"来源根不存在或不属于当前范围: {', '.join(missing)}")
    return selected


def compute_delete_preview(database: V4Database, *, provider: str, root_ids: list[str] | None = None, mirror_root: Path | None = None) -> dict:
    """后端生成完整清理计划并一次事务写入 operation + 全部 items。

    计划项绑定真实 artifacts.artifact_id 与 canonical target path；digest 覆盖
    完整 canonical item 集合、selected root+revision、mirror identity 与活动
    job 快照。返回 public DTO（仅 count 与 ≤20 条相对镜像根脱敏摘要、可读
    名称与 warnings）；完整执行路径与内部 root/work 身份只存 SQLite。
    """

    requested_roots = _validate_requested_roots(database, provider, root_ids)
    roots: list[dict] = []
    skipped_roots: list[dict] = []
    revisions_by_root: dict[str, dict] = {}
    for root in requested_roots:
        revision = _latest_confirmed_revision(database, root["root_id"])
        if revision is None:
            # “全部来源”与来源分类清理面向已建立的媒体库。扫描草稿或已取消
            # 的来源根没有 confirmed revision，也就没有可退役的 V4 媒体库事实；
            # 它们不应阻断同一范围内其他已确认来源的清理。
            if root_ids is not None:
                raise ValueError("所选来源尚未完成导入，无法按媒体库记录清理")
            skipped_roots.append(root)
            continue
        roots.append(root)
        revisions_by_root[str(root["root_id"])] = revision
    if not roots:
        raise ValueError("当前范围没有已确认的媒体库来源，无需清理")

    selected_root_ids = {root["root_id"] for root in roots}
    mirror_identity = _mirror_identity(mirror_root)
    root_items: list[dict] = []
    work_items: list[dict] = []
    item_rows: list[dict] = []
    blocked_jobs: list[dict] = []
    job_snapshot: list[dict] = []
    asset_count = 0
    for root in roots:
        revision = revisions_by_root[str(root["root_id"])]
        root_items.append({
            "root_id": root["root_id"],
            "provider": root["provider"],
            "source_locator": root["source_locator"],
            "revision_id": revision["revision_id"],
            "confirmed_at": revision["confirmed_at"],
        })
        jobs = _active_jobs(database, revision["revision_id"])
        blocked_jobs.extend(jobs)
        job_snapshot.extend({
            "revision_id": revision["revision_id"],
            "job_id": job["job_id"],
            "job_type": job["job_type"],
            "status": job["status"],
        } for job in jobs)
        for work_id in sorted(_revision_work_ids(database, revision["revision_id"])):
            work_items.append({
                "work_id": work_id,
                "root_id": root["root_id"],
                "mixed": _work_has_other_active_source(database, work_id, selected_root_ids),
            })
        with database.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT asset_id) AS c FROM revision_bindings WHERE revision_id = ? AND asset_id IS NOT NULL",
                (revision["revision_id"],),
            ).fetchone()
            asset_count += int(row["c"] or 0)
        for artifact_row in _whitelisted_artifact_rows(database, revision["revision_id"], mirror_root):
            item_rows.append({
                "root_id": root["root_id"],
                "revision_id": revision["revision_id"],
                "artifact_id": artifact_row["artifact_id"],
                "target_path": artifact_row["target_path"],
            })

    orphan_work_ids = sorted({item["work_id"] for item in work_items if not item["mixed"]})
    mixed_work_ids = sorted({item["work_id"] for item in work_items if item["mixed"]})
    history_count = 0
    progress_count = 0
    tracking_count = 0
    if orphan_work_ids:
        placeholders = ",".join("?" for _ in orphan_work_ids)
        with database.connect() as conn:
            history_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM playback_history WHERE work_id IN (" + placeholders + ")",
                orphan_work_ids,
            ).fetchone()["c"] or 0)
            progress_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM playback_progress WHERE work_id IN (" + placeholders + ")",
                orphan_work_ids,
            ).fetchone()["c"] or 0)
            tracking_count = int(conn.execute(
                "SELECT COUNT(*) AS c FROM tracking_states WHERE work_id IN (" + placeholders + ")",
                orphan_work_ids,
            ).fetchone()["c"] or 0)

    artifact_paths = [item["target_path"] for item in item_rows]
    now = _now()
    digest = _digest(provider, root_items, work_items, artifact_paths, mirror_identity, job_snapshot)
    preview_id = "prev_" + uuid.uuid4().hex
    expires_at = (datetime.now(UTC) + PREVIEW_TTL).isoformat()

    summaries: list[str] = []
    for path_text in artifact_paths:
        try:
            if mirror_root is None:
                summaries.append(Path(path_text).name)
            else:
                rel = Path(path_text).relative_to(mirror_root.resolve(strict=False))
                summaries.append(str(rel))
        except ValueError:
            summaries.append(Path(path_text).name)
    # 内部 preview：完整身份与计划，存 SQLite 供确认/恢复逻辑使用。
    skipped_provider_counts = [
        {"provider": provider_name, "count": count}
        for provider_name, count in sorted(
            ((provider_name, sum(1 for root in skipped_roots if root["provider"] == provider_name))
            for provider_name in {str(root["provider"]) for root in skipped_roots}),
            key=lambda item: item[0],
        )
    ]
    warnings = [
        "源视频、挂载盘媒体、外部 TXT、OpenList 远端对象、配置与凭据始终保留",
        "混合来源作品会完整保留，仅退出被清理来源的贡献",
        "孤儿作品的播放历史、进度与追更状态随媒体库退出",
    ]
    if skipped_roots:
        warnings.append(f"{len(skipped_roots)} 个尚未确认导入的来源未纳入本次清理，它们没有可清理的媒体库数据")

    preview = {
        "preview_id": preview_id,
        "scope": provider,
        "root_ids": sorted(selected_root_ids),
        "created_at": now,
        "expires_at": expires_at,
        "root_count": len(root_items),
        "skipped_root_count": len(skipped_roots),
        "skipped_provider_counts": skipped_provider_counts,
        "work_count": len(work_items),
        "orphan_work_count": len(orphan_work_ids),
        "mixed_work_count": len(mixed_work_ids),
        "asset_count": asset_count,
        "artifact_count": len(artifact_paths),
        "artifact_summaries": summaries[:20],
        "blocked": bool(blocked_jobs),
        "blocked_job_count": len(blocked_jobs),
        "blocked_job_types": sorted({job["job_type"] for job in blocked_jobs}),
        "history_count": history_count,
        "progress_count": progress_count,
        "tracking_count": tracking_count,
        "warnings": warnings,
        "roots": root_items,
        "orphan_works": orphan_work_ids,
        "mixed_works": mixed_work_ids,
        "root_names": [{"root_id": item["root_id"], "provider": item["provider"]} for item in root_items],
        "digest": digest,
    }
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO maintenance_operations(
                    operation_id, scope_provider, status, preview_json, result_json,
                    digest, root_ids_json, mirror_root_identity, expires_at, created_at, updated_at
                ) VALUES (?, ?, 'preview', ?, '{}', ?, ?, ?, ?, ?, ?)
                """,
                (preview_id, provider, json.dumps(preview, ensure_ascii=False),
                 digest, json.dumps(sorted(selected_root_ids), ensure_ascii=False),
                 mirror_identity, expires_at, now, now),
            )
            for index, item in enumerate(item_rows):
                conn.execute(
                    """
                    INSERT INTO maintenance_operation_items(
                        item_id, operation_id, artifact_id, root_id, revision_id, target_path,
                        plan_status, result_status, result_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'planned', 'pending', '', ?)
                    """,
                    (f"item-{preview_id}-{index}", preview_id, item["artifact_id"],
                     item["root_id"], item["revision_id"], item["target_path"], now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return _public_preview(preview)


def _public_preview(preview: dict) -> dict:
    """§11.15.3-5：preview DTO 只返回脱敏计数、≤20 条相对镜像根摘要、warnings
    与可读名称；不含 source locator、内部 Work ID、完整执行路径或完整 job 列表。"""

    return {
        "preview_id": preview["preview_id"],
        "scope": preview["scope"],
        "created_at": preview["created_at"],
        "expires_at": preview["expires_at"],
        "root_count": preview["root_count"],
        "skipped_root_count": preview.get("skipped_root_count", 0),
        "skipped_provider_counts": preview.get("skipped_provider_counts", []),
        "work_count": preview["work_count"],
        "orphan_work_count": preview["orphan_work_count"],
        "mixed_work_count": preview["mixed_work_count"],
        "asset_count": preview["asset_count"],
        "artifact_count": preview["artifact_count"],
        "artifact_summaries": preview["artifact_summaries"][:20],
        "blocked": preview["blocked"],
        "blocked_job_count": preview["blocked_job_count"],
        "blocked_job_types": preview["blocked_job_types"],
        "history_count": preview["history_count"],
        "progress_count": preview["progress_count"],
        "tracking_count": preview["tracking_count"],
        "warnings": preview["warnings"],
        "root_names": preview["root_names"],
        "digest": preview["digest"],
    }


def _load_preview(database: V4Database, preview_id: str, *, conn: sqlite3.Connection | None = None) -> dict:
    with _read_conn(database, conn) as c:
        row = c.execute(
            "SELECT operation_id, scope_provider, status, preview_json, result_json, digest, root_ids_json, mirror_root_identity, expires_at, created_at, updated_at "
            "FROM maintenance_operations WHERE operation_id = ?",
            (preview_id,),
        ).fetchone()
    if row is None:
        raise ValueError("删除预览不存在或已失效，请重新生成")
    try:
        preview = json.loads(row["preview_json"] or "{}")
    except (TypeError, ValueError):
        raise ValueError("删除预览数据损坏，请重新生成") from None
    preview["_status"] = str(row["status"] or "")
    preview["_result_json"] = str(row["result_json"] or "")
    preview["_stored_created_at"] = str(row["created_at"] or "")
    preview["_stored_digest"] = str(row["digest"] or "")
    preview["_stored_mirror_identity"] = str(row["mirror_root_identity"] or "")
    preview["_stored_expires_at"] = str(row["expires_at"] or "")
    return preview


def _recompute_digest_for_preview(
    database: V4Database,
    preview: dict,
    mirror_root: Path | None,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """§11.15.3-3：基于同一完整受管计划与当前状态重新计算 digest。

    preview 与 confirm 使用同一白名单函数与同一 job 快照读取；root/revision/
    活动任务/镜像根/生成物变化都会使重算 digest 与受管 digest 不一致而
    fail-closed。必须在调用方事务内（conn 非空）调用以获得一致性视图。
    """

    selected_root_ids = set(preview.get("root_ids") or [])
    provider = str(preview.get("scope") or "")
    roots = [root for root in _activity_roots(database, provider, conn=conn) if root["root_id"] in selected_root_ids]
    root_items: list[dict] = []
    work_items: list[dict] = []
    artifact_paths: list[str] = []
    job_snapshot: list[dict] = []
    for root in roots:
        revision = _latest_confirmed_revision(database, root["root_id"], conn=conn)
        if revision is None:
            continue
        root_items.append({
            "root_id": root["root_id"],
            "revision_id": revision["revision_id"],
        })
        for job in _active_jobs(database, revision["revision_id"], conn=conn):
            job_snapshot.append({
                "revision_id": revision["revision_id"],
                "job_id": job["job_id"],
                "job_type": job["job_type"],
                "status": job["status"],
            })
        for work_id in sorted(_revision_work_ids(database, revision["revision_id"], conn=conn)):
            work_items.append({
                "work_id": work_id,
                "root_id": root["root_id"],
                "mixed": _work_has_other_active_source(database, work_id, selected_root_ids, conn=conn),
            })
        artifact_paths.extend(
            row["target_path"] for row in _whitelisted_artifact_rows(database, revision["revision_id"], mirror_root, conn=conn)
        )
    return _digest(
        provider, root_items, work_items, artifact_paths,
        str(preview.get("_stored_mirror_identity") or ""), job_snapshot,
    )


def _verify_and_claim_tx(
    conn: sqlite3.Connection,
    database: V4Database,
    preview_id: str,
    scope: str,
    digest: str,
    mirror_root: Path | None,
) -> dict:
    """§11.15.3-3：在调用方 BEGIN IMMEDIATE 事务内完成全部校验、原子领取、
    来源退役与孤儿个人状态清理。

    任何变化（job/revision/root/digest/TTL/mirror identity）或行数不符都抛
    异常由调用方回滚；root 更新同时限制 retired_at = ''，防止重复退役。
    """

    if scope not in PROVIDER_SCOPES:
        raise ValueError(f"不支持的来源范围: {scope}")
    preview = _load_preview(database, preview_id, conn=conn)
    status = preview.get("_status")
    if status == "completed":
        try:
            return {"__completed__": True, "result": json.loads(preview["_result_json"])}
        except (TypeError, ValueError):
            return {"__completed__": True, "result": {"preview_id": preview_id, "status": "completed"}}
    if status in {"pending", "cleaning"}:
        raise ValueError("该清理正在执行中，请稍后刷新结果")
    if preview.get("scope") != scope:
        raise ValueError("删除预览范围与请求不一致，请重新生成")
    if _mirror_identity(mirror_root) != preview.get("_stored_mirror_identity"):
        raise ValueError("镜像根目录已变化，请重新生成删除预览")
    if preview.get("_stored_digest") != digest or preview.get("digest") != digest:
        raise ValueError("来源、revision 或生成物已变化，请重新生成删除预览")
    # 事务内一致视图重算：job 快照/root/revision/镜像根任何变化都 fail-closed。
    if _recompute_digest_for_preview(database, preview, mirror_root, conn=conn) != preview.get("_stored_digest"):
        raise ValueError("来源、revision、任务或生成物已变化，请重新生成删除预览")
    try:
        expires = datetime.fromisoformat(str(preview.get("_stored_expires_at") or ""))
    except (ValueError, TypeError):
        expires = None
    if expires is None or datetime.now(UTC) > expires:
        raise ValueError("删除预览已过期，请重新生成")
    if preview.get("blocked"):
        raise ValueError("存在正在运行的后台任务，请等待完成后再清理")
    now = _now()
    cursor = conn.execute(
        "UPDATE maintenance_operations SET status = 'pending', updated_at = ? "
        "WHERE operation_id = ? AND status = 'preview'",
        (now, preview_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("该清理预览已被其他请求处理或状态不允许确认，请刷新结果")
    for root in preview.get("roots", []):
        root_cursor = conn.execute(
            "UPDATE source_roots SET retired_at = ?, retired_reason = '用户按来源清理', updated_at = ? "
            "WHERE root_id = ? AND retired_at = ''",
            (now, now, root["root_id"]),
        )
        if root_cursor.rowcount != 1:
            raise ValueError("来源根状态已变化，请重新生成删除预览")
    for work_id in preview.get("orphan_works", []):
        _work_playback_and_tracking(conn, work_id)
    return {"__completed__": False, "preview": preview}


def _persist_operation_status(database: V4Database, preview_id: str, status: str, result: dict) -> None:
    with database.connect() as conn:
        conn.execute(
            "UPDATE maintenance_operations SET status = ?, result_json = ?, updated_at = ? WHERE operation_id = ?",
            (status, json.dumps(result, ensure_ascii=False), _now(), preview_id),
        )


def _cleanup_items_tx(
    conn: sqlite3.Connection,
    preview_id: str,
    mirror_root: Path | None,
    *,
    retry_only: bool = False,
) -> tuple[list[dict], bool]:
    """在调用方事务内逐项清理并持久化结果。

    §11.15.3-4：每个 item 无论 removed/missing/blocked/failed 都写回
    result_status；blocked 不能 continue 留 pending。retry_only 时只重试
    明确可恢复的条目（pending/failed），blocked/removed/missing 保持终态。
    """

    rows = conn.execute(
        "SELECT item_id, target_path, result_status FROM maintenance_operation_items "
        "WHERE operation_id = ? ORDER BY item_id",
        (preview_id,),
    ).fetchall()
    results: list[dict] = []
    has_failure = False
    now = _now()
    for row in rows:
        item_id = str(row["item_id"])
        path_text = str(row["target_path"])
        result_status = str(row["result_status"] or "")
        if result_status in {"removed", "missing"}:
            results.append({"path": _sanitized_summary(path_text, mirror_root), "status": result_status, "reused": True})
            continue
        if retry_only and result_status not in {"pending", "failed"}:
            results.append({"path": _sanitized_summary(path_text, mirror_root), "status": result_status, "reused": True})
            continue
        status = "pending"
        error = ""
        path = Path(path_text)
        try:
            if mirror_root is not None:
                resolved = path.resolve(strict=False)
                root_resolved = mirror_root.resolve(strict=False)
                if resolved == root_resolved or root_resolved not in resolved.parents:
                    status = "blocked"
                    has_failure = True
                elif resolved.is_dir() and not resolved.is_symlink():
                    status = "blocked"
                    has_failure = True
                elif resolved.exists() or resolved.is_symlink():
                    resolved.unlink()
                    status = "removed"
                else:
                    status = "missing"
            else:
                if path.is_dir() and not path.is_symlink():
                    status = "blocked"
                    has_failure = True
                elif path.exists() or path.is_symlink():
                    path.unlink()
                    status = "removed"
                else:
                    status = "missing"
        except OSError as exc:
            status = "failed"
            error = str(exc)
            has_failure = True
        conn.execute(
            "UPDATE maintenance_operation_items SET result_status = ?, result_error = ?, updated_at = ? WHERE item_id = ?",
            (status, error, now, item_id),
        )
        results.append({"path": _sanitized_summary(path_text, mirror_root), "status": status, "error": error or None})
    return results, has_failure


def _sanitized_summary(path_text: str, mirror_root: Path | None) -> str:
    """相对镜像根的脱敏摘要；绝不含完整本地绝对路径。"""

    if mirror_root is None:
        return Path(path_text).name
    try:
        return str(Path(path_text).relative_to(mirror_root))
    except ValueError:
        return Path(path_text).name


def _rebuild_projection(database: V4Database) -> str:
    try:
        V4LibraryProjection(database).rebuild()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {exc}"


def _public_result(
    preview_id: str,
    scope: str,
    status: str,
    preview: dict,
    cleanup_results: list[dict],
    projection_status: str,
) -> dict:
    """§11.15.3-5：result DTO 只返回脱敏计数、≤20 条相对镜像根摘要与可读
    名称；不含内部 Work ID、source locator 或完整执行路径。"""

    return {
        "preview_id": preview_id,
        "scope": scope,
        "status": status,
        "retired_root_count": len(preview.get("roots", [])),
        "root_names": preview.get("root_names", []),
        "orphan_work_count": preview.get("orphan_work_count", 0),
        "mixed_work_count": preview.get("mixed_work_count", 0),
        "artifact_count": len(cleanup_results),
        "artifact_results": cleanup_results[:20],
        "projection_status": projection_status,
    }


def confirm_delete_preview(database: V4Database, *, preview_id: str, scope: str, digest: str, mirror_root: Path | None = None) -> dict:
    """§11.15.3-3：校验、原子领取、来源退役、孤儿个人状态清理与逐项结果持久化
    合并为同一个 BEGIN IMMEDIATE 事务；任何变化/行数不符都回滚并抛错（409），
    不得退役任何 root。投影重建在提交后执行（允许 projection_failed 语义）。"""

    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            outcome = _verify_and_claim_tx(conn, database, preview_id, scope, digest, mirror_root)
            if outcome["__completed__"]:
                conn.commit()
                return outcome["result"]
            preview = outcome["preview"]
            cleanup_results, has_artifact_failure = _cleanup_items_tx(conn, preview_id, mirror_root)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    projection_status = _rebuild_projection(database)
    if has_artifact_failure:
        operation_status = "partial_failed"
    elif projection_status != "ok":
        operation_status = "projection_failed"
    else:
        operation_status = "completed"
    result = _public_result(preview_id, scope, operation_status, preview, cleanup_results, projection_status)
    _persist_operation_status(database, preview_id, operation_status, result)
    return result


def resume_operation(database: V4Database, *, preview_id: str, mirror_root: Path | None = None) -> dict:
    """partial_failed / projection_failed 基于同一 operation 恢复；不重新生成 preview。

    原子认领（UPDATE ... WHERE status IN ('partial_failed','projection_failed')）
    与逐项清理结果持久化在同一 BEGIN IMMEDIATE 事务内；resume 只重试明确
    可恢复的条目（pending/failed），blocked/removed/missing 保持终态。
    """

    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            preview = _load_preview(database, preview_id, conn=conn)
            status = preview.get("_status")
            if status == "completed":
                conn.commit()
                try:
                    return json.loads(preview["_result_json"])
                except (TypeError, ValueError):
                    return {"preview_id": preview_id, "status": "completed"}
            if status not in {"partial_failed", "projection_failed"}:
                raise ValueError("该清理预览当前不可恢复，请刷新结果")
            if _mirror_identity(mirror_root) != preview.get("_stored_mirror_identity"):
                raise ValueError("镜像根目录已变化，请重新生成删除预览")
            now = _now()
            cursor = conn.execute(
                "UPDATE maintenance_operations SET status = 'pending', updated_at = ? "
                "WHERE operation_id = ? AND status IN ('partial_failed', 'projection_failed')",
                (now, preview_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("该清理预览已被其他请求处理，请刷新结果")
            cleanup_results, has_artifact_failure = _cleanup_items_tx(conn, preview_id, mirror_root, retry_only=True)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    projection_status = _rebuild_projection(database)
    if has_artifact_failure:
        operation_status = "partial_failed"
    elif projection_status != "ok":
        operation_status = "projection_failed"
    else:
        operation_status = "completed"
    result = _public_result(preview_id, str(preview.get("scope") or ""), operation_status, preview, cleanup_results, projection_status)
    _persist_operation_status(database, preview_id, operation_status, result)
    return result


def _scope_from_digest_preview(preview: dict) -> str:
    return str(preview.get("scope") or "all")
def compute_work_delete_preview(database: V4Database, *, work_id: str, mirror_root: Path | None = None) -> dict:
    """单作品删除预览：列出该作品在活动来源中的受控生成物与个人状态影响。"""

    with database.connect() as conn:
        revision = conn.execute(
            """
            SELECT ir.revision_id FROM import_revisions ir
            JOIN revision_bindings rb ON rb.revision_id = ir.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed' AND sr.retired_at = ''
            ORDER BY ir.confirmed_at DESC LIMIT 1
            """,
            (work_id,),
        ).fetchone()
        if revision is None:
            raise ValueError("该作品不在任何活动媒体库中")
        artifact_rows = conn.execute(
            "SELECT artifact_id, target_path FROM artifacts WHERE revision_id = ? AND work_id = ? ORDER BY target_path",
            (revision["revision_id"], work_id),
        ).fetchall()
        playback_count = conn.execute(
            "SELECT COUNT(*) AS c FROM playback_progress WHERE work_id = ?", (work_id,)
        ).fetchone()["c"]
        tracking_count = conn.execute(
            "SELECT COUNT(*) AS c FROM tracking_states WHERE work_id = ?", (work_id,)
        ).fetchone()["c"]
    artifact_paths: list[str] = []
    for artifact in artifact_rows:
        path = Path(artifact["target_path"])
        if mirror_root is not None:
            try:
                resolved = path.resolve(strict=False)
                root_resolved = mirror_root.resolve(strict=False)
                if resolved == root_resolved or root_resolved not in resolved.parents:
                    continue
            except OSError:
                continue
        artifact_paths.append(str(path))
    preview = {
        "preview_id": "prev_work_" + uuid.uuid4().hex,
        "work_id": work_id,
        "artifact_count": len(artifact_paths),
        "artifact_paths": artifact_paths,
        "playback_count": int(playback_count or 0),
        "tracking_count": int(tracking_count or 0),
        "digest": _digest("work:" + work_id, [{"root_id": "", "revision_id": str(revision["revision_id"])}], [{"work_id": work_id}], artifact_paths, ""),
    }
    return preview


def confirm_work_delete(database: V4Database, *, work_id: str, preview_id: str, digest: str, mirror_root: Path | None = None) -> dict:
    """校验并执行单作品删除：标记隐藏 + 清理受控生成物 + 播放/追更状态 + 投影重建。"""

    current = compute_work_delete_preview(database, work_id=work_id, mirror_root=mirror_root)
    if current["digest"] != digest:
        raise ValueError("作品状态已变化，请重新生成删除预览")
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT override_json FROM work_overrides WHERE work_id = ?", (work_id,)
            ).fetchone()
            payload = json.loads(existing["override_json"]) if existing else {}
            payload["hidden"] = True
            conn.execute(
                """
                INSERT INTO work_overrides(work_id, override_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(work_id) DO UPDATE SET override_json = excluded.override_json, updated_at = excluded.updated_at
                """,
                (work_id, json.dumps(payload, ensure_ascii=False), _now(), _now()),
            )
            _work_playback_and_tracking(conn, work_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    cleanup_results: list[dict] = []
    for path_text in current["artifact_paths"]:
        path = Path(path_text)
        try:
            if mirror_root is not None:
                resolved = path.resolve(strict=False)
                root_resolved = mirror_root.resolve(strict=False)
                if resolved == root_resolved or root_resolved not in resolved.parents:
                    cleanup_results.append({"path": path_text, "status": "blocked"})
                    continue
            if path.is_dir() and not path.is_symlink():
                cleanup_results.append({"path": path_text, "status": "blocked"})
                continue
            if path.exists() or path.is_symlink():
                path.unlink()
                cleanup_results.append({"path": path_text, "status": "removed"})
            else:
                cleanup_results.append({"path": path_text, "status": "missing"})
        except OSError as exc:
            cleanup_results.append({"path": path_text, "status": "failed", "error": str(exc)})
    try:
        V4LibraryProjection(database).rebuild()
        projection_status = "ok"
    except Exception as exc:  # noqa: BLE001
        projection_status = f"failed: {exc}"
    return {
        "preview_id": preview_id,
        "work_id": work_id,
        "status": "completed",
        "artifact_results": cleanup_results,
        "projection_status": projection_status,
    }
