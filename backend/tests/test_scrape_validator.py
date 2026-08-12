# -*- coding: utf-8 -*-
"""Scrape metadata validation tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _target(local_count=8, group_type="season"):
    from app.scrape.models import ScrapeTarget

    return ScrapeTarget(
        scrape_target_id="t1",
        source="local",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        show_type="anime_series",
        group_type=group_type,
        series_group="测试番剧",
        local_title="测试番剧",
        scrape_title="测试番剧",
        scrape_type="tv",
        local_season_number=1 if group_type != "special" else 0,
        item_ids=[f"v{i}" for i in range(local_count)],
    )


def test_episode_count_mismatch_warns_but_does_not_block_sparse_library():
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    detail = {
        "id": 123,
        "name": "测试番剧",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [{"season_number": 1, "episode_count": 13}],
    }
    issues = validate_scrape_metadata(_target(local_count=8), detail, "tv", tmdb_season_number=1)

    assert blocking_issues(issues) == []
    assert any(
        issue.code == "episode_count_mismatch" and issue.level == "warn"
        for issue in issues
    )


def test_episode_count_validation_uses_unique_local_episode_count():
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    target = _target(local_count=38)
    target.local_episode_count = 25
    detail = {
        "id": 123,
        "name": "测试番剧",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [{"season_number": 1, "episode_count": 25}],
    }

    issues = validate_scrape_metadata(target, detail, "tv", tmdb_season_number=1)

    assert blocking_issues(issues) == []
    assert not any(issue.code == "episode_count_mismatch" for issue in issues)


def test_absolute_local_prefix_matching_complete_tmdb_seasons_is_not_a_mismatch():
    """本地连续 47 集等于 TMDB 前两季 24+23，不应因缺少第三季而告警。"""
    from app.scrape.validator import validate_scrape_metadata

    detail = {
        "id": 95479,
        "name": "咒术回战",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [
            {"season_number": 1, "episode_count": 24},
            {"season_number": 2, "episode_count": 23},
            {"season_number": 3, "episode_count": 12},
        ],
    }

    for local_count in (47, 59):
        target = _target(local_count=local_count)
        target.local_episode_count = local_count
        issues = validate_scrape_metadata(target, detail, "tv", tmdb_season_number=1)
        assert not any(issue.code == "episode_count_mismatch" for issue in issues)


def test_contiguous_local_prefix_of_single_absolute_tmdb_season_is_not_a_mismatch(monkeypatch):
    """TMDB 单季使用 1-59 绝对编号时，本地只有连续 1-47 仍是合法前缀。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.scrape.validator import validate_scrape_metadata

    items = [
        ImportPlanItem(
            id=f"e{episode}",
            action="generate_strm",
            group_type="season",
            season_number=1,
            episode_number=episode,
        )
        for episode in range(1, 48)
    ]
    plan = ImportPlan(plan_id="jjk-prefix", items=items)
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)
    target = _target(local_count=47)
    target.import_plan_id = plan.plan_id
    target.item_ids = [item.id for item in items]
    target.local_episode_count = 47
    detail = {
        "id": 95479,
        "name": "咒术回战",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [{"season_number": 1, "episode_count": 59}],
    }

    issues = validate_scrape_metadata(target, detail, "tv", tmdb_season_number=1)

    assert not any(issue.code == "episode_count_mismatch" for issue in issues)


def test_absolute_tmdb_episode_count_mismatch_does_not_block_split_local_season():
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    target = _target(local_count=25)
    target.local_season_number = 2
    detail = {
        "id": 123,
        "name": "测试番剧",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [{"season_number": 1, "episode_count": 50}],
    }

    issues = validate_scrape_metadata(target, detail, "tv", tmdb_season_number=1)

    assert blocking_issues(issues) == []
    assert any(issue.code == "episode_count_mismatch" and issue.level == "warn" for issue in issues)


def test_special_episode_count_mismatch_warns_but_does_not_block():
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    detail = {
        "id": 123,
        "name": "测试番剧",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [{"season_number": 0, "episode_count": 20}],
    }
    issues = validate_scrape_metadata(_target(local_count=5, group_type="special"), detail, "tv", tmdb_season_number=0)

    assert any(issue.code == "episode_count_mismatch" and issue.level == "warn" for issue in issues)
    assert blocking_issues(issues) == []


