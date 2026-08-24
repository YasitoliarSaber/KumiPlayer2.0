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
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.media_v4.persistence.database import V4Database
from app.media_v4.projection.library import V4LibraryProjection

PREVIEW_TTL = timedelta(minutes=10)

PROVIDER_SCOPES = {"local", "pan115", "baidu", "quark", "all"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(provider: str, roots: list[dict], works: list[dict], artifact_paths: list[str]) -> str:
    payload = {
        "provider": provider,
        "roots": sorted({(item["root_id"], item["revision_id"]) for item in roots}),
        "works": sorted({item["work_id"] for item in works}),
        "artifacts": sorted(artifact_paths),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _activity_roots(database: V4Database, provider: str) -> list[dict]:
    with database.connect() as conn:
        if provider == "all":
            rows = conn.execute(
                "SELECT root_id, provider, source_locator FROM source_roots WHERE retired_at = ''"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT root_id, provider, source_locator FROM source_roots WHERE retired_at = '' AND provider = ?",
                (provider,),
            ).fetchall()
    return [dict(row) for row in rows]


def _latest_confirmed_revision(database: V4Database, root_id: str) -> dict | None:
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT revision_id, confirmed_at FROM import_revisions
            WHERE root_id = ? AND status = 'confirmed'
            ORDER BY confirmed_at DESC, created_at DESC, revision_id DESC LIMIT 1
            """,
            (root_id,),
        ).fetchone()
    return dict(row) if row else None


def _active_jobs(database: V4Database, revision_id: str) -> list[dict]:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT job_id, job_type, status FROM jobs WHERE revision_id = ? AND status IN ('queued', 'running')",
            (revision_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _revision_work_ids(database: V4Database, revision_id: str) -> set[str]:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id = ? AND work_id != ''",
            (revision_id,),
        ).fetchall()
    return {str(row["work_id"]) for row in rows}


def _work_has_other_active_source(database: V4Database, work_id: str, excluded_root_id: str) -> bool:
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed'
              AND sr.retired_at = '' AND sr.root_id != ?
            LIMIT 1
            """,
            (work_id, excluded_root_id),
        ).fetchone()
    return row is not None


def _artifact_rows(database: V4Database, revision_id: str) -> list[dict]:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT artifact_id, artifact_type, target_path FROM artifacts WHERE revision_id = ? ORDER BY target_path",
            (revision_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _work_playback_and_tracking(database: V4Database, work_id: str) -> None:
    with database.connect() as conn:
        conn.execute("DELETE FROM playback_progress WHERE work_id = ?", (work_id,))
        conn.execute("DELETE FROM tracking_states WHERE work_id = ?", (work_id,))


def compute_delete_preview(database: V4Database, *, provider: str, mirror_root: Path | None = None) -> dict:
    """构造按来源清理预览（不删除任何东西）。"""

    if provider not in PROVIDER_SCOPES:
        raise ValueError(f"不支持的来源范围: {provider}")
    roots = _activity_roots(database, provider)
    root_items: list[dict] = []
    work_items: list[dict] = []
    artifact_paths: list[str] = []
    blocked_jobs: list[dict] = []
    asset_count = 0
    for root in roots:
        revision = _latest_confirmed_revision(database, root["root_id"])
        if revision is None:
            continue
        root_items.append({
            "root_id": root["root_id"],
            "provider": root["provider"],
            "source_locator": root["source_locator"],
            "revision_id": revision["revision_id"],
            "confirmed_at": revision["confirmed_at"],
        })
        jobs = _active_jobs(database, revision["revision_id"])
        blocked_jobs.extend(jobs)
        work_ids = _revision_work_ids(database, revision["revision_id"])
        for work_id in sorted(work_ids):
            has_other = _work_has_other_active_source(database, work_id, root["root_id"])
            work_items.append({
                "work_id": work_id,
                "root_id": root["root_id"],
                "mixed": has_other,
            })
        with database.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT asset_id) AS c FROM revision_bindings WHERE revision_id = ? AND asset_id IS NOT NULL",
                (revision["revision_id"],),
            ).fetchone()
            asset_count += int(row["c"] or 0)
        for artifact in _artifact_rows(database, revision["revision_id"]):
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

    orphan_work_ids = sorted({item["work_id"] for item in work_items if not item["mixed"]})
    mixed_work_ids = sorted({item["work_id"] for item in work_items if item["mixed"]})
    preview = {
        "preview_id": "prev_" + uuid.uuid4().hex,
        "scope": provider,
        "expires_at": (datetime.now(UTC) + PREVIEW_TTL).isoformat(),
        "root_count": len(root_items),
        "work_count": len(work_items),
        "orphan_work_count": len(orphan_work_ids),
        "mixed_work_count": len(mixed_work_ids),
        "asset_count": asset_count,
        "artifact_count": len(artifact_paths),
        "artifact_paths": artifact_paths[:50],
        "blocked": bool(blocked_jobs),
        "blocked_jobs": blocked_jobs[:20],
        "warnings": [
            "源视频、挂载盘媒体、外部 TXT、OpenList 远端对象、配置与凭据始终保留",
            "混合来源作品会完整保留，仅退出被清理来源的贡献",
        ],
        "roots": root_items,
        "orphan_works": orphan_work_ids,
        "mixed_works": mixed_work_ids,
        "digest": _digest(provider, root_items, work_items, artifact_paths),
    }
    return preview


