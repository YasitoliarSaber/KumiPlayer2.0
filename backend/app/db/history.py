# -*- coding: utf-8 -*-
"""播放历史 SQLite 存储"""

from datetime import datetime, timezone, timedelta
from typing import List, Optional

from app.db.database import close_connection, get_connection


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def save_playback_history(item: dict) -> None:
    """保存播放历史"""
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO playback_history
        (history_id, work_id, work_title, episode_id, episode_title,
         source, media_type, group_type, season_number, episode_number,
         strm_path, poster_path, played_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item["history_id"],
        item["work_id"],
        item.get("work_title", ""),
        item.get("episode_id", ""),
        item.get("episode_title", ""),
        item.get("source", ""),
        item.get("media_type", ""),
        item.get("group_type", ""),
        item.get("season_number", 0),
        item.get("episode_number", 0),
        item.get("strm_path", ""),
        item.get("poster_path", ""),
        item.get("played_at", _now_iso()),
    ))
    conn.commit()
    close_connection()


def get_playback_history(
    limit: int = 50,
    work_id: Optional[str] = None,
) -> List[dict]:
    """获取播放历史"""
    conn = get_connection()

    if work_id:
        rows = conn.execute(
            "SELECT * FROM playback_history WHERE work_id = ? ORDER BY played_at DESC LIMIT ?",
            (work_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM playback_history ORDER BY played_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    items = [dict(row) for row in rows]
    close_connection()
    return items


def get_continue_item(work_id: str) -> Optional[dict]:
    """获取指定作品最近播放条目"""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM playback_history WHERE work_id = ? ORDER BY played_at DESC LIMIT 1",
        (work_id,),
    ).fetchone()
    item = dict(row) if row else None
    close_connection()
    return item
