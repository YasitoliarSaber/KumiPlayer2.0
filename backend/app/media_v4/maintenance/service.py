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


def _work_has_other_active_source(database: V4Database, work_id: str, excluded_root_ids: set[str]) -> bool:
    """11.13-3：删除后是否仍有活动来源，按本次将同时退役的 root 集合判定。"""

    if not excluded_root_ids:
        return False
    placeholders = ",".join("?" for _ in excluded_root_ids)
    with database.connect() as conn:
        row = conn.execute(
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


def _artifact_rows(database: V4Database, revision_id: str) -> list[dict]:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT artifact_id, artifact_type, target_path FROM artifacts WHERE revision_id = ? ORDER BY target_path",
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


def _whitelisted_artifact_paths(database: V4Database, revision_id: str, mirror_root: Path | None) -> list[str]:
    """统一白名单：数据库登记、位于受管镜像根内、canonical path 校验。"""

    paths: list[str] = []
    for artifact in _artifact_rows(database, revision_id):
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
        paths.append(str(path))
    return paths


def _digest(provider: str, roots: list[dict], works: list[dict], artifact_paths: list[str], mirror_identity: str) -> str:
    payload = {
        "provider": provider,
        "mirror_root_identity": mirror_identity,
        "roots": sorted({(item["root_id"], item["revision_id"]) for item in roots}),
        "works": sorted({item["work_id"] for item in works}),
        "artifacts": sorted(artifact_paths),
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

    返回仅包含 count 与至多 20 条相对镜像根的脱敏摘要；完整执行路径只存在于
    SQLite operation items，前端不得参与执行路径。
    """

    roots = _validate_requested_roots(database, provider, root_ids)
    selected_root_ids = {root["root_id"] for root in roots}
    mirror_identity = _mirror_identity(mirror_root)
    root_items: list[dict] = []
    work_items: list[dict] = []
    item_rows: list[dict] = []
    blocked_jobs: list[dict] = []
    asset_count = 0
    for root in roots:
        revision = _latest_confirmed_revision(database, root["root_id"])
        if revision is None:
            raise ValueError(f"来源根 {root['root_id']} 没有已确认 revision，无法清理")
        root_items.append({
            "root_id": root["root_id"],
            "provider": root["provider"],
            "source_locator": root["source_locator"],
            "revision_id": revision["revision_id"],
            "confirmed_at": revision["confirmed_at"],
        })
        jobs = _active_jobs(database, revision["revision_id"])
        blocked_jobs.extend(jobs)
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
        for path_text in _whitelisted_artifact_paths(database, revision["revision_id"], mirror_root):
            item_rows.append({
                "root_id": root["root_id"],
                "revision_id": revision["revision_id"],
                "artifact_id": f"art-{uuid.uuid4().hex}",
                "target_path": path_text,
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
    digest = _digest(provider, root_items, work_items, artifact_paths, mirror_identity)
    preview_id = "prev_" + uuid.uuid4().hex
    expires_at = (datetime.now(UTC) + PREVIEW_TTL).isoformat()

    summaries: list[str] = []
    for path_text in artifact_paths:
        try:
            if mirror_root is None:
                summaries.append(Path(path_text).name)
            else:
                rel = Path(path_text).relative_to(mirror_root)
                summaries.append(str(rel))
        except ValueError:
            summaries.append(Path(path_text).name)
    preview = {
        "preview_id": preview_id,
        "scope": provider,
        "root_ids": sorted(selected_root_ids),
        "created_at": now,
        "expires_at": expires_at,
        "root_count": len(root_items),
        "work_count": len(work_items),
        "orphan_work_count": len(orphan_work_ids),
        "mixed_work_count": len(mixed_work_ids),
        "asset_count": asset_count,
        "artifact_count": len(artifact_paths),
        "artifact_summaries": summaries[:20],
        "blocked": bool(blocked_jobs),
        "blocked_jobs": blocked_jobs[:20],
        "history_count": history_count,
        "progress_count": progress_count,
        "tracking_count": tracking_count,
        "warnings": [
            "源视频、挂载盘媒体、外部 TXT、OpenList 远端对象、配置与凭据始终保留",
            "混合来源作品会完整保留，仅退出被清理来源的贡献",
            "孤儿作品的播放历史、进度与追更状态随媒体库退出",
        ],
        "roots": root_items,
        "orphan_works": orphan_work_ids,
        "mixed_works": mixed_work_ids,
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
    return preview


def _load_preview(database: V4Database, preview_id: str) -> dict:
    with database.connect() as conn:
        row = conn.execute(
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


def _recompute_digest_for_preview(database: V4Database, preview: dict, mirror_root: Path | None) -> str:
    """§11.14.1-5：基于同一完整受管计划与当前状态重新计算 digest。

    preview 与 confirm 使用同一白名单函数；root/revision/任务/镜像根变化都会
    使重算 digest 与受管 digest 不一致而 fail-closed。
    """

    selected_root_ids = set(preview.get("root_ids") or [])
    provider = str(preview.get("scope") or "")
    roots = [root for root in _activity_roots(database, provider) if root["root_id"] in selected_root_ids]
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
                "mixed": _work_has_other_active_source(database, work_id, selected_root_ids),
            })
        artifact_paths.extend(_whitelisted_artifact_paths(database, revision["revision_id"], mirror_root))
    return _digest(provider, root_items, work_items, artifact_paths, str(preview.get("_stored_mirror_identity") or ""))


def _verify_preview(database: V4Database, preview_id: str, scope: str, digest: str, mirror_root: Path | None) -> dict:
    if scope not in PROVIDER_SCOPES:
        raise ValueError(f"不支持的来源范围: {scope}")
    preview = _load_preview(database, preview_id)
    status = preview.get("_status")
    if status == "completed":
        try:
            return json.loads(preview["_result_json"])
        except (TypeError, ValueError):
            return {"preview_id": preview_id, "status": "completed"}
    if status in {"pending", "cleaning"}:
        raise ValueError("该清理正在执行中，请稍后刷新结果")
    if preview.get("scope") != scope:
        raise ValueError("删除预览范围与请求不一致，请重新生成")
    # 先校验镜像根身份（§11.14.1-5），避免 mirror 变化被 digest 错误掩盖。
    if _mirror_identity(mirror_root) != preview.get("_stored_mirror_identity"):
        raise ValueError("镜像根目录已变化，请重新生成删除预览")
    if preview.get("_stored_digest") != digest or preview.get("digest") != digest:
        raise ValueError("来源、revision 或生成物已变化，请重新生成删除预览")
    if _recompute_digest_for_preview(database, preview, mirror_root) != preview.get("_stored_digest"):
        raise ValueError("来源、revision 或生成物已变化，请重新生成删除预览")
    try:
        expires = datetime.fromisoformat(str(preview.get("_stored_expires_at") or ""))
    except (ValueError, TypeError):
        expires = None
    if expires is None or datetime.now(UTC) > expires:
        raise ValueError("删除预览已过期，请重新生成")
    if preview.get("blocked"):
        raise ValueError("存在正在运行的后台任务，请等待完成后再清理")
    return preview


def _persist_operation_status(database: V4Database, preview_id: str, status: str, result: dict) -> None:
    with database.connect() as conn:
        conn.execute(
            "UPDATE maintenance_operations SET status = ?, result_json = ?, updated_at = ? WHERE operation_id = ?",
            (status, json.dumps(result, ensure_ascii=False), _now(), preview_id),
        )


def _retire_and_cleanup_personal_state(database: V4Database, preview_id: str, preview: dict) -> None:
    now = _now()
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "UPDATE maintenance_operations SET status = 'pending', updated_at = ? "
                "WHERE operation_id = ? AND status = 'preview'",
                (now, preview_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise ValueError("该清理预览已被其他请求处理或状态不允许确认，请刷新结果")
            for root in preview["roots"]:
                conn.execute(
                    "UPDATE source_roots SET retired_at = ?, retired_reason = '用户按来源清理', updated_at = ? WHERE root_id = ?",
                    (now, now, root["root_id"]),
                )
            for work_id in preview["orphan_works"]:
                _work_playback_and_tracking(conn, work_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _cleanup_items(database: V4Database, preview_id: str, mirror_root: Path | None) -> tuple[list[dict], bool]:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT item_id, target_path, result_status FROM maintenance_operation_items "
            "WHERE operation_id = ? ORDER BY item_id",
            (preview_id,),
        ).fetchall()
    results: list[dict] = []
    has_failure = False
    for row in rows:
        item_id = str(row["item_id"])
        path_text = str(row["target_path"])
        result_status = str(row["result_status"] or "")
        if result_status in {"removed", "missing"}:
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
                    continue
            if path.is_dir() and not path.is_symlink():
                status = "blocked"
                has_failure = True
                continue
            if path.exists() or path.is_symlink():
                path.unlink()
                status = "removed"
            else:
                status = "missing"
        except OSError as exc:
            status = "failed"
            error = str(exc)
            has_failure = True
        with database.connect() as conn:
            conn.execute(
                "UPDATE maintenance_operation_items SET result_status = ?, result_error = ?, updated_at = ? WHERE item_id = ?",
                (status, error, _now(), item_id),
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


def confirm_delete_preview(database: V4Database, *, preview_id: str, scope: str, digest: str, mirror_root: Path | None = None) -> dict:
    preview = _verify_preview(database, preview_id, scope, digest, mirror_root)
    if isinstance(preview, dict) and preview.get("status") == "completed":
        return preview
    _retire_and_cleanup_personal_state(database, preview_id, preview)
    cleanup_results, has_artifact_failure = _cleanup_items(database, preview_id, mirror_root)
    projection_status = _rebuild_projection(database)
    if has_artifact_failure:
        operation_status = "partial_failed"
    elif projection_status != "ok":
        operation_status = "projection_failed"
    else:
        operation_status = "completed"
    result = {
        "preview_id": preview_id,
        "scope": scope,
        "status": operation_status,
        "retired_roots": [root["root_id"] for root in preview["roots"]],
        "orphan_works": preview["orphan_works"],
        "mixed_works": preview["mixed_works"],
        "artifact_results": cleanup_results,
        "projection_status": projection_status,
    }
    _persist_operation_status(database, preview_id, operation_status, result)
    return result


def resume_operation(database: V4Database, *, preview_id: str, mirror_root: Path | None = None) -> dict:
    """partial_failed / projection_failed 基于同一 operation 恢复；不重新生成 preview。"""

    preview = _load_preview(database, preview_id)
    status = preview.get("_status")
    if status == "completed":
        try:
            return json.loads(preview["_result_json"])
        except (TypeError, ValueError):
            return {"preview_id": preview_id, "status": "completed"}
    if status not in {"partial_failed", "projection_failed"}:
        raise ValueError("该清理预览当前不可恢复，请刷新结果")
    if _mirror_identity(mirror_root) != preview.get("_stored_mirror_identity"):
        raise ValueError("镜像根目录已变化，请重新生成删除预览")
    now = _now()
    with database.connect() as conn:
        cursor = conn.execute(
            "UPDATE maintenance_operations SET status = 'pending', updated_at = ? "
            "WHERE operation_id = ? AND status IN ('partial_failed', 'projection_failed')",
            (now, preview_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("该清理预览已被其他请求处理，请刷新结果")
    cleanup_results, has_artifact_failure = _cleanup_items(database, preview_id, mirror_root)
    projection_status = _rebuild_projection(database)
    if has_artifact_failure:
        operation_status = "partial_failed"
    elif projection_status != "ok":
        operation_status = "projection_failed"
    else:
        operation_status = "completed"
    result = {
        "preview_id": preview_id,
        "scope": preview.get("scope", ""),
        "status": operation_status,
        "retired_roots": [root["root_id"] for root in preview.get("roots", [])],
        "orphan_works": preview.get("orphan_works", []),
        "mixed_works": preview.get("mixed_works", []),
        "artifact_results": cleanup_results,
        "projection_status": projection_status,
    }
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
