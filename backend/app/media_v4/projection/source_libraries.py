"""P-005：来源卡只读 read model。

聚合活动来源根的权威事实：首次/最近确认时间、作品规模、紧凑作品预览与
P-003 作品级进度。所有 membership 只来自最新活动 confirmed revision 的
revision_bindings，不按标题/路径猜测；退役来源统一过滤。
"""

from __future__ import annotations

from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService


def list_source_cards(database: V4Database) -> list[dict]:
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                sr.root_id,
                sr.provider,
                sr.ingest_method,
                sr.source_mode,
                sr.last_scan_mode,
                sr.source_locator,
                sr.playback_locator,
                sr.route_id,
                sr.display_name,
                sr.enabled,
                (
                    SELECT MIN(ir.confirmed_at) FROM import_revisions ir
                    WHERE ir.root_id = sr.root_id AND ir.status = 'confirmed'
                ) AS added_at,
                (
                    SELECT MAX(ir.confirmed_at) FROM import_revisions ir
                    WHERE ir.root_id = sr.root_id AND ir.status = 'confirmed'
                ) AS updated_at,
                latest.revision_id,
                latest.confirmed_at,
                (
                    SELECT COUNT(*) FROM revision_bindings rb
                    WHERE rb.revision_id = latest.revision_id AND rb.work_id != ''
                ) AS work_count,
                (
                    SELECT COUNT(DISTINCT rb.asset_id) FROM revision_bindings rb
                    WHERE rb.revision_id = latest.revision_id AND rb.asset_id IS NOT NULL
                ) AS asset_count,
                (
                    SELECT COUNT(*) FROM revision_evidence re
                    WHERE re.revision_id = latest.revision_id
                ) AS evidence_count
            FROM source_roots sr
            JOIN import_revisions latest ON latest.revision_id = (
                SELECT candidate.revision_id
                FROM import_revisions candidate
                WHERE candidate.root_id = sr.root_id AND candidate.status = 'confirmed'
                ORDER BY candidate.confirmed_at DESC, candidate.created_at DESC, candidate.revision_id DESC
                LIMIT 1
            )
            WHERE sr.retired_at = ''
            ORDER BY latest.confirmed_at DESC, sr.updated_at DESC, sr.root_id
            """
        ).fetchall()

    cards = []
    progress_service = V4RevisionService(database)
    revision_ids = [str(row["revision_id"]) for row in rows]
    summaries: dict[str, dict] = {}
    if revision_ids:
        placeholders = ",".join("?" for _ in revision_ids)
        with database.connect() as conn:
            for row2 in conn.execute(
                f"""
                SELECT
                    revision_id,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
                FROM jobs
                WHERE revision_id IN ({placeholders})
                GROUP BY revision_id
                """,
                revision_ids,
            ).fetchall():
                summaries[str(row2["revision_id"])] = {
                    "total": int(row2["total"] or 0),
                    "queued": int(row2["queued"] or 0),
                    "running": int(row2["running"] or 0),
                    "succeeded": int(row2["succeeded"] or 0),
                    "failed": int(row2["failed"] or 0),
                    "cancelled": int(row2["cancelled"] or 0),
                }
    for row in rows:
        progress = _safe_progress(progress_service, str(row["revision_id"]))
        job_summary = summaries.get(str(row["revision_id"]), {
            "total": 0, "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
        })
        work_previews = _work_previews(database, str(row["revision_id"]), progress)
        cards.append({
            "root_id": str(row["root_id"]),
            "provider": str(row["provider"] or ""),
            "ingest_method": str(row["ingest_method"] or ""),
            "source_mode": str(row["source_mode"] or ""),
            "last_scan_mode": str(row["last_scan_mode"] or ""),
            "has_confirmed_baseline": True,
            "source_locator": str(row["source_locator"] or ""),
            "playback_locator": str(row["playback_locator"] or ""),
            "route_id": str(row["route_id"] or ""),
            "display_name": str(row["display_name"] or "")
                or str(row["source_locator"] or "")
                or f"{row['provider']} 媒体库",
            "enabled": int(row["enabled"]),
            "added_at": str(row["added_at"] or ""),
            "updated_at": str(row["updated_at"] or row["confirmed_at"] or ""),
            "revision_id": str(row["revision_id"]),
            "latest_revision_id": str(row["revision_id"]),
            "revision_state": progress.get("overall_status") or "completed",
            "work_count": int(row["work_count"]),
            "asset_count": int(row["asset_count"]),
            "evidence_count": int(row["evidence_count"]),
            "attention_count": sum(
                1 for unit in progress.get("work_units") or []
                if unit.get("overall_status") in {"failed", "cancelled", "needs_attention"}
            ),
            "last_error": _first_error(progress),
            "work_previews": work_previews,
            "progress": {
                "state": progress.get("overall_status") or "completed",
                "stage": _progress_stage(progress),
                "current_work_id": _current_work_id(progress),
                "current_work_title": _current_work_title(progress),
                "completed_work_count": _completed_count(progress),
                "total_work_count": len(progress.get("work_units") or []),
                "percent": _progress_percent(progress),
                "message": _progress_message(progress),
            },
            "available_actions": _available_actions(progress),
            "can_resume": _can_resume(progress) or (
                job_summary["queued"] > 0
                or job_summary["running"] > 0
                or job_summary["failed"] > 0
                or job_summary["cancelled"] > 0
            ),
            "job_summary": job_summary,
        })
    return cards


def _safe_progress(progress_service: V4RevisionService, revision_id: str) -> dict:
    try:
        return progress_service.get_execution_progress(revision_id)
    except KeyError:
        return {}


def _work_previews(database: V4Database, revision_id: str, progress: dict) -> list[dict]:
    current_work_ids = {
        str(unit["work_id"])
        for unit in progress.get("work_units") or []
        if unit.get("overall_status") in {"running_mirror", "running_metadata"}
    }
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                w.work_id, w.preferred_title AS title, w.year, w.work_type,
                (SELECT COUNT(DISTINCT rb2.episode_id) FROM revision_bindings rb2
                 WHERE rb2.revision_id = ? AND rb2.work_id = w.work_id) AS episode_count,
                (SELECT COUNT(DISTINCT rb3.asset_id) FROM revision_bindings rb3
                 WHERE rb3.revision_id = ? AND rb3.work_id = w.work_id AND rb3.asset_id IS NOT NULL) AS asset_count
            FROM revision_bindings rb
            JOIN works w ON w.work_id = rb.work_id
            WHERE rb.revision_id = ? AND rb.work_id != ''
            GROUP BY w.work_id
            ORDER BY
                CASE WHEN w.work_id IN ({current}) THEN 0 ELSE 1 END,
                w.preferred_title COLLATE NOCASE,
                w.work_id
            LIMIT 6
            """.format(current=",".join("?" for _ in current_work_ids)),
            (revision_id, revision_id, revision_id, *current_work_ids),
        ).fetchall()
    return [
        {
            "work_id": str(row["work_id"]),
            "title": str(row["title"] or row["work_id"]),
            "year": row["year"],
            "media_type": "movie" if str(row["work_type"] or "") == "movie" else "tv",
            "poster_path": "",
            "episode_count": int(row["episode_count"] or 0),
            "asset_count_for_source": int(row["asset_count"] or 0),
        }
        for row in rows
    ]


