"""ImportPlan / ImportPlanItem 数据模型

ImportPlan 是镜像生成的唯一执行依据。
ImportPlanItem 承载媒体识别结果。
"""

from dataclasses import dataclass, field


@dataclass
class ImportPlanItem:
    """导入计划中的单个条目"""

    id: str = ""
    plan_id: str = ""
    raw_file_id: str = ""
    source: str = ""
    provider_id: str = ""  # 内容提供商（识别层优先使用；兼容旧 source）
    ingest_method: str = ""  # openlist_api / directory_tree / local_scan
    source_route_id: str = ""  # 使用的 OpenList 提供商路由，可为空
    relative_path: str = ""
    real_path: str = ""
    source_size: int = 0
    source_mtime: float = 0.0
    source_fingerprint: str = ""
    availability: str = "available"  # available / missing / source_unavailable

    # 资源类型与动作
    resource_type: str = "other"  # video / subtitle / nfo / image / font / archive / other
    action: str = "ignore"  # generate_strm / ignore / attach_only

    # 作品信息
    work_id: str = ""  # 稳定的作品标识
    canonical_work_id: str = ""  # 追更/人工绑定后的前台稳定身份
    work_title: str = ""
    original_title: str = ""
    year: int | None = None
    media_type: str = ""  # tv / movie
    show_type: str = ""  # anime_series / anime_movie / live_series / live_movie
    tmdb_hint_id: int | None = None
    tmdb_hint_type: str = ""  # tv / movie
    import_family: str = ""  # anime / live，来自导入入口的大类选择

    # 系列关系
    series_group: str = ""  # 系列组标识
    card_type: str = ""  # main_series / standalone
    belongs_to_series: str = ""  # 所属主系列 work_id
    relation_type: str = ""  # main / movie / recap / spin_off / related

    # 分组
    group_type: str = ""  # season / special / movie / ignored
    season_number: int | None = None
    episode_number: int | None = None
    special_number: int | None = None
    title: str = ""  # 剧集标题

    # 目标路径
    target_dir: str = ""
    target_filename: str = ""
    target_strm_path: str = ""

    # 置信度
    confidence: str = "medium"  # high / medium / low
    needs_review: bool = False
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    user_override_id: str | None = None


@dataclass
class ImportPlan:
    """导入计划"""

    plan_id: str = ""
    source: str = ""
    provider_id: str = ""  # 内容提供商：pan115/baidu/quark/other/local
    ingest_method: str = ""  # openlist_api / directory_tree / local_scan
    source_route_id: str = ""  # 使用的 OpenList 提供商路由，可为空
    source_snapshot_id: str = ""
    # 选中目录名（OpenList）：无作品容器层时作为系列名候选（S1/S2 归同一系列）
    root_container: str = ""
    import_family: str = ""  # anime / live，来自 RawSnapshot
    import_scope: str = ""  # seasonal 表示导入完成后注册追更作品
    created_at: str = ""
    updated_at: str = ""
    status: str = "draft"  # draft / confirmed / executed
    items: list[ImportPlanItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
