# -*- coding: utf-8 -*-
"""刮削候选 SQLite 缓存"""

import json
from datetime import datetime, timezone, timedelta
from typing import List

from app.db.database import close_connection, get_connection
from app.scrape.models import ScrapeCandidate


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def save_candidates(candidates: List[ScrapeCandidate]) -> None:
    """保存候选缓存。"""
    if not candidates:
        return
    conn = get_connection()
    now = _now_iso()
    for c in candidates:
        cache_id = f"{c.scrape_target_id}:{c.provider}:{c.tmdb_type}:{c.tmdb_id}"
        conn.execute("""
            INSERT OR REPLACE INTO scrape_candidate_cache
            (cache_id, scrape_target_id, provider, tmdb_id, tmdb_type, title,
             original_title, year, overview, poster_path, popularity,
             vote_average, score, reasons, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cache_id, c.scrape_target_id, c.provider, c.tmdb_id, c.tmdb_type,
            c.title, c.original_title, c.year, c.overview, c.poster_path,
            c.popularity, c.vote_average, c.score,
            json.dumps(c.reasons, ensure_ascii=False), now,
        ))
    conn.commit()
    close_connection()


def list_candidates(scrape_target_id: str) -> list:
    """按 target 读取候选缓存。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM scrape_candidate_cache WHERE scrape_target_id = ? ORDER BY score DESC, popularity DESC",
        (scrape_target_id,),
    ).fetchall()
    items = [dict(row) for row in rows]
    close_connection()
    return items


def list_candidates_by_tmdb_identity(tmdb_id: int, tmdb_type: str) -> list:
    """读取同一 TMDB 身份最近保存的候选，供明确 ID 提示离线复用。"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM scrape_candidate_cache
        WHERE tmdb_id = ? AND tmdb_type = ?
        ORDER BY score DESC, cached_at DESC, popularity DESC
        """,
        (tmdb_id, tmdb_type),
    ).fetchall()
    items = [dict(row) for row in rows]
    close_connection()
    return items
