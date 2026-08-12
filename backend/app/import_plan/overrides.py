"""用户修正结构

UserOverride 用于记录用户对 ImportPlanItem 的修正。
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.import_plan.models import ImportPlanItem

# 允许 patch 的字段白名单
PATCHABLE_FIELDS = {
    "action",
    "work_title",
    "original_title",
    "year",
    "media_type",
    "show_type",
    "series_group",
    "card_type",
    "belongs_to_series",
    "relation_type",
    "group_type",
    "season_number",
    "episode_number",
    "special_number",
    "title",
    "confidence",
    "needs_review",
    "warnings",
}

# 禁止 patch 的字段
FORBIDDEN_PATCH_FIELDS = {
    "id",
    "plan_id",
    "raw_file_id",
    "source",
    "relative_path",
    "real_path",
    "resource_type",
    "target_dir",
    "target_filename",
    "target_strm_path",
}

# 字段值约束
_ACTION_VALUES = {"generate_strm", "ignore", "attach_only"}
_CONFIDENCE_VALUES = {"high", "medium", "low"}
_MEDIA_TYPE_VALUES = {"", "tv", "movie"}
_SHOW_TYPE_VALUES = {"", "anime_series", "anime_movie", "live_series", "live_movie"}
_CARD_TYPE_VALUES = {"", "main_series", "standalone"}
_GROUP_TYPE_VALUES = {"", "season", "special", "movie", "ignored", "sps", "op_ed"}


@dataclass
class UserOverride:
    """单条用户修正记录"""

    override_id: str = ""
    plan_id: str = ""
    item_id: str = ""
    source: str = ""
    updated_at: str = ""
    patch: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserOverridesFile:
    """user_overrides.json 文件结构"""

    version: int = 1
    items: list[UserOverride] = field(default_factory=list)


def _make_override_id(plan_id: str, item_id: str, updated_at: str) -> str:
    """生成稳定的 override_id"""
    content = f"{plan_id}:{item_id}:{updated_at}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


def validate_patch(patch: dict) -> tuple[bool, str]:
    """校验 patch 字段是否合法

    返回:
        (is_valid, error_message)
    """
    for key in patch:
        if key in FORBIDDEN_PATCH_FIELDS:
            return False, f"禁止 patch 来源字段或 target 字段: {key}"
        if key not in PATCHABLE_FIELDS:
            return False, f"未知 patch 字段: {key}"

    # 值约束校验
    if "action" in patch and patch["action"] not in _ACTION_VALUES:
        return False, f"action 必须是 {_ACTION_VALUES} 之一"

    if "confidence" in patch and patch["confidence"] not in _CONFIDENCE_VALUES:
        return False, f"confidence 必须是 {_CONFIDENCE_VALUES} 之一"

    if "media_type" in patch and patch["media_type"] not in _MEDIA_TYPE_VALUES:
        return False, f"media_type 必须是 {_MEDIA_TYPE_VALUES} 之一"

    if "show_type" in patch and patch["show_type"] not in _SHOW_TYPE_VALUES:
        return False, f"show_type 必须是 {_SHOW_TYPE_VALUES} 之一"

    if "card_type" in patch and patch["card_type"] not in _CARD_TYPE_VALUES:
        return False, f"card_type 必须是 {_CARD_TYPE_VALUES} 之一"

    if "group_type" in patch and patch["group_type"] not in _GROUP_TYPE_VALUES:
        return False, f"group_type 必须是 {_GROUP_TYPE_VALUES} 之一"

    # 数值字段校验（必须为正整数或 None，不允许 0）
    for field_name in ("season_number", "episode_number", "special_number"):
        if field_name in patch:
            val = patch[field_name]
            if val is not None:
                if not isinstance(val, int) or val <= 0:
                    return False, f"{field_name} 必须为正整数或 None"

    if "year" in patch:
        val = patch["year"]
        if val is not None:
            if not isinstance(val, int) or not (1900 <= val <= 2099):
                return False, "year 必须为 1900-2099 或 None"

    return True, ""


def apply_patch_to_item(item: ImportPlanItem, patch: dict) -> None:
    """将 patch 应用到 ImportPlanItem

    要求 patch 已通过 validate_patch 校验。
    """
    for key, value in patch.items():
        if hasattr(item, key):
            setattr(item, key, value)

    # 设置 user_override_id
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    item.user_override_id = _make_override_id(item.plan_id, item.id, now)

    # 追加用户修正原因
    item.reasons.append(f"用户修正于 {now}")


def build_user_override(plan_id: str, item_id: str, source: str, patch: dict) -> UserOverride:
    """构建 UserOverride 记录"""
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    return UserOverride(
        override_id=_make_override_id(plan_id, item_id, now),
        plan_id=plan_id,
        item_id=item_id,
        source=source,
        updated_at=now,
        patch=patch,
    )
