# -*- coding: utf-8 -*-
"""OpenList 兄弟子目录集号撞车的单元级收口回归（2026-08-22 天元突破实库样本）。

背景：OpenList 相对路径前两段是「作品/结构子目录」，同一作品的 tv/ 与
sprcial/（拼错的 special）在目录身份下分裂，「06(On Air Ver)」变体集号
与正片 SxxEyy 撞车时收口规则失效，用户只看到无法分辨的重复剧集错误。
修复：durable discovery 在单元范围内（card_identity 覆盖）重跑两个收口
规则；legacy 全树计划行为不变（守卫用例见 test_media_recognizer）。
"""

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.import_plan.service import build_preview
from app.recognition.plan_recognizer import (
    _auto_resolve_duplicate_episodes,
    _move_implicit_season_collision_to_specials,
    recognize_import_plan_media,
)


def _plan(paths: list[str], *, source: str = "openlist", root_container: str = "") -> ImportPlan:
    plan = ImportPlan(
        plan_id="t", source=source, import_family="anime",
        root_container=root_container, items=[],
    )
    for i, path in enumerate(paths):
        plan.items.append(ImportPlanItem(
            id=f"it{i}", relative_path=path,
            resource_type="video", action="generate_strm",
        ))
    return plan


def _run_unit_scope(plan: ImportPlan, boundary: str) -> None:
    """模拟 discovery._process_unit 的单元级收口接线。"""
    _move_implicit_season_collision_to_specials(plan, card_identity=f"unit:{boundary}")
    _auto_resolve_duplicate_episodes(plan, card_identity=f"unit:{boundary}")


def test_openlist_sibling_variant_moves_to_special_not_duplicate():
    """On Air Ver 变体集号与正片 SxxEyy 撞车 → 变体归特别篇，无重复错误。"""
    plan = _plan([
        "天元突破/tv/天元突破：红莲螺岩 - S01E01 - 用你的钻头突破天际啊！.mkv",
        "天元突破/tv/天元突破：红莲螺岩 - S01E06 - 你们全部泡澡热晕过去吧！！.mkv",
        "天元突破/sprcial/[4K_EA] 天元突破 06(On Air Ver) [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        "天元突破/sprcial/[4K_EA] 天元突破 05.5 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
    ], root_container="天元突破")

    recognize_import_plan_media(plan)
    _run_unit_scope(plan, "天元突破")

    items = {item.relative_path: item for item in plan.items}
    tv_e6 = items["天元突破/tv/天元突破：红莲螺岩 - S01E06 - 你们全部泡澡热晕过去吧！！.mkv"]
    on_air = items["天元突破/sprcial/[4K_EA] 天元突破 06(On Air Ver) [简体内嵌]【Bilibili_AYWDXNH】.mkv"]

    assert tv_e6.group_type == "season" and tv_e6.season_number == 1 and tv_e6.episode_number == 6
    assert on_air.group_type == "special" and on_air.season_number == 0
    assert any("特别篇" in reason for reason in on_air.reasons)

    preview = build_preview(plan)
    duplicate_errors = [
        issue for issue in preview.issues
        if issue.code == "duplicate_episode" and issue.level == "error"
    ]
    assert not duplicate_errors, f"仍出现重复剧集错误: {[i.message for i in duplicate_errors]}"


def test_txt_category_layer_collision_unchanged_without_override():
    """目录树 TXT（分类层路径）经 legacy 路径（不传覆盖）行为保持不变。"""
    plan = _plan([
        "动画/天元突破/tv/天元突破：红莲螺岩 - S01E06 - 你们全部泡澡热晕过去吧！！.mkv",
        "动画/天元突破/sprcial/[4K_EA] 天元突破 06(On Air Ver) [简体内嵌]【Bilibili_AYWDXNH】.mkv",
    ], source="pan115")

    recognize_import_plan_media(plan)

    on_air = plan.items[1]
    assert on_air.group_type == "special" and on_air.season_number == 0


def test_unit_scope_both_implicit_duplicates_auto_resolve():
    """两个均无 Sxx 标记的同集号文件（不同兄弟目录）→ 单元内自动保留一个。"""
    plan = _plan([
        "天元突破/tv/[Group] 天元突破 06 [1080p].mkv",
        "天元突破/sprcial/[Group] 天元突破 06 [720p].mkv",
    ], root_container="天元突破")

    recognize_import_plan_media(plan)
    _run_unit_scope(plan, "天元突破")

    actions = sorted(item.action for item in plan.items)
    assert actions == ["generate_strm", "ignore"], actions
