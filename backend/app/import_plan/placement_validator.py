"""ImportPlan 归位质检。

识别层负责“猜出”媒体结构；本模块负责在确认/镜像前做保守校验。
原则：宁可标记 needs_review，也不允许明显冲突的条目静默进入镜像生成。
"""

import re
from dataclasses import dataclass

from app.import_plan.models import ImportPlan, ImportPlanItem


@dataclass
class PlacementIssue:
    """单个归位问题。"""

    item_id: str
    level: str
    code: str
    message: str


_VALID_GROUP_TYPES = {"season", "special", "movie", "auxiliary", "ignored", "sps", "op_ed"}

_SPECIAL_CONTEXT_PATTERNS = [
    re.compile(r"\[(?:OVA|OAD|SP\d*|SPECIAL|PV\d*|CM\d*|MENU\d*)\]", re.IGNORECASE),
    re.compile(r"(?:^|[/\\._\s-])(?:OVA|OAD|SPECIAL|PV|CM|MENU)(?:\d+)?(?:$|[/\\._\s([【-])", re.IGNORECASE),
    re.compile(r"番外|特典|小剧场|短篇"),
    re.compile(r"\d+\.5"),
    re.compile(r"\[\d+\s*β\]", re.IGNORECASE),
]

_OP_ED_CONTEXT_PATTERNS = [
    re.compile(r"NCOP|NCED", re.IGNORECASE),
    re.compile(r"NON[-\s]?CREDIT\s+(?:OP|ED)", re.IGNORECASE),
    re.compile(r"无字幕\s*(?:OP|ED)", re.IGNORECASE),
    re.compile(r"\[(?:OP|ED)\d*\]", re.IGNORECASE),
    re.compile(r"(?:^|[/\\._\s-])(?:OP|ED)\d+(?:$|[/\\._\s([【-])", re.IGNORECASE),
]

_MOVIE_CONTEXT_PATTERNS = [
    re.compile(r"动画电影|剧场版|映画|总集篇|MOVIE|EXTRA\s+EDITION", re.IGNORECASE),
]


def validate_import_plan_placement(
    plan: ImportPlan,
    mutate: bool = True,
) -> list[PlacementIssue]:
    """校验 ImportPlan 中视频条目的归位结果。

    参数:
        plan: 已经过媒体识别的 ImportPlan
        mutate: True 时把高风险条目标记为 needs_review

    返回:
        PlacementIssue 列表
    """
    issues: list[PlacementIssue] = []

    for item in plan.items:
        if item.resource_type != "video" or item.action != "generate_strm":
            continue

        _check_required_fields(item, issues)
        _check_group_shape(item, issues)
        _check_context_conflict(item, issues)

    if mutate:
        issue_map = {}
        for issue in issues:
            issue_map.setdefault(issue.item_id, []).append(issue)
        for item in plan.items:
            item_issues = issue_map.get(item.id, [])
            if not item_issues:
                continue
            item.needs_review = True
            for issue in item_issues:
                warning = f"识别结果需要确认：{issue.message}"
                if warning not in item.warnings:
                    item.warnings.append(warning)

    return issues


def _check_required_fields(item: ImportPlanItem, issues: list[PlacementIssue]) -> None:
    if not item.work_title:
        _add(issues, item, "error", "missing_work_title", "没有识别出作品名")
    if item.group_type not in _VALID_GROUP_TYPES:
        _add(issues, item, "error", "invalid_group_type", "还没判断出这是正片、特别篇还是电影")


def _check_group_shape(item: ImportPlanItem, issues: list[PlacementIssue]) -> None:
    if item.group_type == "season":
        if item.media_type and item.media_type != "tv":
            _add(issues, item, "error", "season_media_type", "正片条目被识别成了电影类型")
        if item.season_number is None or item.episode_number is None:
            _add(issues, item, "error", "season_missing_number", "正片条目缺少季数或集数")
        elif item.season_number <= 0 or item.episode_number <= 0:
            _add(issues, item, "error", "season_invalid_number", "正片的季数和集数必须大于 0")

    if item.group_type in {"special", "sps", "op_ed", "ignored"} and item.episode_number is not None:
        _add(issues, item, "error", "special_has_episode", "特别篇或忽略项不应该占用正片集数")

    if item.group_type == "movie":
        if item.media_type and item.media_type != "movie":
            _add(issues, item, "error", "movie_media_type", "电影条目被识别成了剧集类型")
        if item.season_number is not None or item.episode_number is not None:
            _add(issues, item, "error", "movie_has_episode", "电影不应该带季数或集数")


def _check_context_conflict(item: ImportPlanItem, issues: list[PlacementIssue]) -> None:
    text = item.relative_path.replace("\\", "/")
    filename = text.split("/")[-1] if text else ""
    focused_text = _focused_context_text(item)

    if item.group_type == "season":
        if _matches_any(_OP_ED_CONTEXT_PATTERNS, text):
            _add(issues, item, "error", "season_conflicts_op_ed", "文件名像 OP/ED 特典，但被当成了正片")
        if _matches_any(_SPECIAL_CONTEXT_PATTERNS, text):
            _add(issues, item, "error", "season_conflicts_special", "文件名像 SP/OVA/PV 或特殊篇，但被当成了正片")
        if _matches_any(_MOVIE_CONTEXT_PATTERNS, focused_text):
            _add(issues, item, "error", "season_conflicts_movie", "文件名像电影、剧场版或总集篇，但被当成了正片")

    if item.group_type == "special" and _matches_any(_OP_ED_CONTEXT_PATTERNS, text):
        _add(issues, item, "error", "special_conflicts_op_ed", "文件名像 OP/ED 特典，但被当成了特别篇")

    if item.group_type == "movie":
        if _matches_any(_OP_ED_CONTEXT_PATTERNS, text):
            _add(issues, item, "error", "movie_conflicts_op_ed", "文件名像 OP/ED 特典，但被当成了电影")
        if _matches_any(_SPECIAL_CONTEXT_PATTERNS, filename):
            _add(issues, item, "warning", "movie_conflicts_special", "文件名像 SP/OVA/PV，作为电影前需要确认")


def _focused_context_text(item: ImportPlanItem) -> str:
    """返回用于判断 movie 冲突的聚焦上下文。

    系列容器名可能包含 “+剧场版+外传” 这种范围声明，不能据此判定
    其下所有正片都和 movie 冲突；只检查顶层动画电影分类、子作品目录和文件名。
    """
    parts = item.relative_path.replace("\\", "/").split("/")
    if not parts:
        return ""

    if item.source != "local":
        top = parts[0]
        if any(kw in top for kw in ("动画电影", "电影")):
            return item.relative_path
        if len(parts) >= 3:
            return "/".join(parts[2:])
        return parts[-1]

    # local 没有固定分类层。若第一层像合集容器，跳过它，只看子作品和文件名。
    if len(parts) >= 3 and parts[0].startswith("[") and "]" in parts[0]:
        return "/".join(parts[1:])
    return "/".join(parts)


def _matches_any(patterns: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _add(
    issues: list[PlacementIssue],
    item: ImportPlanItem,
    level: str,
    code: str,
    message: str,
) -> None:
    issues.append(PlacementIssue(item_id=item.id, level=level, code=code, message=message))
