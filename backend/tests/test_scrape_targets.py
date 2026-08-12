# -*- coding: utf-8 -*-
"""M08 ScrapeTarget 生成测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_item(item_id, work_title="测试", year=2024, group_type="season",
               season_number=1, episode_number=1, series_group="", card_type="main_series",
               original_title="", **kwargs):
    from app.import_plan.models import ImportPlanItem
    return ImportPlanItem(
        id=item_id, plan_id="p1", raw_file_id=f"r-{item_id}", source="pan115",
        relative_path=f"动画/test/{item_id}.mkv", real_path=f"H:\\test\\{item_id}.mkv",
        resource_type="video", action="generate_strm",
        work_title=work_title, year=year, group_type=group_type,
        season_number=season_number, episode_number=episode_number,
        series_group=series_group or work_title, card_type=card_type,
        original_title=original_title, **kwargs,
    )


def _make_plan(items, status="confirmed"):
    from app.import_plan.models import ImportPlan
    return ImportPlan(plan_id="p1", source="pan115", status=status, items=items)


def test_season_target():
    """CLANNAD S1 -> CLANNAD / 2007 / local_season=1"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("v1", work_title="CLANNAD", year=2007, season_number=1, episode_number=1),
        _make_item("v2", work_title="CLANNAD", year=2007, season_number=1, episode_number=2),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.scrape_title == "CLANNAD"
    assert t.scrape_year == 2007
    assert t.local_season_number == 1
    assert t.scrape_type == "tv"


def test_season_target_counts_unique_episode_numbers():
    """同季多版本文件不应把远端结构校验误报成 3 集。"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("original-e1", episode_number=1),
        _make_item("recut-e1", episode_number=1),
        _make_item("original-e2", episode_number=2),
    ]

    target = build_scrape_targets(_make_plan(items))[0]

    assert len(target.item_ids) == 3
    assert target.local_episode_count == 2


def test_same_title_in_two_card_directories_builds_two_targets(tmp_path):
    """同一来源的两个独立作品目录不能在刮削阶段重新合并。"""
    from app.scrape.target_builder import build_scrape_targets

    first = _make_item("copy-a", work_title="东方源", series_group="东方源")
    first.target_dir = str(tmp_path / "东方源" / "Season 1")
    first.relative_path = "动画/东方源/copy-a.mkv"
    second = _make_item("copy-b", work_title="东方源", series_group="东方源")
    second.target_dir = str(tmp_path / "东方源 (1)" / "Season 1")
    second.relative_path = "动画/东方源 (1)/copy-b.mkv"

    targets = build_scrape_targets(_make_plan([first, second]))

    assert len(targets) == 2
    assert {tuple(target.item_ids) for target in targets} == {("copy-a",), ("copy-b",)}


def test_target_keeps_common_filename_series_alias_as_original_title():
    """目录名与文件内剧名不同时，文件名前缀应作为搜索别名。"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("v1", work_title="明日同学的水手服", year=None, season_number=1, episode_number=1),
        _make_item("v2", work_title="明日同学的水手服", year=None, season_number=1, episode_number=2),
        _make_item("v3", work_title="明日同学的水手服", year=None, season_number=1, episode_number=3),
    ]
    for index, item in enumerate(items, start=1):
        item.relative_path = (
            "刮削好的动画/明日同学的水手服/S01/"
            f"明日酱的水手服 - S01E{index:02d} - 第{index}集.mkv"
        )

    targets = build_scrape_targets(_make_plan(items))

    assert len(targets) == 1
    assert targets[0].scrape_title == "明日同学的水手服"
    assert targets[0].original_title == "明日酱的水手服"


