# -*- coding: utf-8 -*-
"""LibraryIndex 相关数据模型

可重建的前端索引，不是主真相。
从 mirror + NFO + import_plan + scrape_map 重建。
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EpisodeIndex:
    """剧集索引条目"""

    episode_id: str = ""
    work_id: str = ""
    source: str = ""  # 每集可来自不同的网盘或本地目录（兼容字段）
    provider_id: str = ""  # 内容提供商
    season_number: int = 0
    episode_number: int = 0
    title: str = ""
    plot: str = ""
    runtime: int = 0
    group_type: str = ""  # season / special / movie
    kind: str = ""  # main / ova / sp / ncop / nced / pv / extra
    strm_path: str = ""
    nfo_path: str = ""
    thumb_path: str = ""
    availability: str = "available"
    metadata_pending: bool = False


@dataclass
class SeasonIndex:
    """季/分组索引条目"""

    season_id: str = ""
    work_id: str = ""
    season_number: int = 0
    group_type: str = ""  # season / special
    label: str = ""  # 显示标签，如 "第1季"、"特别篇"
    episode_count: int = 0
    scrape_target_id: str = ""
    scrape_title: str = ""
    scrape_year: Optional[int] = None
    tmdb_id: Optional[int] = None
    tmdb_type: str = ""
    tmdb_season_number: Optional[int] = None
    nfo_path: str = ""
    poster_path: str = ""
    fanart_path: str = ""
    clearlogo_path: str = ""
    plot: str = ""
    rating: float = 0.0
    scraped: bool = False


@dataclass
class RelatedWork:
    """关联作品"""

    work_id: str = ""
    title: str = ""
    year: Optional[int] = None
    card_type: str = ""  # main_series / standalone
    relation_type: str = ""  # main / movie / recap / spin_off / related
    poster_path: str = ""
    fanart_path: str = ""
    show_type: str = ""


@dataclass
class WorkIndex:
    """作品索引条目"""

    work_id: str = ""
    title: str = ""
    original_title: str = ""
    year: Optional[int] = None
    rating: float = 0.0
    plot: str = ""
    genres: List[str] = field(default_factory=list)
    studios: List[str] = field(default_factory=list)
    show_type: str = ""  # anime_series / anime_movie / live_series / live_movie
    media_type: str = ""  # tv / movie
    source: str = ""  # pan115 / baidu / local（兼容字段；真实提供商见 provider_id）
    sources: List[str] = field(default_factory=list)  # 跨来源合并卡片包含的全部来源
    provider_id: str = ""  # 内容提供商：pan115/baidu/quark/other/local
    ingest_method: str = ""  # openlist_api / directory_tree / local_scan
    source_route_id: str = ""  # 使用的 OpenList 提供商路由，可为空
    import_scope: str = ""  # seasonal 表示来自新番/追更目录树
    card_type: str = ""  # main_series / standalone
    poster_path: str = ""
    fanart_path: str = ""
    clearlogo_path: str = ""
    dir_path: str = ""  # 镜像中的作品目录路径
    source_locations: dict = field(default_factory=dict)  # 每个实际来源用于安全文件定位的代表剧集
    source_episode_counts: dict = field(default_factory=dict)  # 合卡去重前每个来源的剧集贡献数
    seasons: List[SeasonIndex] = field(default_factory=list)
    episodes: List[EpisodeIndex] = field(default_factory=list)
    related_works: List[RelatedWork] = field(default_factory=list)
    cast: List[dict] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    last_played: Optional[str] = None
    tracking: Optional[dict] = None
    metadata_state: str = "ready"
    certification: str = ""
    certification_country: str = ""
    artwork_provenance: dict = field(default_factory=dict)


@dataclass
class LibraryIndex:
    """媒体库索引（可重建，不是主真相）"""

    version: int = 2
    works: List[WorkIndex] = field(default_factory=list)
    source_summary: dict = field(default_factory=dict)
    generated_at: str = ""
