# -*- coding: utf-8 -*-
"""ImportPlan 归位质检测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _video_item(**kwargs):
    from app.import_plan.models import ImportPlanItem

    data = dict(
        id="i1",
        source="pan115",
        relative_path="动画/Test/Test.S01E01.mkv",
        resource_type="video",
        action="generate_strm",
        work_title="Test",
        media_type="tv",
        group_type="season",
        season_number=1,
        episode_number=1,
        card_type="main_series",
        series_group="Test",
    )
    data.update(kwargs)
    return ImportPlanItem(**data)


def _plan(items):
    from app.import_plan.models import ImportPlan

    return ImportPlan(plan_id="p1", source="pan115", items=items)


def test_validator_marks_op_ed_misplaced_as_season():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(relative_path="动画/AIR.2005/OP＆ED/AIR [NCOP01].mkv")
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert any(i.code == "season_conflicts_op_ed" for i in issues)
    assert item.needs_review is True
    assert any("OP/ED" in w for w in item.warnings)


def test_validator_marks_ova_misplaced_as_season():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.[S01][OVA]/CLANNAD [24].mkv")
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert any(i.code == "season_conflicts_special" for i in issues)
    assert item.needs_review is True


def test_validator_marks_movie_misplaced_as_season():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(relative_path="动画电影/蓦然回首 (2024)/蓦然回首 (2024) 2160p.mkv")
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert any(i.code == "season_conflicts_movie" for i in issues)
    assert item.needs_review is True


def test_validator_ignores_series_container_movie_range_for_season():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/1.刀剑神域.[S1].2012/刀剑神域 第1季 01.mkv",
        work_title="刀剑神域",
        series_group="刀剑神域",
    )
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert issues == []
    assert item.needs_review is False


def test_validator_accepts_normal_season():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(relative_path="动画/冰海战记.2019/冰海战记.S01E01.mkv")
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert issues == []
    assert item.needs_review is False


def test_validator_accepts_sps():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.[S01][OVA]/CLANNAD [24].mkv",
        media_type="tv",
        group_type="special",
        season_number=1,
        episode_number=None,
    )
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert issues == []
    assert item.needs_review is False


def test_validator_accepts_auxiliary():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(
        relative_path="动画/Vinland Saga/[CZ&MAI] Vinland Saga [Menu01][Ma10p_2160p][x265_flac].mkv",
        media_type="tv",
        group_type="auxiliary",
        season_number=0,
        episode_number=None,
        title="MENU01",
    )
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert issues == []
    assert item.needs_review is False


def test_validator_accepts_movie():
    from app.import_plan.placement_validator import validate_import_plan_placement

    item = _video_item(
        relative_path="动画电影/蓦然回首 (2024)/蓦然回首 (2024) 2160p.mkv",
        media_type="movie",
        group_type="movie",
        card_type="standalone",
        season_number=None,
        episode_number=None,
    )
    issues = validate_import_plan_placement(_plan([item]), mutate=True)

    assert issues == []
    assert item.needs_review is False