def test_season2_with_subwork():
    """CLANNAD S2 -> CLANNAD After Story / 2008 / local_season=2"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("v1", work_title="CLANNAD", year=None, season_number=2, episode_number=1,
                    reasons=["子作品目录: 3.CLANNAD After Story.[S02].2008"]),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.scrape_title == "CLANNAD After Story"
    assert t.scrape_year == 2008
    assert t.local_season_number == 2


def test_verified_later_season_keeps_strong_tmdb_hint_with_subwork():
    from app.scrape.target_builder import build_scrape_targets

    item = _make_item(
        "ggo-s2-e1",
        work_title="外传：Gun Gale Online",
        year=None,
        season_number=2,
        episode_number=1,
        series_group="外传：Gun Gale Online",
        tmdb_hint_id=78204,
        tmdb_hint_type="tv",
        reasons=["子作品目录: 外传：Gun Gale Online"],
    )
    item.relative_path = (
        "动画/刀剑神域.S1-S3+剧场版+外传/外传：Gun Gale Online/"
        "[4K_EA] 刀剑神域外传 Gun Gale Online 第2季 01.mkv"
    )

    target = build_scrape_targets(_make_plan([item]))[0]

    assert target.local_season_number == 2
    assert target.tmdb_hint_id == 78204


def test_one_room_s2_no_year():
    """One Room S2 -> One Room / year=None / needs_review"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("v1", work_title="One Room", year=None, season_number=2, episode_number=1,
                    reasons=["子作品目录: [LP-Raws] One Room S2 [Ma10p_1080p]"]),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.scrape_title == "One Room"
    assert t.scrape_year is None
    assert t.needs_review is True


def test_movie_target():
    """总集篇 -> movie target"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("v1", work_title="CLANNAD", year=2009, group_type="movie",
                    card_type="standalone", title="CLANNAD总集篇：在那苍绿的树下"),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.group_type == "movie"
    assert t.scrape_type == "movie"
    assert t.scrape_year == 2009


def test_tmdb_hint_carried_to_scrape_target():
    """ImportPlanItem 的 TMDB hint 要进入 ScrapeTarget，供搜索直连。"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item(
            "v1",
            work_title="Re: 从零开始的异世界生活",
            original_title="Re: 从零开始的异世界生活 (2016) {tmdb-65942}",
            year=2016,
            season_number=2,
            episode_number=1,
            tmdb_hint_id=65942,
            tmdb_hint_type="tv",
        ),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.tmdb_hint_id == 65942
    assert t.tmdb_hint_type == "tv"
    assert "{tmdb" not in t.original_title.lower()


def test_tmdb_hint_accepts_equals_and_square_brackets():
    """显式 TMDB 绑定支持 {tmdbid=123} 和 [tmdbid=123]。"""
    from app.scrape.target_builder import build_scrape_targets

    for original_title in ("葬送的芙莉莲 {tmdbid=209867}", "葬送的芙莉莲 [tmdbid=209867]"):
        items = [
            _make_item(
                "v1",
                work_title="葬送的芙莉莲",
                original_title=original_title,
                year=2023,
                season_number=1,
                episode_number=1,
            ),
        ]
        targets = build_scrape_targets(_make_plan(items))
        assert len(targets) == 1
        assert targets[0].tmdb_hint_id == 209867
        assert "tmdbid" not in targets[0].original_title.lower()


def test_op_ed_no_target():
    """OP/ED 不生成 target"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("v1", group_type="ignored", season_number=None, episode_number=None),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 0


def test_auxiliary_video_no_target():
    """PV/CM/Menu 等附属视频不生成 ScrapeTarget。"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("pv1", group_type="auxiliary", season_number=0, episode_number=None),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 0