def _recompute_digest(database: V4Database, provider: str) -> str:
    """确认时重新计算 digest；来源/revision/Artifact/任务变化会导致不一致。"""

    roots = _activity_roots(database, provider)
    root_items: list[dict] = []
    work_items: list[dict] = []
    artifact_paths: list[str] = []
    for root in roots:
        revision = _latest_confirmed_revision(database, root["root_id"])
        if revision is None:
            continue
        root_items.append({
            "root_id": root["root_id"],
            "revision_id": revision["revision_id"],
        })
        for work_id in sorted(_revision_work_ids(database, revision["revision_id"])):
            work_items.append({
                "work_id": work_id,
                "root_id": root["root_id"],
                "mixed": _work_has_other_active_source(database, work_id, root["root_id"]),
            })
        artifact_paths.extend(
            str(item["target_path"]) for item in _artifact_rows(database, revision["revision_id"])
        )
    return _digest(provider, root_items, work_items, artifact_paths)


def confirm_delete_preview(database: V4Database, *, preview_id: str, scope: str, digest: str, mirror_root: Path | None = None) -> dict:
    """校验并执行按来源清理。幂等：已完成 operation 直接返回既有结果。"""

    if scope not in PROVIDER_SCOPES:
        raise ValueError(f"不支持的来源范围: {scope}")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT operation_id, scope_provider, status, preview_json, result_json FROM maintenance_operations WHERE operation_id = ?",
            (preview_id,),
        ).fetchone()
    if row is not None:
        if row["status"] == "completed":
            return json.loads(row["result_json"])
        raise ValueError("该清理预览正在执行或已失败，请重新生成预览")
    # 重新计算 digest；来源、revision 或 Artifact 状态变化时返回不一致。
    if _recompute_digest(database, scope) != digest:
        raise ValueError("来源、revision 或生成物已变化，请重新生成删除预览")
    preview = compute_delete_preview(database, provider=scope, mirror_root=mirror_root)
    if preview["digest"] != digest:
        raise ValueError("来源、revision 或生成物已变化，请重新生成删除预览")
    if preview["blocked"]:
        raise ValueError("存在正在运行的后台任务，请等待完成后再清理")

    provider_scope = scope
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 幂等登记 operation
            conn.execute(
                "INSERT OR IGNORE INTO maintenance_operations(operation_id, scope_provider, status, preview_json, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?)",
                (preview_id, provider_scope, json.dumps(preview, ensure_ascii=False), _now(), _now()),
            )
            # 退役来源根（同一事务）
            for root in preview["roots"]:
                conn.execute(
                    "UPDATE source_roots SET retired_at = ?, retired_reason = '用户按来源清理', updated_at = ? WHERE root_id = ?",
                    (_now(), _now(), root["root_id"]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # 孤儿 Work 的播放/追更状态随媒体库退出（事务外独立写入，避免锁）
    for work_id in preview["orphan_works"]:
        _work_playback_and_tracking(database, work_id)

    # 文件系统清理（白名单精确执行，逐项记结果；失败不影响已完成的退役标记）
    cleanup_results: list[dict] = []
    for path_text in preview["artifact_paths"]:
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

    # 重建投影
    try:
        V4LibraryProjection(database).rebuild()
        projection_status = "ok"
    except Exception as exc:  # noqa: BLE001
        projection_status = f"failed: {exc}"

    result = {
        "preview_id": preview_id,
        "scope": provider_scope,
        "status": "completed",
        "retired_roots": [root["root_id"] for root in preview["roots"]],
        "orphan_works": preview["orphan_works"],
        "mixed_works": preview["mixed_works"],
        "artifact_results": cleanup_results,
        "projection_status": projection_status,
    }
    with database.connect() as conn:
        conn.execute(
            "UPDATE maintenance_operations SET status = 'completed', result_json = ?, updated_at = ? WHERE operation_id = ?",
            (json.dumps(result, ensure_ascii=False), _now(), preview_id),
        )
    return result


def _scope_from_digest_preview(preview: dict) -> str:
    return str(preview.get("scope") or "all")
