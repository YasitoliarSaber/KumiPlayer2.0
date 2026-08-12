# -*- coding: utf-8 -*-
"""ScrapeMap / ScrapeTarget / ScrapeCandidate 数据模型"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScrapeTarget:
    """可刮削目标（按季/电影聚合）"""

    scrape_target_id: str = ""
    source: str = ""
    import_plan_id: str = ""
    work_id: str = ""
    card_type: str = ""           # main_series / standalone
    media_type: str = ""          # tv / movie
    show_type: str = ""           # anime_series / anime_movie / live_series / live_movie
    group_type: str = ""          # season / movie
    series_group: str = ""
    local_title: str = ""
    original_title: str = ""
    source_subwork_dir: str = ""
    local_year: Optional[int] = None
    local_season_number: Optional[int] = None
    scrape_title: str = ""
    scrape_year: Optional[int] = None
    scrape_type: str = ""         # tv / movie
    tmdb_hint_id: Optional[int] = None
    tmdb_hint_type: str = ""      # tv / movie
    target_dir: str = ""
    target_nfo_path: str = ""
    target_poster_path: str = ""
    target_fanart_path: str = ""
    target_clearlogo_path: str = ""
    item_ids: List[str] = field(default_factory=list)
    local_episode_count: int = 0
    needs_review: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class ScrapeCandidate:
    """TMDB 搜索候选"""

    candidate_id: str = ""
    scrape_target_id: str = ""
    provider: str = "tmdb"
    tmdb_id: int = 0
    tmdb_type: str = ""           # tv / movie
    title: str = ""
    original_title: str = ""
    year: Optional[int] = None
    overview: str = ""
    poster_path: str = ""
    popularity: float = 0.0
    vote_average: float = 0.0
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScrapeMapItem:
    """单个作品的刮削映射（扩展版）"""

    scrape_target_id: str = ""
    work_id: str = ""
    source: str = ""
    import_plan_id: str = ""
    card_type: str = ""
    media_type: str = ""
    series_group: str = ""
    local_title: str = ""
    original_title: str = ""
    source_subwork_dir: str = ""
    local_year: Optional[int] = None
    local_season_number: Optional[int] = None
    scrape_title: str = ""
    scrape_year: Optional[int] = None
    search_query: str = ""
    tmdb_id: Optional[int] = None
    tmdb_type: str = ""
    tmdb_season_number: Optional[int] = None
    relation_to_series: str = ""
    selected_by: str = ""
    confidence: str = ""
    identity_evidence: Dict[str, Any] = field(default_factory=dict)
    scraped_at: str = ""
    nfo_path: str = ""
    poster_path: str = ""
    fanart_path: str = ""
    clearlogo_path: str = ""


@dataclass
class ScrapeMap:
    """刮削映射集合"""

    version: int = 1
    items: List[ScrapeMapItem] = field(default_factory=list)