def test_special_target():
    """Season 0 special 也要生成 TV 刮削目标"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item(
            "v1",
            work_title="莉可丽丝：友谊是时间的窃贼",
            group_type="special",
            season_number=0,
            episode_number=None,
            series_group="莉可丽丝：友谊是时间的窃贼",
        ),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.group_type == "special"
    assert t.scrape_type == "tv"
    assert t.local_season_number == 0
    assert t.scrape_title == "莉可丽丝：友谊是时间的窃贼"


def test_main_series_special_target_uses_series_group_not_first_subwork():
    """主系列 Season 0 混合多季特别篇时，不能被第一个子目录污染搜索标题。"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item(
            "s2-sp1",
            work_title="Yuru Camp Season 2",
            group_type="special",
            season_number=0,
            episode_number=None,
            special_number=1,
            series_group="Yuru Camp",
            year=None,
            reasons=["子作品目录: [VCB-Studio] Yuru Camp Season 2 [Ma10p_1080p]"],
        ),
        _make_item(
            "s1-sp1",
            work_title="Yuru Camp",
            group_type="special",
            season_number=0,
            episode_number=None,
            special_number=2,
            series_group="Yuru Camp",
            year=None,
            reasons=["子作品目录: [VCB-Studio] Yuru Camp [Ma10p_1080p]"],
        ),
    ]

    targets = build_scrape_targets(_make_plan(items))

    assert len(targets) == 1
    target = targets[0]
    assert target.group_type == "special"
    assert target.scrape_title == "Yuru Camp"
    assert target.source_subwork_dir == ""
    assert all("子作品目录" not in warning for warning in target.warnings)


def test_stable_id():
    """同一输入生成稳定 ID"""
    from app.scrape.target_builder import build_scrape_targets

    items = [_make_item("v1")]
    t1 = build_scrape_targets(_make_plan(items))
    t2 = build_scrape_targets(_make_plan(items))
    assert t1[0].scrape_target_id == t2[0].scrape_target_id


def test_target_id_does_not_change_with_import_plan_revision():
    """同一作品季度的增量计划不能让既有刮削绑定失效。"""
    from app.scrape.target_builder import build_scrape_targets

    first = _make_plan([_make_item("v1")])
    revised = _make_plan([_make_item("v1")])
    revised.plan_id = "p2"
    revised.items[0].plan_id = "p2"

    first_target = build_scrape_targets(first)[0]
    revised_target = build_scrape_targets(revised)[0]

    assert first_target.scrape_target_id == revised_target.scrape_target_id


