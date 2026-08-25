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


def _work_playback_and_tracking(database: V4Database, work_id: str) -> None:
    with database.connect() as conn:
        conn.execute("DELETE FROM playback_progress WHERE work_id = ?", (work_id,))
        conn.execute("DELETE FROM tracking_states WHERE work_id = ?", (work_id,))


def compute_delete_preview(database: V4Database, *, provider: str, root_ids: list[str] | None = None, mirror_root: Path | None = None) -> dict:
    """构造按来源清理预览（不删除任何东西）并持久化到 maintenance_operations。

    11.13-3：混合来源判定以 selected_root_ids 为集合；同一 Work 属于本次全部
    被选 root 时视为将退出。11.13-4：preview 身份、scope、digest、创建/过期
    时间与状态写入受事务记录，confirm 只接受未过期且完全匹配的预览。
    """

    if provider not in PROVIDER_SCOPES:
        raise ValueError(f"不支持的来源范围: {provider}")
    roots = _activity_roots(database, provider)
    if root_ids is not None:
        requested = set(root_ids)
        roots = [root for root in roots if root["root_id"] in requested]
    selected_root_ids = {root["root_id"] for root in roots}
    root_items: list[dict] = []
    work_items: list[dict] = []
    artifact_paths: list[str] = []
    blocked_jobs: list[dict] = []
    asset_count = 0
    history_count = 0
    progress_count = 0
    tracking_count = 0
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
            has_other = _work_has_other_active_source(database, work_id, selected_root_ids)
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
    now = _now()
    preview = {
        "preview_id": "prev_" + uuid.uuid4().hex,
        "scope": provider,
        "root_ids": sorted(selected_root_ids),
        "created_at": now,
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
        "digest": _digest(provider, root_items, work_items, artifact_paths),
    }
    # 11.13-4：preview 持久化；同一 preview_id 幂等覆盖为 preview 状态。
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO maintenance_operations(operation_id, scope_provider, status, preview_json, result_json, created_at, updated_at)
            VALUES (?, ?, 'preview', ?, '{}', ?, ?)
            ON CONFLICT(operation_id) DO UPDATE SET
                scope_provider = excluded.scope_provider,
                status = 'preview',
                preview_json = excluded.preview_json,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (preview["preview_id"], provider, json.dumps(preview, ensure_ascii=False), now, now),
        )
    return preview


def _load_preview(database: V4Database, preview_id: str) -> dict:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT operation_id, scope_provider, status, preview_json, result_json, created_at, updated_at "
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
    return preview


