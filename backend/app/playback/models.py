# -*- coding: utf-8 -*-
"""播放系统数据结构"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class PlaybackRequest:
    """播放请求"""
    work_id: str = ""
    episode_id: str = ""
    strm_path: str = ""
    start_position: float = 0.0


@dataclass
class PlaybackSession:
    """播放会话"""
    session_id: str = ""
    work_id: str = ""
    episode_id: str = ""
    strm_path: str = ""
    real_path: str = ""
    ipc_server: str = ""
    position: float = 0.0
    duration: float = 0.0
    pid: int = 0
    status: str = "starting"  # starting / playing / stopped / exited / failed
    started_at: str = ""
    ended_at: str = ""
    exit_code: Optional[int] = None
    error: str = ""


@dataclass
class PlaybackHistoryItem:
    """播放历史条目"""
    history_id: str = ""
    work_id: str = ""
    work_title: str = ""
    episode_id: str = ""
    episode_title: str = ""
    source: str = ""
    media_type: str = ""
    group_type: str = ""
    season_number: int = 0
    episode_number: int = 0
    strm_path: str = ""
    poster_path: str = ""
    played_at: str = ""
