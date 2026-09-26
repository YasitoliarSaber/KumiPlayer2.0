"""V4 的不可变来源证据和值对象。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.media_v4.domain.identity import (
    CLASSIFICATION_UNKNOWN,
    CONTENT_CLASS_UNKNOWN,
    DecisionTrace,
    NumberingEvidence,
)


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
    episode_title: str = ""
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
    # C-002 / C-004：单文件可见词法事实与决定，不承载批量推断。
    content_class: str = CONTENT_CLASS_UNKNOWN
    classification_state: str = CLASSIFICATION_UNKNOWN
    decision_trace: tuple[DecisionTrace, ...] = field(default_factory=tuple)
    numbering: NumberingEvidence = field(default_factory=NumberingEvidence)


@dataclass(frozen=True, slots=True)
class ResolvedWork:
    """Resolver 阶段的作品候选；持久化时再分配真正的 work_id。"""

    work_key: str
    preferred_title: str
    year: int | None
    media_type: str
    source_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    card_type: str = ""
    show_type: str = ""
    series_group: str = ""
    relation_type: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedWorkRelation:
    """同一媒体图内的作品关系（父系列/独立关联作品/电影/重制版等）。"""

    parent_work_key: str
    child_work_key: str
    relation_type: str



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
    display_title: str
    edition_key: str
    asset_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    provider_season_number: int | None = None
    provider_episode_number: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedWorkAsset:
    """电影等 Work 直属的内容版本及其物理 Asset。"""

    work_key: str
    edition_key: str
    asset_evidence_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ResolutionIssue:
    code: str
    evidence_id: str
    message: str
    # 普通歧义只作展示，不阻断整批导入；安全/结构错误仍走服务端错误路径。
    severity: str = "warning"


@dataclass(frozen=True, slots=True)
class ResolvedMediaGraph:
    works: tuple[ResolvedWork, ...] = field(default_factory=tuple)
    episodes: tuple[ResolvedEpisode, ...] = field(default_factory=tuple)
    work_assets: tuple[ResolvedWorkAsset, ...] = field(default_factory=tuple)
    relations: tuple[ResolvedWorkRelation, ...] = field(default_factory=tuple)
    issues: tuple[ResolutionIssue, ...] = field(default_factory=tuple)
