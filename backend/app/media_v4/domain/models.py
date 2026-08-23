"""V4 的不可变来源证据和值对象。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """一次扫描观察到的来源条目，保存后不允许原位改写。"""

    evidence_id: str
    scan_id: str
    root_id: str
    source_key: str
    relative_path: str
    entry_kind: str
    provider: str = ""
    size: int | None = None
    mtime: float | None = None
    fingerprint: str = ""
    raw_file_id: str = ""
    ingest_method: str = ""
    source_route_id: str = ""
    source_locator: str = ""
    playback_locator: str = ""
    tmdb_hint_id: str = ""
    tmdb_hint_type: str = ""
    import_family: str = "anime"
    target_filename: str = ""
    observed_at: str = ""
    presence_state: str = "present"


@dataclass(frozen=True, slots=True)
class ParsedFacts:
    """从 SourceEvidence 提取的候选事实，不包含 Work 身份或执行动作。"""

    parsed_fact_id: str
    evidence_id: str
    parser_version: str
    resource_type: str = "video"
    media_type: str = ""
    group_type: str = ""
    work_title: str = ""
    original_title: str = ""
    series_group: str = ""
    card_type: str = ""
    relation_type: str = ""
    show_type: str = ""
    title_candidates: tuple[str, ...] = field(default_factory=tuple)
    year_candidate: int | None = None
    season_token_raw: str = ""
    episode_token_raw: str = ""
    season_candidate: int | None = None
    episode_candidate: int | None = None
    absolute_episode_candidate: int | None = None
    special_candidate: bool = False
    episode_range: tuple[int, int] | None = None
    special_number: int | None = None
    tmdb_hint_id: int | None = None
    tmdb_hint_type: str = ""
    release_group: str = ""
    edition_tags: tuple[str, ...] = field(default_factory=tuple)
    quality_tags: tuple[str, ...] = field(default_factory=tuple)
    confidence: str = "medium"
    needs_review: bool = False
    is_importable: bool = True
    is_auxiliary: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ResolvedWork:
    """Resolver 阶段的作品候选；持久化时再分配真正的 work_id。"""

    work_key: str
    preferred_title: str
    year: int | None
    media_type: str
    source_evidence_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ResolvedEpisode:
    """Resolver 阶段的逻辑剧集及其全部物理 Asset 证据。"""

    episode_key: str
    work_key: str
    local_season_number: int | None
    local_episode_number: int | None
    absolute_episode_number: int | None
    season_kind: str
    episode_kind: str
    special_number: int | None
    edition_key: str
    asset_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    provider_season_number: int | None = None
    provider_episode_number: int | None = None


@dataclass(frozen=True, slots=True)
class ResolutionIssue:
    code: str
    evidence_id: str
    message: str


@dataclass(frozen=True, slots=True)
class ResolvedMediaGraph:
    works: tuple[ResolvedWork, ...] = field(default_factory=tuple)
    episodes: tuple[ResolvedEpisode, ...] = field(default_factory=tuple)
    issues: tuple[ResolutionIssue, ...] = field(default_factory=tuple)
