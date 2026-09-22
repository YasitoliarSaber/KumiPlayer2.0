"""Playback state API backed by V4 Asset identities."""

from __future__ import annotations

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
        if episode_id == f"movie:{work_id}":
            row = conn.execute(
                """
                SELECT a.asset_id, a.playback_locator, a.source_locator
                FROM work_assets wa JOIN assets a ON a.asset_id = wa.asset_id
                WHERE wa.work_id = ? AND (? = '' OR a.asset_id = ?)
                ORDER BY wa.preference_rank, a.asset_id LIMIT 1
                """,
                (work_id, asset_id, asset_id),
            ).fetchone()
        elif asset_id:
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
        where = "WHERE work_id = ?"
        params.append(work_id)
    with get_database().connect() as conn:
        rows = conn.execute(
            "SELECT * FROM playback_history " + where + " ORDER BY played_at DESC, event_id LIMIT ?",
            (*params, bounded),
        ).fetchall()
        # 用户反馈："最近播放"只显示四个字，看不到**哪一集、进度、什么时候**。
        # 播放历史行只存事件与快照（`season_snapshot`/`episode_snapshot`），
        # 进度在 `playback_progress` 里，因此这里联表补全。取法是防御式的：
        # 该表结构变化或缺表都不影响历史列表本身。
        progress_by_key: dict[tuple[str, str, str], dict] = {}
        try:
            for row in conn.execute("SELECT * FROM playback_progress").fetchall():
                data = dict(row)
                key = (
                    str(data.get("work_id") or ""),
                    str(data.get("episode_id") or ""),
                    str(data.get("asset_id") or ""),
                )
                progress_by_key[key] = data
        except Exception:  # noqa: BLE001 - 历史列表不得因进度表问题整体失败
            progress_by_key = {}
    items: list[dict] = []
    for row in rows:
        item = dict(row)
        data = progress_by_key.get(
            (
                str(item.get("work_id") or ""),
                str(item.get("episode_id") or ""),
                str(item.get("asset_id") or ""),
            )
        )
        if data:
            item["position"] = data.get("position", 0)
            item["duration"] = data.get("duration", 0)
            item["completed"] = bool(data.get("completed", 0))
            item["updated_at"] = str(data.get("updated_at") or item.get("played_at") or "")
        else:
            item["updated_at"] = str(item.get("played_at") or "")
        items.append(item)
    return {"items": items, "limit": bounded}