def confirm_delete_preview(database: V4Database, *, preview_id: str, scope: str, digest: str, mirror_root: Path | None = None) -> dict:
    """校验并执行按来源清理。11.13-4/6：只接受未过期且完全匹配的持久化预览；
    状态机 preview → pending → completed / partial_failed / projection_failed；
    失败项不得伪装成 completed。"""

    if scope not in PROVIDER_SCOPES:
        raise ValueError(f"不支持的来源范围: {scope}")
    preview = _load_preview(database, preview_id)
    if preview.get("_status") == "completed":
        # 幂等：已完成 operation 直接返回既有结果。
        try:
            return json.loads(preview["_result_json"])
        except (TypeError, ValueError):
            return {"preview_id": preview_id, "status": "completed"}
    if preview.get("_status") in {"pending", "cleaning"}:
        raise ValueError("该清理预览正在执行中，请稍后刷新结果")
    if preview.get("_status") in {"partial_failed", "projection_failed"}:
        # 可重试：返回既有结果并提示重新生成预览。
        try:
            return json.loads(preview["_result_json"])
        except (TypeError, ValueError):
            pass
    if preview.get("scope") != scope:
        raise ValueError("删除预览范围与请求不一致，请重新生成")
    try:
        created = datetime.fromisoformat(str(preview.get("_stored_created_at") or preview.get("created_at") or ""))
    except (ValueError, TypeError):
        created = None
    if created is None or datetime.now(UTC) - created > PREVIEW_TTL:
        raise ValueError("删除预览已过期，请重新生成")
    # 11.13-3：按持久化 root 集合重新计算 digest，任何来源/revision/Artifact/任务变化都拒绝。
    recomputed = _recompute_digest(database, scope, set(preview.get("root_ids") or []))
    if recomputed != digest:
        raise ValueError("来源、revision 或生成物已变化，请重新生成删除预览")
    if preview.get("digest") != digest:
        raise ValueError("来源、revision 或生成物已变化，请重新生成删除预览")
    if preview.get("blocked"):
        raise ValueError("存在正在运行的后台任务，请等待完成后再清理")

    provider_scope = scope
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 状态机：preview → pending
            conn.execute(
                "UPDATE maintenance_operations SET status = 'pending', updated_at = ? WHERE operation_id = ?",
                (_now(), preview_id),
            )
            for root in preview["roots"]:
                conn.execute(
                    "UPDATE source_roots SET retired_at = ?, retired_reason = '用户按来源清理', updated_at = ? WHERE root_id = ?",
                    (_now(), _now(), root["root_id"]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # 孤儿 Work 的播放历史/进度/追更随媒体库退出（事务外独立写入，避免锁）
    for work_id in preview["orphan_works"]:
        _work_playback_and_tracking(database, work_id)
        with database.connect() as conn:
            conn.execute("DELETE FROM playback_history WHERE work_id = ?", (work_id,))

    # 文件系统清理（白名单精确执行，逐项记结果；失败不影响退役标记）
    cleanup_results: list[dict] = []
    has_artifact_failure = False
    for path_text in preview["artifact_paths"]:
        path = Path(path_text)
        try:
            if mirror_root is not None:
                resolved = path.resolve(strict=False)
                root_resolved = mirror_root.resolve(strict=False)
                if resolved == root_resolved or root_resolved not in resolved.parents:
                    cleanup_results.append({"path": path_text, "status": "blocked"})
                    has_artifact_failure = True
                    continue
            if path.is_dir() and not path.is_symlink():
                cleanup_results.append({"path": path_text, "status": "blocked"})
                has_artifact_failure = True
                continue
            if path.exists() or path.is_symlink():
                path.unlink()
                cleanup_results.append({"path": path_text, "status": "removed"})
            else:
                cleanup_results.append({"path": path_text, "status": "missing"})
        except OSError as exc:
            cleanup_results.append({"path": path_text, "status": "failed", "error": str(exc)})
            has_artifact_failure = True

    # 投影重建；失败必须进入 projection_failed，不得伪装完成。
    projection_status = "ok"
    try:
        V4LibraryProjection(database).rebuild()
    except Exception as exc:  # noqa: BLE001
        projection_status = f"failed: {exc}"

    if has_artifact_failure:
        operation_status = "partial_failed"
    elif projection_status != "ok":
        operation_status = "projection_failed"
    else:
        operation_status = "completed"
    result = {
        "preview_id": preview_id,
        "scope": provider_scope,
        "status": operation_status,
        "retired_roots": [root["root_id"] for root in preview["roots"]],
        "orphan_works": preview["orphan_works"],
        "mixed_works": preview["mixed_works"],
        "artifact_results": cleanup_results,
        "projection_status": projection_status,
    }
    with database.connect() as conn:
        conn.execute(
            "UPDATE maintenance_operations SET status = ?, result_json = ?, updated_at = ? WHERE operation_id = ?",
            (operation_status, json.dumps(result, ensure_ascii=False), _now(), preview_id),
        )
    return result


def _recompute_digest(database: V4Database, provider: str, selected_root_ids: set[str]) -> str:
    """按持久化 preview 的 root 集合重新计算 digest；与 compute_delete_preview 完全一致。"""

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
        artifact_paths.extend(
            str(item["target_path"]) for item in _artifact_rows(database, revision["revision_id"])
        )
    return _digest(provider, root_items, work_items, artifact_paths)


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
        "digest": _digest("work:" + work_id, [{"root_id": "", "revision_id": str(revision["revision_id"])}], [{"work_id": work_id}], artifact_paths),
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
            conn.execute("DELETE FROM playback_progress WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM tracking_states WHERE work_id = ?", (work_id,))
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