def test_movie_target_cannot_bind_to_tv_metadata():
    from app.scrape.models import ScrapeTarget
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    target = ScrapeTarget(
        scrape_target_id="m1",
        source="local",
        import_plan_id="p1",
        work_id="w1",
        card_type="standalone",
        media_type="movie",
        group_type="movie",
        series_group="测试电影",
        local_title="测试电影",
        scrape_title="测试电影",
        scrape_type="movie",
        item_ids=["v1"],
    )
    issues = validate_scrape_metadata(target, {"id": 123, "name": "测试 TV"}, "tv", tmdb_season_number=1)

    assert any(issue.code == "type_mismatch" for issue in blocking_issues(issues))


def test_season1_episode_count_mismatch_warns_when_tmdb_merges_seasons():
    """本地 Season 1 12 集 vs TMDB Season 1 24 集（两季合并标注）→ warn 不 block"""
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    target = _target(local_count=12)
    target.local_season_number = 1
    detail = {
        "id": 241811,
        "name": "噗妮露是可爱史莱姆",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [
            {"season_number": 1, "episode_count": 24},
            {"season_number": 2, "episode_count": 12},
        ],
    }
    issues = validate_scrape_metadata(target, detail, "tv", tmdb_season_number=1)
    assert blocking_issues(issues) == []
    assert any(issue.code == "episode_count_mismatch" and issue.level == "warn" for issue in issues)


def test_explicit_tmdb_hint_allows_absolute_season_count_that_is_not_multiple():
    """Re:Zero-like TMDB data: all local seasons live under one absolute Season 1."""
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    target = _target(local_count=25)
    target.local_season_number = 1
    target.tmdb_hint_id = 65942
    detail = {
        "id": 65942,
        "name": "Re：从零开始的异世界生活",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [
            {"season_number": 0, "episode_count": 77},
            {"season_number": 1, "episode_count": 85},
        ],
    }

    issues = validate_scrape_metadata(target, detail, "tv", tmdb_season_number=1)

    assert blocking_issues(issues) == []
    assert any(issue.code == "episode_count_mismatch" and issue.level == "warn" for issue in issues)


def test_explicit_tmdb_hint_does_not_block_partial_remote_season():
    """明确 TMDB 绑定时，本地只收录该季一部分内容也应保留主元数据。"""
    from app.scrape.validator import blocking_issues, validate_scrape_metadata

    target = _target(local_count=24)
    target.local_season_number = 4
    target.tmdb_hint_id = 86031
    detail = {
        "id": 86031,
        "name": "石纪元",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [{"season_number": 4, "episode_count": 37}],
    }

    issues = validate_scrape_metadata(target, detail, "tv", tmdb_season_number=4)

    assert blocking_issues(issues) == []
    assert any(issue.code == "episode_count_mismatch" and issue.level == "warn" for issue in issues)


def test_confirmed_series_reuse_does_not_emit_localized_title_warning():
    """已确认同系列绑定只校验季度结构，不重复质疑跨语言主标题。"""
    from app.scrape.models import ScrapeTarget
    from app.scrape.validator import validate_scrape_metadata

    target = ScrapeTarget(
        scrape_target_id="rezero-season-2",
        source="pan115",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="Re：从零开始的异世界生活",
        local_title="Re：从零开始的异世界生活",
        scrape_title="Re：从零开始的异世界生活",
        scrape_year=2020,
        scrape_type="tv",
        local_season_number=2,
    )
    detail = {
        "id": 65942,
        "name": "Re: 从零开始的异世界生活",
        "original_name": "Re:ゼロから始める異世界生活",
        "genres": [{"id": 16, "name": "Animation"}],
        "seasons": [{"season_number": 2, "episode_count": 25}],
    }

    issues = validate_scrape_metadata(
        target,
        detail,
        "tv",
        tmdb_season_number=2,
        trusted_series_binding=True,
    )

    assert not any(issue.code == "title_low_similarity" for issue in issues)
