"""Playback state API backed by V4 Asset identities."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.media_v4 import get_database
from app.media_v4.playback.session import get_v4_playback_manager
from app.media_v4.playback.store import V4PlaybackStore

router = APIRouter(prefix="/api/playback", tags=["playback"])


class PlayRequest(BaseModel):
    work_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    asset_id: str = ""


class ProgressRequest(BaseModel):
    work_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    asset_id: str = ""
    position: float = Field(ge=0)
    duration: float = Field(ge=0)
    completed: bool = False


class ProgressMarkRequest(BaseModel):
    work_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    asset_id: str = ""
    completed: bool


def _default_asset(episode_id: str, asset_id: str = "", work_id: str = "") -> dict:
    database = get_database()
    with database.connect() as conn:
        row = conn.execute(
            "SELECT rb.work_id FROM revision_bindings rb "
            "JOIN import_revisions ir ON ir.revision_id=rb.revision_id "
            "JOIN source_roots sr ON sr.root_id=ir.root_id AND sr.retired_at='' "
            "WHERE ir.status='confirmed' AND (?='' OR rb.work_id=?) "
            "AND (rb.episode_id=? OR (rb.episode_id IS NULL AND ?='movie:'||rb.work_id)) "
            "AND (?='' OR rb.asset_id=?) ORDER BY ir.confirmed_at DESC LIMIT 1",
            (work_id, work_id, episode_id, episode_id, asset_id, asset_id),
        ).fetchone()
    if row is None:
        raise KeyError((episode_id, asset_id))
    return get_v4_playback_manager(database)._resolve_asset(str(row['work_id']), episode_id, asset_id)

@router.post("/play")
def play(request: PlayRequest):
    try:
        session = get_v4_playback_manager(get_database()).play(
            request.work_id,
            request.episode_id,
            request.asset_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="剧集或 Asset 不存在") from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return session


@router.post("/stop")
def stop():
    return get_v4_playback_manager(get_database()).stop()


@router.get("/status")
def status():
    return get_v4_playback_manager(get_database()).status()


@router.post("/progress")
def report_progress(request: ProgressRequest):
    try:
        asset = _default_asset(request.episode_id, request.asset_id, request.work_id)
        V4PlaybackStore(get_database()).save_progress(
            request.work_id,
            request.episode_id,
            asset["asset_id"],
            request.position,
            request.duration,
            request.completed,
        )
        return V4PlaybackStore(get_database()).get_progress(request.episode_id, asset["asset_id"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="剧集或 Asset 不存在") from exc


@router.post("/progress/mark")
def mark_progress(request: ProgressMarkRequest):
    return report_progress(
        ProgressRequest(
            work_id=request.work_id,
            episode_id=request.episode_id,
            asset_id=request.asset_id,
            position=0,
            duration=0,
            completed=request.completed,
        )
    )


@router.get("/progress")
def list_progress(work_id: str | None = None):
    with get_database().connect() as conn:
        rows = conn.execute(
            "SELECT * FROM playback_progress WHERE (? IS NULL OR work_id = ?) ORDER BY updated_at DESC",
            (work_id, work_id),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/history")
def history(limit: int = 50, work_id: str | None = None):
    """P-006：播放历史是独立事件，按播放时间降序并严格尊重 limit。"""

    bounded = max(1, min(int(limit), 200))
    params: list = []
    where = ""
    if work_id:
        where = "WHERE h.work_id = ?"
        params.append(work_id)
    with get_database().connect() as conn:
        rows = conn.execute(
            "SELECT h.*, COALESCE(p.position, 0) AS position, COALESCE(p.duration, 0) AS duration, "
            "COALESCE(p.completed, 0) AS completed, COALESCE(p.updated_at, h.played_at) AS updated_at, "
            "s.local_season_number AS season_number, e.local_episode_number AS episode_number, "
            "COALESCE(e.display_title, '') AS episode_title "
            "FROM playback_history h "
            "LEFT JOIN playback_progress p ON p.work_id = h.work_id AND p.episode_id = h.episode_id "
            "AND p.asset_id = h.asset_id "
            "LEFT JOIN episodes e ON e.episode_id = h.episode_id AND e.work_id = h.work_id "
            "LEFT JOIN seasons s ON s.season_id = e.season_id "
            + where + " ORDER BY h.played_at DESC, h.event_id LIMIT ?",
            (*params, bounded),
        ).fetchall()
        # 与详情页消费同一份最近成功资料；每个 Work 只读取一次，
        # 按 Episode 身份取图，绝不把作品 fanart 当成分集缩略图。
        mappings_by_work: dict[str, dict[str, dict]] = {}
        items: list[dict] = []
        for row in rows:
            item = dict(row)
            key = str(item["work_id"])
            if key not in mappings_by_work:
                mappings_by_work[key] = _history_episode_metadata(conn, key)
            episode = mappings_by_work[key].get(str(item["episode_id"]), {})
            item["thumb_path"] = str(
                episode.get("local_thumb_path") or episode.get("still_url") or episode.get("thumb_path") or ""
            )
            item["episode_title"] = str(episode.get("title") or item["episode_title"])
            item["completed"] = bool(item["completed"])
            items.append(item)
    return {"items": items, "limit": bounded}


def _history_episode_metadata(conn, work_id: str) -> dict[str, dict]:
    from app.media_v4.persistence.metadata_lifecycle import referenced_metadata

    row = conn.execute(
        "SELECT sb.metadata_json FROM scrape_bindings sb "
        "JOIN import_revisions ir ON ir.revision_id = sb.revision_id "
        "JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = '' "
        "WHERE sb.work_id = ? AND ir.status = 'confirmed' "
        "ORDER BY sb.updated_at DESC, sb.binding_id DESC LIMIT 1",
        (work_id,),
    ).fetchone()
    try:
        metadata = json.loads(row["metadata_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    retained = referenced_metadata(conn, work_id)
    if retained is not None:
        metadata = {**metadata, **retained}
    mappings = metadata.get("episode_mappings")
    return {
        str(item["episode_id"]): item for item in mappings
        if isinstance(item, dict) and item.get("episode_id")
    } if isinstance(mappings, list) else {}
