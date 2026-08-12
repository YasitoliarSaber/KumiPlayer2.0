# -*- coding: utf-8 -*-
"""播放 API 端点

POST /api/playback/play
POST /api/playback/stop
GET  /api/playback/status
GET  /api/playback/history
GET  /api/playback/continue/{work_id}
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.playback.history import get_continue_item, get_history
from app.playback.models import PlaybackRequest
from app.playback.progress import list_progress, mark_episode_completed, save_progress
from app.playback.service import get_playback_manager

router = APIRouter(prefix="/api/playback", tags=["playback"])


# ============================================================
# 请求模型
# ============================================================

class PlayRequest(BaseModel):
    work_id: str
    episode_id: str = ""
    strm_path: str = ""


class ProgressRequest(BaseModel):
    work_id: str
    episode_id: str
    position: float = 0
    duration: float = 0


class ProgressMarkRequest(BaseModel):
    work_id: str
    episode_id: str
    completed: bool


# ============================================================
# 端点
# ============================================================

@router.post("/play")
def play(req: PlayRequest):
    """播放指定剧集"""
    manager = get_playback_manager()
    request = PlaybackRequest(
        work_id=req.work_id,
        episode_id=req.episode_id,
        strm_path=req.strm_path,
    )
    try:
        session = manager.play(request)
    except ValueError as e:
        # 参数校验失败
        msg = str(e)
        if "rescan" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "不属于" in msg:
            raise HTTPException(status_code=400, detail=msg)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "session_id": session.session_id,
        "status": session.status,
        "pid": session.pid,
        "work_id": session.work_id,
        "episode_id": session.episode_id,
        "strm_path": session.strm_path,
    }


@router.post("/stop")
def stop():
    """停止当前播放"""
    manager = get_playback_manager()
    return manager.stop()


@router.get("/status")
def status():
    """获取当前播放状态"""
    manager = get_playback_manager()
    return manager.status()


@router.get("/history")
def history(limit: int = 50, work_id: Optional[str] = None):
    """获取播放历史"""
    items = get_history(limit=limit, work_id=work_id)
    return {
        "items": [
            {
                "history_id": i.history_id,
                "work_id": i.work_id,
                "work_title": i.work_title,
                "episode_id": i.episode_id,
                "episode_title": i.episode_title,
                "source": i.source,
                "media_type": i.media_type,
                "group_type": i.group_type,
                "season_number": i.season_number,
                "episode_number": i.episode_number,
                "strm_path": i.strm_path,
                "poster_path": i.poster_path,
                "played_at": i.played_at,
            }
            for i in items
        ],
        "total": len(items),
    }


@router.get("/progress")
def progress(work_id: Optional[str] = None):
    """获取播放进度。completed=true 表示达到看完阈值。"""
    return {"items": [_progress_to_dict(item) for item in list_progress(work_id)]}


@router.post("/progress")
def report_progress(req: ProgressRequest):
    """上报播放进度，达到阈值后只记录本地看完状态。"""
    if not req.work_id or not req.episode_id:
        raise HTTPException(status_code=400, detail="work_id 和 episode_id 必填")
    if req.position < 0 or req.duration < 0:
        raise HTTPException(status_code=400, detail="position/duration 不能为负数")
    return _progress_to_dict(save_progress(req.work_id, req.episode_id, req.position, req.duration))


@router.post("/progress/mark")
def mark_progress(req: ProgressMarkRequest):
    """手动标记单集已看完/未看。"""
    if not req.work_id or not req.episode_id:
        raise HTTPException(status_code=400, detail="work_id 和 episode_id 必填")
    return _progress_to_dict(mark_episode_completed(req.work_id, req.episode_id, req.completed))


@router.get("/continue/{work_id}")
def continue_play(work_id: str):
    """继续播放指定作品"""
    item = get_continue_item(work_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"没有 {work_id} 的播放历史")
    return {
        "history_id": item.history_id,
        "work_id": item.work_id,
        "work_title": item.work_title,
        "episode_id": item.episode_id,
        "episode_title": item.episode_title,
        "strm_path": item.strm_path,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "played_at": item.played_at,
    }


def _progress_to_dict(item):
    return {
        "work_id": item.work_id,
        "episode_id": item.episode_id,
        "position": item.position,
        "duration": item.duration,
        "ratio": item.ratio,
        "completed": item.completed,
        "updated_at": item.updated_at,
        "bangumi_synced": item.bangumi_synced,
        "bangumi_error": item.bangumi_error,
        "manually_unwatched": item.manually_unwatched,
    }