def test_vinland_saga_s2():
    """冰海战记 S2 -> 冰海战记 / 2023 / local_season=2"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item("v1", work_title="冰海战记", year=2023, season_number=2, episode_number=1,
                    series_group="冰海战记"),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.scrape_title == "冰海战记"
    assert t.scrape_year == 2023
    assert t.local_season_number == 2
    assert t.scrape_type == "tv"


def test_movie_keeps_special_title():
    """总集篇 movie 保留特别标题"""
    from app.scrape.target_builder import build_scrape_targets
    from app.import_plan.models import ImportPlanItem

    items = [
        ImportPlanItem(
            id="v1", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/test/recap.mkv", real_path="H:\\test.mkv",
            resource_type="video", action="generate_strm",
            work_title="CLANNAD", year=2009, group_type="movie",
            card_type="standalone", media_type="movie",
            series_group="CLANNAD", title="CLANNAD总集篇：在那苍绿的树下",
        ),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert "总集篇" in t.scrape_title or "苍绿" in t.scrape_title, f"scrape_title: {t.scrape_title}"
    assert t.scrape_year == 2009
    assert t.scrape_type == "movie"


def test_series_movie_uses_specific_movie_title():
    """系列剧场版不能只用系列名做刮削标题"""
    from app.scrape.target_builder import build_scrape_targets
    from app.import_plan.models import ImportPlanItem

    items = [
        ImportPlanItem(
            id="v1", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/刀剑神域.S1-S3+剧场版+外传/4.剧场版：序列之争.2017/movie.mkv",
            real_path="H:\\test.mkv",
            resource_type="video", action="generate_strm",
            work_title="刀剑神域", year=2017, group_type="movie",
            card_type="standalone", media_type="movie",
            series_group="刀剑神域", title="4.剧场版：序列之争.2017",
            original_title="4.剧场版：序列之争.2017",
            reasons=["子作品目录: 4.剧场版：序列之争.2017"],
        ),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    t = targets[0]
    assert t.scrape_type == "movie"
    assert t.local_title == "刀剑神域 剧场版：序列之争"
    assert t.scrape_title == "刀剑神域 剧场版：序列之争"
    assert t.scrape_year == 2017


def test_movie_title_from_bracketed_filename():
    """字幕组合集目录不能盖住具体电影文件名"""
    from app.scrape.target_builder import build_scrape_targets
    from app.import_plan.models import ImportPlanItem

    items = [
        ImportPlanItem(
            id="v1", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path=(
                "动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]/"
                "[BeanSub&FZSD][Jujutsu_Kaisen_0][MOVIE][BDRip][CHS][1080P][AVC_AAC](FA6841CF).mp4"
            ),
            real_path="H:\\test.mp4",
            resource_type="video", action="generate_strm",
            work_title="Jujutsu_Kaisen", year=None, group_type="movie",
            card_type="standalone", media_type="movie",
            series_group="[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]",
        ),
    ]

    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    assert targets[0].scrape_title == "Jujutsu Kaisen 0"


def test_movie_title_does_not_repeat_equivalent_underscore_series_name():
    """系列名仅下划线不同，不得再次拼到已含系列名的电影标题前。"""
    from app.scrape.target_builder import build_scrape_targets
    from app.import_plan.models import ImportPlanItem

    items = [
        ImportPlanItem(
            id="v1", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/Jujutsu_Kaisen/Jujutsu Kaisen 0.mkv",
            real_path="H:\\Jujutsu Kaisen 0.mkv",
            resource_type="video", action="generate_strm",
            work_title="Jujutsu Kaisen 0", year=None, group_type="movie",
            card_type="standalone", media_type="movie",
            series_group="Jujutsu_Kaisen", title="Jujutsu Kaisen 0",
            original_title="Jujutsu Kaisen 0",
        ),
    ]

    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    assert targets[0].scrape_title == "Jujutsu Kaisen 0"


def test_movie_subwork_year_overrides_series_year():
    """剧场版年份应优先取子作品目录年份，而不是主系列年份"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item(
            "v1",
            work_title="钢之炼金术师 FA",
            series_group="钢之炼金术师 FA",
            year=2009,
            group_type="movie",
            card_type="standalone",
            title="剧场版：叹息之丘的圣星.2011",
            reasons=["子作品目录: 剧场版：叹息之丘的圣星.2011"],
        ),
    ]

    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    assert targets[0].scrape_year == 2011


def test_single_letter_numeric_prefix_cleaned_for_scrape():
    """旧 plan 中的 B 86-不存在的战区 刮削前应清成 86 不存在的战区"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item(
            "v1",
            work_title="B 86-不存在的战区",
            series_group="B 86-不存在的战区",
            year=2021,
            season_number=1,
            episode_number=1,
        ),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    assert targets[0].scrape_title == "86 不存在的战区"


def test_generic_season_work_title_falls_back_to_series_group():
    """work_title 被污染成 Season 1 时，刮削名必须回退到 series_group"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item(
            "v1",
            work_title="Season 1",
            series_group="更衣人偶坠入爱河",
            year=2022,
            season_number=1,
            episode_number=1,
        ),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    assert targets[0].local_title == "更衣人偶坠入爱河"
    assert targets[0].scrape_title == "更衣人偶坠入爱河"


def test_season_suffix_subwork_is_removed_from_scrape_title():
    """虫师/虫师S1 子目录用于季信息，搜索标题应清成虫师。"""
    from app.scrape.target_builder import build_scrape_targets

    items = [
        _make_item(
            "v1",
            work_title="虫师",
            series_group="虫师",
            year=None,
            season_number=1,
            episode_number=1,
            reasons=["子作品目录: 虫师S1"],
        ),
    ]
    targets = build_scrape_targets(_make_plan(items))
    assert len(targets) == 1
    assert targets[0].local_title == "虫师"
    assert targets[0].scrape_title == "虫师"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_season_target, test_season2_with_subwork, test_one_room_s2_no_year,
        test_movie_target, test_op_ed_no_target, test_special_target, test_stable_id,
        test_vinland_saga_s2, test_movie_keeps_special_title,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
