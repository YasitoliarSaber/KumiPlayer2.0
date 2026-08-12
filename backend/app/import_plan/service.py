"""导入计划服务函数

build_preview: 生成预览
patch_plan_item: 修正条目
confirm_plan: 确认计划
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.import_plan.overrides import (
    apply_patch_to_item,
    build_user_override,
    validate_patch,
)
from app.import_plan.placement_validator import validate_import_plan_placement
from app.import_plan.preview import ImportPreview, PreviewGroup, PreviewIssue
from app.import_plan.store import save_import_plan, save_user_override

DUPLICATE_EPISODE_WARNING_MARK = "同一季集存在多个来源文件"


# ============================================================
# build_preview
# ============================================================

def build_preview(plan: ImportPlan) -> ImportPreview:
    """生成导入预览

    检查低置信度、待确认、未分组、重复集数等问题。
    按 work_id + card_type + group_type + season_number 分组。
    """
    issues: list[PreviewIssue] = []
    groups_map: dict = {}  # group_key -> PreviewGroup

    # 统计
    total = len(plan.items)
    video_count = 0
    generate_strm_count = 0
    ignored_count = 0
    attach_only_count = 0
    low_confidence_count = 0
    needs_review_count = 0
    ungrouped_video_count = 0
    duplicate_episode_count = 0

    # 重复集数检测：(work_id, season_number, episode_number) -> item_ids
    season_episode_map: dict = defaultdict(list)

    for item in plan.items:
        # 统计资源类型
        if item.resource_type == "video":
            video_count += 1
        if item.action == "generate_strm":
            generate_strm_count += 1
        elif item.action == "ignore":
            ignored_count += 1
        elif item.action == "attach_only":
            attach_only_count += 1

        # 低置信度检查（只统计视频）
        if item.resource_type == "video" and item.action == "generate_strm":
            if item.confidence == "low" and not item.user_override_id:
                low_confidence_count += 1

        # 视频 + generate_strm 的额外检查
        if item.resource_type == "video" and item.action == "generate_strm":
            # 未分组视频检查
            if not item.group_type:
                ungrouped_video_count += 1

            # 重复 season episode 检查
            if (item.group_type == "season"
                    and item.season_number is not None
                    and item.episode_number is not None):
                identity = _duplicate_episode_identity(item)
                key = (identity, item.season_number, item.episode_number)
                season_episode_map[key].append(item.id)

        # 构建分组
        if item.resource_type == "video" and item.action == "generate_strm":
            group_key = (
                item.work_id,
                item.card_type,
                item.group_type,
                item.season_number,
            )
            if group_key not in groups_map:
                groups_map[group_key] = PreviewGroup(
                    work_id=item.work_id,
                    work_title=item.work_title,
                    year=item.year,
                    card_type=item.card_type,
                    media_type=item.media_type,
                    show_type=item.show_type,
                    series_group=item.series_group,
                    group_type=item.group_type,
                    season_number=item.season_number,
                )
            group = groups_map[group_key]
            group.item_count += 1
            group.item_ids.append(item.id)

    duplicate_item_ids = _duplicate_episode_item_ids(season_episode_map)
    needs_review_count = sum(
        1 for item in plan.items
        if item.resource_type == "video"
        and item.action == "generate_strm"
        and _has_active_needs_review(item, duplicate_item_ids)
    )

    # 已识别业务作品数：preview.groups 的一项只是“作品 + 卡片类型 + 分组类型 + 季度”，
    # 不能直接当“部作品”。这里按真实作品身份去重；同一作品多季只算一部。
    # 只统计 action=generate_strm 的正片类条目；OP/ED 与 PV/CM 等附属视频 action=ignore，
    # 不会进入统计。不得按 availability 过滤：路径暂不可达是路径验证边界，
    # 不能因此把已经识别出的作品数量归零。
    work_identities: set = set()
    for item in plan.items:
        if not (
            item.resource_type == "video"
            and item.action == "generate_strm"
            and item.group_type not in {"ignored", "op_ed"}
        ):
            continue
        if item.work_id:
            work_identities.add(("id", item.work_id))
        else:
            fallback_title = item.series_group or item.work_title or ""
            work_identities.add(("fallback", f"{item.card_type}|{fallback_title}|{item.year or ''}"))
    work_count = len(work_identities)

    # 生成 issues
    _check_low_confidence(plan.items, issues)
    _check_needs_review(plan.items, issues, duplicate_item_ids)
    _check_ungrouped_video(plan.items, issues)
    _check_duplicate_episode(season_episode_map, plan.items, issues)
    _check_invalid_items(plan.items, issues)

    # 统计 duplicate_episode
    for key, ids in season_episode_map.items():
        if len(ids) > 1:
            duplicate_episode_count += len(ids)

    summary = {
        "total_items": total,
        "video_count": video_count,
        "generate_strm_count": generate_strm_count,
        "ignored_count": ignored_count,
        "attach_only_count": attach_only_count,
        "low_confidence_count": low_confidence_count,
        "needs_review_count": needs_review_count,
        "ungrouped_video_count": ungrouped_video_count,
        "duplicate_episode_count": duplicate_episode_count,
        "work_count": work_count,
        "target_conflict_count": 0,  # 第一版不检查 target 冲突
    }

    return ImportPreview(
        plan_id=plan.plan_id,
        source=plan.source,
        status=plan.status,
        import_scope=plan.import_scope,
        summary=summary,
        issues=issues,
        groups=list(groups_map.values()),
        items=plan.items,
    )


def _check_low_confidence(items: list[ImportPlanItem], issues: list[PreviewIssue]) -> None:
    """检查低置信度视频"""
    ids = [
        item.id for item in items
        if item.resource_type == "video"
        and item.action == "generate_strm"
        and item.confidence == "low"
        and not item.user_override_id
    ]
    if ids:
        issues.append(PreviewIssue(
            code="low_confidence",
            level="warning",
            message=f"有 {len(ids)} 个视频识别不太确定，建议人工确认一下",
            item_ids=ids,
        ))


def _duplicate_episode_item_ids(season_episode_map: dict) -> set[str]:
    ids: set[str] = set()
    for current_ids in season_episode_map.values():
        if len(current_ids) > 1:
            ids.update(current_ids)
    return ids


def _duplicate_episode_identity(item: ImportPlanItem) -> str:
    if item.canonical_work_id:
        return item.canonical_work_id

    # series_group 是作品归并用的母系列名称，外传与主系列会共用它。
    # 重复剧集只能在同一具体作品内判断，否则两部作品各自的 S01E01
    # 会被错误送入人工确认队列。
    work_title = " ".join((item.work_title or "").split()).casefold()
    if work_title:
        return f"{work_title}|{_duplicate_episode_version_bucket(item)}"

    series = " ".join((item.series_group or "").split()).casefold()
    if series:
        return f"{series}|{_duplicate_episode_version_bucket(item)}"

    return item.work_id


def _duplicate_episode_version_bucket(item: ImportPlanItem) -> str:
    text = " ".join(
        value for value in [
            item.relative_path,
            item.work_title,
            item.original_title,
        ] if value
    )
    edition_keywords = (
        "新编撰版",
        "新編集版",
        "导演剪辑版",
        "导演版",
        "特别编辑版",
        "总集篇",
        "合集版",
        "remake",
        "re-edit",
        "reedit",
        "director",
    )
    lowered = text.casefold()
    for keyword in edition_keywords:
        if keyword.casefold() in lowered:
            return keyword.casefold()
    return str(item.year or "")


def _has_active_needs_review(item: ImportPlanItem, duplicate_item_ids: set[str]) -> bool:
    if item.id in duplicate_item_ids:
        return True
    if not item.needs_review:
        return False

    duplicate_warnings = [
        warning for warning in item.warnings
        if DUPLICATE_EPISODE_WARNING_MARK in warning
    ]
    non_duplicate_warnings = [
        warning for warning in item.warnings
        if DUPLICATE_EPISODE_WARNING_MARK not in warning
    ]
    if duplicate_warnings and not non_duplicate_warnings:
        return False
    return True


def _check_needs_review(
    items: list[ImportPlanItem],
    issues: list[PreviewIssue],
    duplicate_item_ids: set[str],
) -> None:
    """检查 needs_review 视频"""
    ids = [
        item.id for item in items
        if item.resource_type == "video"
        and item.action == "generate_strm"
        and _has_active_needs_review(item, duplicate_item_ids)
    ]
    if ids:
        issues.append(PreviewIssue(
            code="needs_review",
            level="warning",
            message=f"有 {len(ids)} 个视频需要人工确认",
            item_ids=ids,
        ))


def _check_ungrouped_video(items: list[ImportPlanItem], issues: list[PreviewIssue]) -> None:
    """检查未分组视频"""
    ids = [
        item.id for item in items
        if item.resource_type == "video"
        and item.action == "generate_strm"
        and not item.group_type
    ]
    if ids:
        issues.append(PreviewIssue(
            code="ungrouped_video",
            level="warning",
            message=f"有 {len(ids)} 个视频还没判断出是正片、特别篇还是电影，请人工确认",
            item_ids=ids,
        ))


def _check_duplicate_episode(
    season_episode_map: dict,
    items: list[ImportPlanItem],
    issues: list[PreviewIssue],
) -> None:
    """检查重复 season episode"""
    item_map = {item.id: item for item in items}
    for key, ids in season_episode_map.items():
        if len(ids) > 1:
            work_id, season, episode = key
            sample = item_map.get(ids[0])
            title = sample.work_title if sample and sample.work_title else "这个作品"
            issues.append(PreviewIssue(
                code="duplicate_episode",
                level="error",
                message=f"《{title}》第 {season} 季第 {episode} 集有 {len(ids)} 个重复视频，请保留正确版本或跳过重复项",
                item_ids=ids,
            ))


def _check_invalid_items(items: list[ImportPlanItem], issues: list[PreviewIssue]) -> None:
    """检查无效条目（视频但缺少必要字段）"""
    ids_no_work_title = []
    ids_no_group_type = []
    ids_season_incomplete = []
    for item in items:
        if item.resource_type == "video" and item.action == "generate_strm":
            # 缺少 work_title
            if not item.work_title:
                ids_no_work_title.append(item.id)
            # 缺少 group_type
            if not item.group_type:
                ids_no_group_type.append(item.id)
            # group_type=season 但缺 season_number 或 episode_number
            if item.group_type == "season":
                if item.season_number is None or item.episode_number is None:
                    ids_season_incomplete.append(item.id)
    if ids_no_work_title:
        issues.append(PreviewIssue(
            code="missing_work_title",
            level="error",
            message=f"有 {len(ids_no_work_title)} 个视频没识别出作品名，请人工填写",
            item_ids=ids_no_work_title,
        ))
    if ids_no_group_type:
        issues.append(PreviewIssue(
            code="missing_group_type",
            level="error",
            message=f"有 {len(ids_no_group_type)} 个视频还没判断出是正片、特别篇还是电影，请人工确认",
            item_ids=ids_no_group_type,
        ))
    if ids_season_incomplete:
        issues.append(PreviewIssue(
            code="season_incomplete",
            level="error",
            message=f"有 {len(ids_season_incomplete)} 个正片视频缺少季数或集数，请补全后再生成镜像",
            item_ids=ids_season_incomplete,
        ))


# ============================================================
# patch_plan_item
# ============================================================

def patch_plan_item(
    plan: ImportPlan,
    item_id: str,
    patch: dict,
) -> tuple[ImportPlanItem | None, ImportPreview | None, str | None]:
    """修正单个 ImportPlanItem

    返回:
        (更新后的 item, 更新后的 preview, 错误信息)
    """
    patch = _normalize_patch_defaults(patch)

    # 校验 patch
    is_valid, error_msg = validate_patch(patch)
    if not is_valid:
        return None, None, error_msg

    # 查找 item
    target_item = None
    for item in plan.items:
        if item.id == item_id:
            target_item = item
            break

    if target_item is None:
        return None, None, f"未找到 item_id={item_id}"

    # 应用 patch
    apply_patch_to_item(target_item, patch)
    _normalize_item_shape(target_item)

    # 人工修改分类时必须同步派生字段，并立即按和首次识别相同的规则复检。
    # 否则“季集”仍保留 movie/show_type 等旧值，前端看似保存成功，后续刮削却会
    # 因为结构不一致继续失败。
    validate_import_plan_placement(plan, mutate=True)

    # 保存 user_override
    override = build_user_override(plan.plan_id, item_id, plan.source, patch)
    save_user_override(override)

    # 更新 plan 的 updated_at
    plan.updated_at = datetime.now(timezone(timedelta(hours=8))).isoformat()

    # 重新生成 preview
    preview = build_preview(plan)

    return target_item, preview, None


def _normalize_patch_defaults(patch: dict) -> dict:
    normalized = dict(patch)

    if normalized.get("action") == "ignore":
        normalized["group_type"] = "ignored"
        normalized["needs_review"] = False
        normalized.setdefault("confidence", "medium")
        return normalized

    if normalized.get("group_type") == "season":
        normalized["media_type"] = "tv"
        normalized["card_type"] = "main_series"
        normalized["special_number"] = None
        if normalized.get("show_type") in {"anime_movie", "live_movie"}:
            normalized["show_type"] = (
                "live_series" if normalized["show_type"] == "live_movie" else "anime_series"
            )
        return normalized

    is_movie = (
        normalized.get("media_type") == "movie"
        or normalized.get("group_type") == "movie"
        or normalized.get("show_type") in {"anime_movie", "live_movie"}
    )
    if is_movie:
        normalized["media_type"] = "movie"
        normalized["group_type"] = "movie"
        normalized["card_type"] = "standalone"
        normalized["season_number"] = None
        normalized["episode_number"] = None
        normalized["special_number"] = None

    if normalized.get("show_type") in {"anime_series", "live_series"} and not normalized.get("media_type"):
        normalized["media_type"] = "tv"

    return normalized


def _normalize_item_shape(item: ImportPlanItem) -> None:
    """保持人工修正后的分类字段自洽。"""
    if item.action == "ignore":
        item.group_type = "ignored"
        item.needs_review = False
        return
    if item.group_type == "season":
        item.media_type = "tv"
        item.card_type = "main_series"
        item.special_number = None
        if item.show_type == "anime_movie":
            item.show_type = "anime_series"
        elif item.show_type == "live_movie":
            item.show_type = "live_series"
        return
    if item.group_type == "movie":
        item.media_type = "movie"
        item.card_type = "standalone"
        item.season_number = None
        item.episode_number = None
        item.special_number = None
        if item.show_type == "anime_series":
            item.show_type = "anime_movie"
        elif item.show_type == "live_series":
            item.show_type = "live_movie"


# ============================================================
# confirm_plan
# ============================================================

def confirm_plan(
    plan: ImportPlan, force: bool = False, update_latest: bool = True,
) -> tuple[ImportPlan | None, str | None]:
    """确认导入计划

    校验通过后将 status 从 draft 改为 confirmed。

    参数:
        force: 为 True 时遇到 error 级别问题也继续确认，仅用于兼容旧调用方。
               needs_review 本身属于可延后的 warning，不再阻塞确认。

    返回:
        (确认后的 plan, 错误信息)
    """
    # 校验 status
    if plan.status != "draft":
        return None, f"plan.status 必须是 draft，当前为 {plan.status}"

    # 校验至少有一个 generate_strm 视频
    has_video = any(
        item.resource_type == "video" and item.action == "generate_strm"
        for item in plan.items
    )
    if not has_video:
        return None, "至少需要一个 action=generate_strm 的视频条目"

    # 构建预览检查 issues
    preview = build_preview(plan)
    review_issues = [issue for issue in preview.issues if issue.code == "needs_review"]
    error_issues = [issue for issue in preview.issues if issue.level == "error"]

    if error_issues and not force:
        return None, f"存在 {len(error_issues)} 个 error 级别问题，请先处理"

    # 低置信度项目不再阻塞导入。它们会随计划继续进入镜像/刮削流程，
    # 后续由自动刮削写入 review queue，用户可在媒体管理中随时处理。
    if review_issues or (error_issues and force):
        from app.core.error_log import log_error
        for issue in review_issues:
            log_error(
                stage="import_plan",
                category="needs_review_deferred",
                message=f"导入计划确认后延后处理: {issue.message}",
                level="warning",
                source=plan.source,
                context={
                    "plan_id": plan.plan_id,
                    "issue_code": issue.code,
                    "item_ids": issue.item_ids,
                    "deferred": True,
                },
            )
        for issue in error_issues:
            log_error(
                stage="import_plan",
                category=issue.code or "import_error",
                message=f"导入计划使用 force 确认并跳过: {issue.message}",
                level="error",
                source=plan.source,
                context={
                    "plan_id": plan.plan_id,
                    "issue_code": issue.code,
                    "item_ids": issue.item_ids,
                },
            )

    # 确认
    plan.status = "confirmed"
    plan.updated_at = datetime.now(timezone(timedelta(hours=8))).isoformat()

    # 保存
    save_import_plan(plan, update_latest=update_latest)

    return plan, None
