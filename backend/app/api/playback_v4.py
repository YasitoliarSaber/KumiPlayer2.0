"""Playback state API backed by V4 Asset identities."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.media_v4 import get_database
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


def _default_asset(episode_id: str, asset_id: str = "") -> dict:
    database = get_database()
    with database.connect() as conn:
        if asset_id:
            row = conn.execute(
                """
                SELECT a.asset_id, a.playback_locator, a.source_locator
                FROM episode_assets ea JOIN assets a ON a.asset_id = ea.asset_id
                WHERE ea.episode_id = ? AND ea.asset_id = ?
                """,
                (episode_id, asset_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT a.asset_id, a.playback_locator, a.source_locator
                FROM episode_assets ea JOIN assets a ON a.asset_id = ea.asset_id
                WHERE ea.episode_id = ? ORDER BY ea.edition_id, a.asset_id LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
    if row is None:
        raise KeyError((episode_id, asset_id))
    return dict(row)


@router.post("/play")
def play(request: PlayRequest):
    try:
        asset = _default_asset(request.episode_id, request.asset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="剧集或 Asset 不存在") from exc
    return {
        "session_id": f"v4:{request.episode_id}:{asset['asset_id']}",
        "status": "ready",
        "work_id": request.work_id,
        "episode_id": request.episode_id,
        "asset_id": asset["asset_id"],
        "playback_locator": asset["playback_locator"] or asset["source_locator"],
    }


@router.post("/stop")
def stop():
    return {"status": "stopped"}


@router.get("/status")
def status():
    return {"status": "idle", "session": None}


@router.post("/progress")
def report_progress(request: ProgressRequest):
    try:
        asset = _default_asset(request.episode_id, request.asset_id)
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
    del limit
    return list_progress(work_id)
