# -*- coding: utf-8 -*-
"""播放历史 JSON 持久化"""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK

from app.playback.models import PlaybackHistoryItem


def _get_history_path() -> Path:
    from app.core.paths import get_data_dir
    history_dir = get_data_dir() / "playback"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir / "history.json"


def _make_history_id(work_id: str, episode_id: str, played_at: str) -> str:
    content = f"{work_id}:{episode_id}:{played_at}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


def save_history(item: PlaybackHistoryItem) -> None:
    """追加一条播放历史"""
    with DATA_WRITE_LOCK:
        items = load_history()
        items.insert(0, item)
        from dataclasses import asdict
        write_json_atomic(_get_history_path(), [asdict(i) for i in items])
    _save_history_to_db(item)


def load_history() -> List[PlaybackHistoryItem]:
    """加载全部播放历史（按 played_at 倒序）"""
    path = _get_history_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = [PlaybackHistoryItem(**item) for item in data]
        return sorted(items, key=lambda item: item.played_at, reverse=True)
    except (json.JSONDecodeError, KeyError):
        return []


def get_history(
    limit: int = 50,
    work_id: Optional[str] = None,
) -> List[PlaybackHistoryItem]:
    """获取播放历史（支持 limit 和 work_id 筛选）"""
    items = load_history()
    if work_id:
        items = [i for i in items if i.work_id == work_id]
    return items[:limit]


def get_continue_item(work_id: str) -> Optional[PlaybackHistoryItem]:
    """获取指定作品最近播放条目（继续播放）"""
    items = load_history()
    for item in items:
        if item.work_id == work_id:
            return item
    return None


def build_history_item(
    work_id: str,
    work_title: str,
    episode_id: str,
    episode_title: str,
    source: str,
    media_type: str,
    group_type: str,
    season_number: int,
    episode_number: int,
    strm_path: str,
    poster_path: str = "",
) -> PlaybackHistoryItem:
    """构建播放历史条目"""
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    return PlaybackHistoryItem(
        history_id=_make_history_id(work_id, episode_id, now),
        work_id=work_id,
        work_title=work_title,
        episode_id=episode_id,
        episode_title=episode_title,
        source=source,
        media_type=media_type,
        group_type=group_type,
        season_number=season_number,
        episode_number=episode_number,
        strm_path=strm_path,
        poster_path=poster_path,
        played_at=now,
    )


def _save_history_to_db(item: PlaybackHistoryItem) -> None:
    """播放历史双写 SQLite；JSON 仍保留为当前主流程。"""
    try:
        from dataclasses import asdict
        from app.db.database import close_connection, init_db
        from app.db.history import save_playback_history
        init_db()
        save_playback_history(asdict(item))
    except Exception:
        pass
    finally:
        try:
            from app.db.database import close_connection
            close_connection()
        except Exception:
            pass