def _first_error(progress: dict) -> str:
    for unit in progress.get("work_units") or []:
        for slot in ("mirror", "metadata"):
            job = unit.get(slot) or {}
            if job.get("last_error"):
                return str(job["last_error"])
    return ""


def _progress_stage(progress: dict) -> str:
    stage = (progress.get("stage_summary") or {}).get("mirror") or {}
    if stage.get("status") == "running":
        return "mirror"
    metadata = (progress.get("stage_summary") or {}).get("metadata") or {}
    if metadata.get("status") == "running":
        return "metadata"
    projection = (progress.get("stage_summary") or {}).get("projection") or {}
    if projection.get("status") in {"queued", "running"}:
        return "projection"
    return "idle"


def _current_work_id(progress: dict) -> str:
    for unit in progress.get("work_units") or []:
        if unit.get("overall_status") in {"running_mirror", "running_metadata"}:
            return str(unit["work_id"])
    return ""


def _current_work_title(progress: dict) -> str:
    for unit in progress.get("work_units") or []:
        if unit.get("overall_status") in {"running_mirror", "running_metadata"}:
            return str(unit.get("title") or "")
    return ""


def _completed_count(progress: dict) -> int:
    return sum(1 for unit in progress.get("work_units") or [] if unit.get("overall_status") == "completed")


def _progress_percent(progress: dict) -> int | None:
    units = progress.get("work_units") or []
    if not units:
        return None
    return round(_completed_count(progress) / len(units) * 100)


def _progress_message(progress: dict) -> str:
    overall = progress.get("overall_status")
    if overall == "running":
        title = _current_work_title(progress)
        stage = _progress_stage(progress)
        label = {"mirror": "正在生成镜像", "metadata": "正在获取媒体信息", "projection": "正在更新媒体库"}.get(stage, "处理中")
        return f"{label}" + (f"《{title}》" if title else "")
    if overall == "needs_attention":
        return "有任务需要处理"
    if overall == "queued":
        return "正在准备任务"
    return "上次导入已处理完毕"


def _available_actions(progress: dict) -> list[str]:
    actions = ["inspect"]
    if progress.get("overall_status") in {"running", "queued"}:
        actions.append("resume")
    else:
        actions.append("update")
    return actions


def _can_resume(progress: dict) -> bool:
    overall = progress.get("overall_status")
    return overall in {"running", "queued", "needs_attention"}
