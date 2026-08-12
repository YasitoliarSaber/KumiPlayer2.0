"""导入预览数据结构

ImportPreview / PreviewIssue / PreviewGroup 用于展示导入计划的预览信息。
"""

from dataclasses import dataclass, field


@dataclass
class PreviewIssue:
    """预览中发现的问题"""

    code: str = ""       # issue code
    level: str = ""      # info / warning / error
    message: str = ""
    item_ids: list[str] = field(default_factory=list)


@dataclass
class PreviewGroup:
    """预览分组（按 work_id + card_type + group_type + season_number）"""

    work_id: str = ""
    work_title: str = ""
    year: int | None = None
    card_type: str = ""
    media_type: str = ""
    show_type: str = ""
    series_group: str = ""
    group_type: str = ""
    season_number: int | None = None
    item_count: int = 0
    item_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportPreview:
    """导入预览"""

    plan_id: str = ""
    source: str = ""
    status: str = ""
    import_scope: str = ""  # seasonal 表示新番（追更中）
    summary: dict = field(default_factory=dict)
    issues: list[PreviewIssue] = field(default_factory=list)
    groups: list[PreviewGroup] = field(default_factory=list)
    items: list = field(default_factory=list)  # List[ImportPlanItem]
