# -*- coding: utf-8 -*-
"""自动刮削测试"""

import pytest
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.scrape.auto import (
    _blocking_issues_after_candidate_evidence,
    _find_series_binding,
    _find_series_tmdb_id,
    _scrape_result_has_required_artwork,
    _should_reuse_series_scrape,
    _target_already_scraped,
    decide_auto_candidate,
    infer_auto_tmdb_season_number,
    run_auto_scrape,
)
from app.import_plan.models import ImportPlan, ImportPlanItem
from app.scrape.models import ScrapeCandidate, ScrapeTarget
from app.api.scrape import _resolve_tmdb_season_number
from app.scrape.service import _build_target_search_title_variants, resolve_tmdb_season_number
from app.scrape.validator import ScrapeValidationIssue


def test_scoped_auto_scrape_only_processes_requested_targets_without_pruning_queue(monkeypatch):
    """详情卡片子任务不得处理或归档其他作品的刮削目标。"""
    import app.import_plan.store as import_plan_store
    import app.scrape.auto as auto_module
    import app.scrape.review_queue as review_queue
    import app.scrape.service as scrape_service

    first = ScrapeTarget(
        scrape_target_id="railgun-s1",
        source="local",
        import_plan_id="plan-railgun",
        work_id="railgun-s1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="某科学的超电磁炮",
        local_season_number=1,
        scrape_title="某科学的超电磁炮",
        scrape_type="tv",
    )
    second = ScrapeTarget(
        scrape_target_id="railgun-s2",
        source="local",
        import_plan_id="plan-railgun",
        work_id="railgun-s2",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="某科学的超电磁炮",
        local_season_number=2,
        scrape_title="某科学的超电磁炮S",
        scrape_type="tv",
    )
    prune_calls = []

    monkeypatch.setattr(scrape_service, "get_targets", lambda *_args, **_kwargs: ([first, second], None))
    monkeypatch.setattr(import_plan_store, "load_import_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(import_plan_store, "load_latest_confirmed_import_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(review_queue, "get_pending_review_items", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        review_queue,
        "prune_pending_review_items",
        lambda *args, **kwargs: prune_calls.append((args, kwargs)) or 0,
    )
    monkeypatch.setattr(auto_module, "_build_existing_scrape_index", lambda: {})
    monkeypatch.setattr(auto_module, "load_scrape_map", lambda: SimpleNamespace(items=[]))
    monkeypatch.setattr(auto_module, "_target_already_scraped", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(auto_module, "_work_targets_complete", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        auto_module,
        "_group_targets_by_library_work",
        lambda targets, _plans: (
            {target.scrape_target_id: target.scrape_target_id for target in targets},
            {target.scrape_target_id: [target] for target in targets},
        ),
    )

    result = run_auto_scrape(
        "local",
        plan_id="plan-railgun",
        tmdb_client=object(),
        target_ids={"railgun-s2"},
        library_work_id="library-railgun",
    )

    assert result["total_targets"] == 1
    assert [item["target_id"] for item in result["results"]] == ["railgun-s2"]
    assert prune_calls == []


@pytest.fixture
def sample_target():
    return ScrapeTarget(
        scrape_target_id="t1",
        source="pan115",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="CLANNAD",
        local_title="CLANNAD",
        scrape_title="CLANNAD",
        scrape_year=2007,
        scrape_type="tv",
        local_season_number=1,
        needs_review=False,
        warnings=[],
    )


def test_scrape_result_requires_poster_and_fanart():
    assert _scrape_result_has_required_artwork({"poster_path": "poster.jpg", "fanart_path": "fanart.jpg"})
    assert not _scrape_result_has_required_artwork({"poster_path": "", "fanart_path": "fanart.jpg"})
    assert not _scrape_result_has_required_artwork({"poster_path": "poster.jpg", "fanart_path": ""})


def test_demon_slayer_arc_uses_verified_tmdb_season():
    target = ScrapeTarget(
        scrape_target_id="kimetsu-entertainment",
        source="pan115",
        import_plan_id="p1",
        work_id="w1",
        media_type="tv",
        group_type="season",
        scrape_type="tv",
        local_season_number=2,
        source_subwork_dir="3.游郭篇(花街篇).[S2].2021",
        scrape_title="鬼灭之刃",
    )
    assert resolve_tmdb_season_number(target, 85937, "tv") == 3


@pytest.fixture
def high_score_candidate():
    return ScrapeCandidate(
        candidate_id="c1",
        scrape_target_id="t1",
        provider="tmdb",
        tmdb_id=12189,
        tmdb_type="tv",
        title="CLANNAD",
        original_title="CLANNAD",
        year=2007,
        score=85,
        popularity=50,
        vote_average=8.5,
        reasons=["标题完全匹配", "年份匹配"],
    )


@pytest.fixture
def low_score_candidate():
    return ScrapeCandidate(
        candidate_id="c2",
        scrape_target_id="t1",
        provider="tmdb",
        tmdb_id=99999,
        tmdb_type="tv",
        title="Something Else",
        original_title="Something Else",
        year=2020,
        score=30,
        popularity=10,
        vote_average=5.0,
        reasons=["标题部分匹配"],
    )


def test_series_reuse_can_fill_first_season_from_confirmed_second_season(sample_target):
    scrape_map = SimpleNamespace(items=[SimpleNamespace(
        source="baidu",
        card_type="main_series",
        media_type="tv",
        tmdb_type="tv",
        tmdb_id=94664,
        series_group="无职转生",
        local_title="无职转生",
        scrape_title="无职转生",
        local_season_number=2,
        confidence="high",
        nfo_path="",
    )])
    sample_target.series_group = "无职转生"
    sample_target.local_title = "无职转生"
    sample_target.scrape_title = "无职转生"
    sample_target.local_season_number = 1

    assert _should_reuse_series_scrape(sample_target) is True
    assert _find_series_tmdb_id(sample_target, scrape_map) == 94664


def test_candidate_dedupe_preserves_trusted_provider_alias_evidence(sample_target):
    from app.scrape.service import _dedupe_candidates

    tmdb = ScrapeCandidate(
        provider="tmdb", tmdb_id=624860, tmdb_type="movie",
        title="鬼灭之刃：无限列车篇", original_title="劇場版 鬼滅の刃 無限列車編",
        score=95, popularity=100,
    )
    anilist = ScrapeCandidate(
        provider="anilist", tmdb_id=624860, tmdb_type="movie",
        title="Kimetsu no Yaiba Mugen Ressha-hen", original_title="劇場版 鬼滅の刃 無限列車編",
        score=90, popularity=80,
        raw={
            "provider_title_aliases": ["Kimetsu no Yaiba Mugen Ressha-hen", "鬼灭之刃：无限列车篇"],
            "provider_tmdb_link": "direct",
        },
    )

    merged = _dedupe_candidates([tmdb, anilist])[0]

    assert merged.tmdb_id == 624860
    assert merged.raw["provider_title_aliases"] == anilist.raw["provider_title_aliases"]
    assert merged.raw["provider_tmdb_link"] == "direct"


def test_series_binding_reuses_confirmed_tmdb_season_from_other_source(sample_target):
    scrape_map = SimpleNamespace(items=[SimpleNamespace(
        source="baidu", card_type="main_series", media_type="tv", tmdb_type="tv",
        tmdb_id=65942, tmdb_season_number=1, series_group="Re：从零开始的异世界生活",
        local_title="Re：从零开始的异世界生活", scrape_title="Re：从零开始的异世界生活",
        original_title="", local_season_number=2, confidence="high", nfo_path="",
        identity_evidence={},
    )])
    sample_target.source = "pan115"
    sample_target.series_group = "Re：从零开始的异世界生活"
    sample_target.local_title = sample_target.series_group
    sample_target.scrape_title = sample_target.series_group
    sample_target.local_season_number = 2

    assert _find_series_binding(sample_target, scrape_map) == (65942, 1)


def test_series_reuse_accepts_explicit_later_season_suffix_only_for_later_season(sample_target):
    scrape_map = SimpleNamespace(items=[SimpleNamespace(
        source="pan115",
        card_type="main_series",
        media_type="tv",
        tmdb_type="tv",
        tmdb_id=94664,
        series_group="无职转生",
        local_title="无职转生",
        scrape_title="无职转生",
        local_season_number=1,
        confidence="high",
        nfo_path="",
    )])
    sample_target.series_group = "无职转生2"
    sample_target.local_title = "无职转生2"
    sample_target.scrape_title = "无职转生2"
    sample_target.local_season_number = 2

    assert _find_series_tmdb_id(sample_target, scrape_map) == 94664

    sample_target.local_season_number = 1
    assert _find_series_tmdb_id(sample_target, scrape_map) is None


def test_series_reuse_rejects_legacy_mapping_whose_nfo_title_conflicts(tmp_path, sample_target):
    nfo = tmp_path / "tvshow.nfo"
    nfo.write_text("<tvshow><title>异世界魔王与召唤少女的奴隶魔术</title></tvshow>", encoding="utf-8")
    scrape_map = SimpleNamespace(items=[SimpleNamespace(
        source="baidu", card_type="main_series", media_type="tv", tmdb_type="tv",
        tmdb_id=80563, series_group="异世界舅舅", local_title="异世界舅舅",
        scrape_title="异世界舅舅", original_title="", local_season_number=1,
        confidence="high", nfo_path=str(nfo), identity_evidence={},
    )])
    sample_target.source = "baidu"
    sample_target.series_group = "异世界舅舅"
    sample_target.local_title = "异世界舅舅"
    sample_target.scrape_title = "异世界舅舅"

    assert _find_series_tmdb_id(sample_target, scrape_map) is None


# ============================================================
# decide_auto_candidate 测试
# ============================================================

class TestDecideAutoCandidate:
    """测试自动采用决策"""

    def test_clannad_movie_unique_candidate_accepts_equivalent_movie_alias(self, sample_target):
        """CLANNAD 剧场版与 Clannad Movie / 劇場版 クラナド应视为同一电影。"""
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "CLANNAD 剧场版"
        sample_target.local_title = "CLANNAD 剧场版"
        sample_target.series_group = "CLANNAD"
        candidate = ScrapeCandidate(
            candidate_id="clannad-movie",
            provider="anilist",
            tmdb_id=16516,
            tmdb_type="movie",
            title="Clannad Movie",
            original_title="劇場版 クラナド",
            year=2007,
            score=122,
            raw={
                "provider_title_aliases": ["劇場版 クラナド", "Clannad: The Motion Picture", "Clannad Movie"],
                "provider_tmdb_link": "title_resolution",
            },
        )

        selected, reason = decide_auto_candidate(sample_target, [candidate])

        assert selected is candidate
        assert "自动采用" in reason

    @pytest.mark.parametrize(
        ("local_title", "tmdb_id", "romanized_title", "native_title", "year"),
        [
            ("福音战士新剧场版：序", 15137, "Evangelion Shin Movie: Jo", "ヱヴァンゲリヲン新劇場版:序", 2007),
            ("福音战士新剧场版：破", 22843, "Evangelion Shin Movie: Ha", "ヱヴァンゲリヲン新劇場版:破", 2009),
        ],
    )
    def test_evangelion_movie_chapter_matches_native_title(
        self, sample_target, local_title, tmdb_id, romanized_title, native_title, year
    ):
        """中文《序》《破》可由日文原名中的新劇場版篇章标识确认。"""
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = local_title
        sample_target.local_title = local_title
        sample_target.series_group = "新世纪福音战士"
        sample_target.scrape_year = year
        correct = ScrapeCandidate(
            candidate_id=f"eva-{tmdb_id}",
            provider="anilist",
            tmdb_id=tmdb_id,
            tmdb_type="movie",
            title=romanized_title,
            original_title=native_title,
            year=year,
            score=103,
            raw={
                "provider_title_aliases": [native_title, romanized_title],
                "provider_tmdb_link": "title_resolution",
            },
        )
        unrelated = ScrapeCandidate(
            candidate_id="wrong",
            provider="tmdb",
            tmdb_id=8088,
            tmdb_type="movie",
            title="破碎的拥抱",
            original_title="Los abrazos rotos",
            year=year,
            score=68.7,
        )

        selected, reason = decide_auto_candidate(sample_target, [correct, unrelated])

        assert selected is correct
        assert "自动采用" in reason

    def test_high_score_auto_adopt(self, sample_target, high_score_candidate):
        """高分候选应自动采用"""
        candidate, reason = decide_auto_candidate(
            sample_target, [high_score_candidate], threshold=70
        )
        assert candidate is not None
        assert candidate.tmdb_id == 12189
        assert "自动采用" in reason

    def test_original_title_alias_can_confirm_identity(self, sample_target):
        """文件名前缀别名命中时，即使目录名不同也可自动采用。"""
        sample_target.series_group = "明日同学的水手服"
        sample_target.local_title = "明日同学的水手服"
        sample_target.scrape_title = "明日同学的水手服"
        sample_target.original_title = "明日酱的水手服"
        sample_target.scrape_year = None
        candidate = ScrapeCandidate(
            candidate_id="c-alias",
            scrape_target_id="t1",
            provider="anilist",
            tmdb_id=121792,
            tmdb_type="tv",
            title="明日酱的水手服",
            original_title="明日ちゃんのセーラー服",
            year=2022,
            score=45,
            popularity=12,
            vote_average=7.0,
            reasons=["标题完全匹配"],
        )

        selected, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)

        assert selected == candidate
        assert "自动采用" in reason

    def test_tv_first_season_allows_one_year_offset_with_strong_identity(self, sample_target):
        """目录年份常取文件发布时间；首播跨年但标题已确认时不应漏刮第一季。"""
        sample_target.series_group = "伪恋"
        sample_target.local_title = "伪恋"
        sample_target.scrape_title = "伪恋.NISEKOI"
        sample_target.scrape_year = 2013
        candidate = ScrapeCandidate(
            candidate_id="nisekoi-s1",
            scrape_target_id=sample_target.scrape_target_id,
            provider="tmdb",
            tmdb_id=62640,
            tmdb_type="tv",
            title="Nisekoi",
            original_title="ニセコイ",
            year=2014,
            score=132,
            popularity=100,
            vote_average=7.3,
            reasons=["AniList 命中(18897)", "标题完全匹配"],
        )

        selected, reason = decide_auto_candidate(sample_target, [candidate])

        assert selected is candidate
        assert "自动采用" in reason

    def test_prefers_identity_safe_candidate_over_higher_scored_lookalike(self, sample_target):
        """正确候选在列表中时，不能被高分的相似标题遮蔽。"""
        sample_target.series_group = "路人女主的养成方法"
        sample_target.local_title = "路人女主的养成方法"
        sample_target.scrape_title = "路人女主的养成方法"
        sample_target.scrape_year = None
        wrong = ScrapeCandidate(
            candidate_id="c-lookalike",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=139512,
            tmdb_type="tv",
            title="恋爱游戏世界对路人角色很不友好",
            original_title="乙女ゲー世界はモブに厳しい世界です",
            year=2022,
            score=93.5,
            popularity=28,
            vote_average=7.3,
            reasons=["标题部分匹配", "高评分(7.3)"],
        )
        correct = ScrapeCandidate(
            candidate_id="c-exact",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=69367,
            tmdb_type="tv",
            title="路人女主的养成方法",
            original_title="冴えない彼女の育てかた",
            year=2015,
            score=76,
            popularity=18,
            vote_average=7.7,
            reasons=["标题完全匹配"],
        )

        selected, reason = decide_auto_candidate(sample_target, [wrong, correct], threshold=70)

        assert selected is correct
        assert "自动采用" in reason

    def test_movie_part_marker_is_an_identity_constraint_not_a_small_score_bonus(self, sample_target):
        """同系列前后篇同时命中时，不能把高分的相反篇章自动采用。"""
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "BanG Dream! It's MyGO!!!!! 前篇：春日向阳，迷途野猫"
        sample_target.local_title = sample_target.scrape_title
        sample_target.series_group = sample_target.scrape_title
        sample_target.scrape_year = 2024

        wrong_part = ScrapeCandidate(
            candidate_id="mygo-back",
            scrape_target_id="t1",
            tmdb_id=1233186,
            tmdb_type="movie",
            title="迷途之子!!!!! 后篇：唱吧、成为我们羁绊的诗歌＆电影演唱会",
            original_title="BanG Dream! It's MyGO!!!!! 後編：うたう、僕らになれるうた & FILM LIVE",
            year=2024,
            score=128,
            reasons=["标题高度相似", "篇章不匹配", "年份匹配"],
        )
        correct_part = ScrapeCandidate(
            candidate_id="mygo-front",
            scrape_target_id="t1",
            tmdb_id=1231799,
            tmdb_type="movie",
            title="迷途之子!!!!! 前篇：春暖向阳，迷星之猫",
            original_title="BanG Dream! It's MyGO!!!!! 前編：春の陽だまり、迷い猫",
            year=2024,
            score=121,
            reasons=["标题高度相似", "篇章匹配", "年份匹配"],
            provider="anilist",
            raw={
                "provider_title_aliases": [
                    "BanG Dream! It's MyGO!!!!! 前篇：春日向阳，迷途野猫",
                    "BanG Dream! It's MyGO!!!!! 前編：春の陽だまり、迷い猫",
                ],
                "provider_tmdb_link": "direct",
            },
        )

        selected, _ = decide_auto_candidate(sample_target, [wrong_part, correct_part])

        assert selected is correct_part

    def test_bangumi_confirmed_alias_must_pass_title_identity(self, sample_target):
        """Bangumi 命中不再绕过标题身份校验；候选必须通过标题验证才能自动采用。"""
        sample_target.scrape_target_id = "hellsing-ultimate"
        sample_target.series_group = "Hellsing Ultimate"
        sample_target.local_title = "Hellsing Ultimate"
        sample_target.scrape_title = "Hellsing Ultimate"
        sample_target.local_season_number = 1
        sample_target.scrape_year = None

        # OVA 候选的 TMDB 标题与本地标题一致（Bangumi→TMDB 解析后应为英文标题）
        ova = ScrapeCandidate(
            candidate_id="hellsing-ova",
            scrape_target_id=sample_target.scrape_target_id,
            provider="tmdb",
            tmdb_id=61752,
            tmdb_type="tv",
            title="Hellsing Ultimate",
            original_title="Hellsing Ultimate",
            year=2006,
            score=92.9,
            reasons=["Bangumi 命中(493)", "标题完全匹配", "标题高度相似"],
        )
        original_series = ScrapeCandidate(
            candidate_id="hellsing-tv",
            scrape_target_id=sample_target.scrape_target_id,
            tmdb_id=16830,
            tmdb_type="tv",
            title="Hellsing",
            original_title="Hellsing",
            year=2001,
            score=80.8,
            reasons=["Bangumi 命中(2216)", "标题部分匹配"],
        )

        selected, _ = decide_auto_candidate(sample_target, [ova, original_series])

        assert selected is ova, "标题一致的 Bangumi 候选应被自动采用"
        assert selected is ova

    def test_bangumi_only_alias_without_title_identity_goes_to_review(self, sample_target):
        """仅有 Bangumi 命中但标题不一致的候选不应自动采用，应进入人工确认。"""
        sample_target.scrape_target_id = "akebi"
        sample_target.local_title = "明日同学的水手服"
        sample_target.scrape_title = "明日同学的水手服"
        sample_target.scrape_year = None

        wrong_candidate = ScrapeCandidate(
            candidate_id="akebi-wrong",
            scrape_target_id=sample_target.scrape_target_id,
            provider="bangumi",
            tmdb_id=99999,
            tmdb_type="tv",
            title="明日的与一",
            original_title="明日的与一",
            year=2022,
            score=75.0,
            reasons=["Bangumi 命中(12345)", "标题相似"],
        )

        selected, reason = decide_auto_candidate(sample_target, [wrong_candidate])

        assert selected is None, "Bangumi 命中但标题不一致不应自动采用"
        assert reason != ""

    def test_anilist_trusted_alias_chain_accepts_cross_language_movie(self, sample_target):
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "Jujutsu Kaisen 0"
        sample_target.local_title = "Jujutsu Kaisen 0"
        sample_target.series_group = "Jujutsu Kaisen"
        sample_target.scrape_year = 2021
        candidate = ScrapeCandidate(
            candidate_id="jjk0",
            scrape_target_id=sample_target.scrape_target_id,
            provider="anilist",
            tmdb_id=810693,
            tmdb_type="movie",
            title="Jujutsu Kaisen 0",
            original_title="劇場版 呪術廻戦 0",
            year=2021,
            score=90,
            reasons=["AniList 命中(131573)", "标题完全匹配", "年份匹配"],
            raw={
                "provider_title_aliases": ["Jujutsu Kaisen 0", "劇場版 呪術廻戦 0", "咒术回战 0"],
                "provider_tmdb_link": "direct",
            },
        )

        selected, _ = decide_auto_candidate(sample_target, [candidate])
        blockers = [ScrapeValidationIssue("title_low_similarity", "block", "标题不同")]
        remaining = _blocking_issues_after_candidate_evidence(
            sample_target, candidate, [candidate, MagicMock()], blockers
        )

        assert selected is candidate
        assert remaining == []

    def test_wrong_bangumi_subject_alias_cannot_bypass_title_block(self, sample_target):
        sample_target.scrape_title = "明日同学的水手服"
        sample_target.local_title = "明日同学的水手服"
        sample_target.series_group = "明日同学的水手服"
        candidate = ScrapeCandidate(
            candidate_id="wrong-akebi",
            scrape_target_id=sample_target.scrape_target_id,
            provider="bangumi",
            tmdb_id=46033,
            tmdb_type="tv",
            title="明日的与一",
            original_title="明日のよいち!",
            year=2009,
            score=90,
            raw={
                "provider_title_aliases": ["明日的与一", "明日のよいち!"],
                "provider_tmdb_link": "title_resolution",
            },
        )
        blockers = [ScrapeValidationIssue("title_low_similarity", "block", "标题不同")]

        remaining = _blocking_issues_after_candidate_evidence(
            sample_target, candidate, [candidate], blockers
        )

        assert remaining == blockers

    def test_accepts_romanized_title_with_stable_cjk_original_prefix(self, sample_target):
        """TMDB 罗马音主标题可由日文原名中的稳定中文前缀确认。"""
        sample_target.series_group = "天元突破"
        sample_target.local_title = "天元突破"
        sample_target.scrape_title = "天元突破"
        sample_target.original_title = "天元突破：红莲螺岩"
        sample_target.scrape_year = None
        candidate = ScrapeCandidate(
            candidate_id="c-gurren-lagann",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=21729,
            tmdb_type="tv",
            title="Tengen Toppa Gurren Lagann",
            original_title="天元突破グレンラガン",
            year=2007,
            score=94,
            popularity=35,
            vote_average=8.4,
            reasons=["标题高度相似"],
        )

        selected, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)

        assert selected is candidate
        assert "自动采用" in reason

    def test_low_score_year_conflict_reject(self, sample_target, low_score_candidate):
        """唯一候选低分也可采用，但年份明确冲突时拒绝"""
        candidate, reason = decide_auto_candidate(
            sample_target, [low_score_candidate], threshold=70
        )
        assert candidate is None
        assert "年份不一致" in reason

    def test_low_score_same_year_unique_adopt(self, sample_target, low_score_candidate):
        """唯一候选也必须通过标题身份校验"""
        low_score_candidate.year = 2007
        low_score_candidate.score = 31
        candidate, reason = decide_auto_candidate(
            sample_target, [low_score_candidate], threshold=70
        )
        assert candidate is None
        assert "标题不够明确" in reason

    def test_unique_candidate_low_score_still_adopt(self, sample_target, low_score_candidate):
        """唯一低分候选不能绕过标题身份校验"""
        low_score_candidate.year = 2007
        low_score_candidate.score = 8
        candidate, reason = decide_auto_candidate(
            sample_target, [low_score_candidate], threshold=70
        )
        assert candidate is None
        assert "标题不够明确" in reason

    def test_unique_tv_candidate_without_title_evidence_requires_review(self, sample_target):
        """唯一 TV 候选如果没有标题证据，不能只靠类型/热度自动采用。"""
        wrong_candidate = ScrapeCandidate(
            candidate_id="c_wrong",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=244620,
            tmdb_type="tv",
            title="从路人角色开始的探索英雄谭",
            original_title="モブから始まる探索英雄譚",
            year=2024,
            score=20,
            popularity=50,
            vote_average=7.3,
            reasons=["类型匹配", "高评分(7.3)"],
        )
        sample_target.scrape_title = "路人女主的养成方法"
        sample_target.local_title = "路人女主的养成方法"
        sample_target.series_group = "路人女主的养成方法"
        sample_target.scrape_year = None
        sample_target.local_year = None

        candidate, reason = decide_auto_candidate(
            sample_target, [wrong_candidate], threshold=70
        )

        assert candidate is None
        assert "标题不够明确" in reason

    def test_high_score_tv_candidate_without_title_evidence_requires_review(self, sample_target):
        """分数再高也不能代替标题证据，否则整部作品会套错元数据。"""
        wrong_candidate = ScrapeCandidate(
            candidate_id="c_wrong_high",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=102086,
            tmdb_type="tv",
            title="被神捡到的男人",
            original_title="神達に拾われた男",
            year=2019,
            score=95,
            popularity=50,
            vote_average=8.0,
            reasons=["类型匹配", "高评分(8.0)"],
        )
        sample_target.scrape_title = "君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note"
        sample_target.local_title = sample_target.scrape_title
        sample_target.series_group = sample_target.scrape_title
        sample_target.scrape_year = 2019
        sample_target.local_year = 2019

        candidate, reason = decide_auto_candidate(sample_target, [wrong_candidate], threshold=70)

        assert candidate is None
        assert "标题不够明确" in reason

    def test_needs_review_movie_unique_adopt_with_strong_title(self, sample_target, high_score_candidate):
        """needs_review 的 movie target 只有强标题证据时才自动采用"""
        sample_target.needs_review = True
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        high_score_candidate.tmdb_type = "movie"
        candidate, reason = decide_auto_candidate(
            sample_target, [high_score_candidate], threshold=70
        )
        assert candidate is high_score_candidate
        assert "自动采用" in reason

    def test_movie_unique_candidate_without_title_identity_goes_to_review(self, sample_target):
        """电影唯一候选的年份和热度不能代替作品身份。"""
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "刀剑神域 剧场版：序列之争"
        sample_target.local_title = "刀剑神域 剧场版：序列之争"
        sample_target.scrape_year = 2017
        candidate = ScrapeCandidate(
            candidate_id="c_sao",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=1,
            tmdb_type="movie",
            title="刀剑神域",
            original_title="Sword Art Online",
            year=2017,
            score=80,
            popularity=100,
            vote_average=8.0,
            reasons=["年份匹配", "高热度(100)"],
        )

        adopted, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)
        assert adopted is None
        assert "不匹配" in reason or "标题不够明确" in reason

    def test_movie_unique_unrelated_candidate_is_rejected_even_with_popular_title(self, sample_target):
        """唯一候选不能把其他作品写进当前电影目录。"""
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_year = 2021
        candidate = ScrapeCandidate(
            candidate_id="c_pancreas",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=504253,
            tmdb_type="movie",
            title="我想吃掉你的胰脏",
            original_title="君の膵臓をたべたい",
            year=2018,
            score=8,
            popularity=8,
            vote_average=8.0,
            reasons=[],
        )

        adopted, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)
        assert adopted is None
        assert "不匹配" in reason or "标题不够明确" in reason

    def test_unique_movie_candidate_survives_detail_title_language_mismatch(self, sample_target):
        """唯一候选已匹配时，不应因详情中文名和本地罗马音相似度低而进人工队列"""
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "Yuru Camp Movie"
        sample_target.local_title = "Yuru Camp Movie"
        candidate = ScrapeCandidate(
            candidate_id="c_yuru_movie",
            scrape_target_id="t1",
            provider="anilist",
            tmdb_id=566466,
            tmdb_type="movie",
            title="Yuru Camp△ Movie",
            original_title="映画 ゆるキャン△",
            year=2022,
            score=72,
            popularity=38618,
            vote_average=8.3,
            reasons=["AniList 命中(104460)", "标题高度相似"],
            raw={
                "provider_title_aliases": ["Yuru Camp Movie", "映画 ゆるキャン△", "摇曳露营 剧场版"],
                "provider_tmdb_link": "direct",
            },
        )
        blockers = [
            ScrapeValidationIssue(
                code="title_low_similarity",
                level="block",
                message="标题相似度很低: 本地「Yuru Camp Movie」/ 刮削「摇曳露营△  剧场版」",
            )
        ]

        remaining = _blocking_issues_after_candidate_evidence(
            sample_target,
            candidate,
            [candidate],
            blockers,
        )

        assert remaining == []

    def test_multi_movie_candidates_require_identity_not_just_highest_score(self, sample_target):
        """多候选的最高分也必须先证明作品身份。"""
        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_year = 1992
        wrong = ScrapeCandidate(
            candidate_id="c_wrong",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=1,
            tmdb_type="movie",
            title="Traces of Red",
            year=1992,
            score=30,
        )
        best = ScrapeCandidate(
            candidate_id="c_best",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=11621,
            tmdb_type="movie",
            title="红猪",
            original_title="紅の豚",
            year=1992,
            score=49,
        )

        adopted, reason = decide_auto_candidate(sample_target, [wrong, best], threshold=70)
        assert adopted is None
        assert "标题不够明确" in reason

    def test_needs_review_tv_exact_title_auto_adopt(self, sample_target):
        """缺年份但标题完全匹配的 TV 不应要求人工确认"""
        sample_target.needs_review = True
        sample_target.scrape_year = None
        sample_target.local_year = None
        sample_target.scrape_title = "One Room"
        sample_target.local_title = "One Room"
        sample_target.series_group = "One Room"
        candidate = ScrapeCandidate(
            candidate_id="c_one_room",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=69298,
            tmdb_type="tv",
            title="One Room",
            original_title="One Room",
            year=2017,
            score=42,
            popularity=4.8,
            vote_average=5.7,
            reasons=["标题完全匹配"],
        )

        adopted, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)
        assert adopted is candidate
        assert "自动采用" in reason

    def test_needs_review_tv_unique_quality_auto_adopt(self, sample_target):
        """罗马音标题搜到唯一高质量 TV 候选时自动采用"""
        sample_target.needs_review = True
        sample_target.scrape_title = "Hyouka"
        sample_target.scrape_year = None
        sample_target.local_year = None
        candidate = ScrapeCandidate(
            candidate_id="c_hyouka",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=65329,
            tmdb_type="tv",
            title="冰菓",
            original_title="氷菓",
            year=2012,
            score=8.8,
            popularity=18.6,
            vote_average=8.1,
            reasons=["高评分(8.1)"],
            raw={
                "provider_title_aliases": ["Hyouka", "氷菓", "冰菓"],
                "provider_tmdb_link": "direct",
            },
        )

        adopted, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)
        assert adopted is candidate
        assert "自动采用" in reason

    def test_romanized_unique_tv_low_score_auto_adopt(self, sample_target):
        """罗马音标题唯一 TV 候选分数低，也应贴近手动搜索直接采用"""
        sample_target.scrape_title = "Seihantai na Kimi to Boku"
        sample_target.local_title = "Seihantai na Kimi to Boku"
        sample_target.scrape_year = None
        sample_target.local_year = None
        sample_target.needs_review = True
        sample_target.warnings = ["缺少年份"]

        candidate = ScrapeCandidate(
            candidate_id="c_seihantai",
            scrape_target_id="t1",
            provider="anilist",
            tmdb_id=280366,
            tmdb_type="tv",
            title="正相反的你与我",
            original_title="正反対な君と僕",
            year=2026,
            score=8.4,
            popularity=2,
            vote_average=8.4,
            reasons=["高评分(8.4)"],
            raw={
                "provider_title_aliases": ["Seihantai na Kimi to Boku", "正反対な君と僕", "正相反的你与我"],
                "provider_tmdb_link": "direct",
            },
        )

        adopted, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)
        assert adopted is candidate
        assert "唯一候选" in reason

    def test_tv_sequel_year_mismatch_is_not_strict_conflict(self, sample_target):
        """后续季匹配同一个 TMDB show 时，show 首播年份不能拦住自动采用"""
        sample_target.series_group = "刀剑神域"
        sample_target.local_title = "刀剑神域"
        sample_target.scrape_title = "刀剑神域"
        sample_target.local_season_number = 4
        sample_target.scrape_year = 2019
        sample_target.local_year = 2019
        candidate = ScrapeCandidate(
            candidate_id="c_sao",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=45782,
            tmdb_type="tv",
            title="刀剑神域",
            original_title="ソードアート・オンライン",
            year=2012,
            score=70,
            popularity=80,
            vote_average=8.2,
            reasons=["标题完全匹配"],
        )

        adopted, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)
        assert adopted is candidate
        assert "唯一候选" in reason

    def test_needs_review_generic_title_still_reject(self, sample_target):
        """Season 1 这类脏标题不能因为有候选就自动采用"""
        sample_target.needs_review = True
        sample_target.scrape_title = "Season 1"
        sample_target.scrape_year = None
        candidate = ScrapeCandidate(
            candidate_id="c_bad",
            scrape_target_id="t1",
            provider="tmdb",
            tmdb_id=1,
            tmdb_type="tv",
            title="Monster Class Season 1",
            original_title="Monster Class Season 1",
            year=2023,
            score=25,
            popularity=10,
            vote_average=8,
            reasons=["标题部分匹配"],
        )

        adopted, reason = decide_auto_candidate(sample_target, [candidate], threshold=70)
        assert adopted is None
        assert "人工确认" in reason

    def test_no_candidates_reject(self, sample_target):
        """无候选应拒绝"""
        candidate, reason = decide_auto_candidate(sample_target, [], threshold=70)
        assert candidate is None
        assert "无候选" in reason

    def test_type_mismatch_reject(self, sample_target, high_score_candidate):
        """类型不匹配应拒绝"""
        high_score_candidate.tmdb_type = "movie"
        candidate, reason = decide_auto_candidate(
            sample_target, [high_score_candidate], threshold=70
        )
        assert candidate is None
        assert "类型不匹配" in reason

    def test_year_diff_too_large(self, sample_target, high_score_candidate):
        """年份差距过大应拒绝"""
        high_score_candidate.year = 2020
        candidate, reason = decide_auto_candidate(
            sample_target, [high_score_candidate], threshold=70
        )
        assert candidate is None
        assert "年份不一致" in reason

    def test_missing_year_unique_candidate_adopt(self, sample_target, high_score_candidate):
        """缺少年份但只有一个候选时自动采用，减少无意义确认"""
        sample_target.warnings = ["缺少年份"]
        high_score_candidate.reasons = ["标题部分匹配"]
        candidate, reason = decide_auto_candidate(
            sample_target, [high_score_candidate], threshold=70
        )
        assert candidate is high_score_candidate
        assert "唯一候选" in reason

    def test_subwork_fallback_keeps_local_season_number(self, sample_target):
        """无 TMDB 上下文时只做保守回退，保留本地季号"""
        sample_target.series_group = "CLANNAD"
        sample_target.local_title = "CLANNAD"
        sample_target.scrape_title = "CLANNAD After Story"
        sample_target.source_subwork_dir = "2.CLANNAD.After.Story.2008"
        sample_target.local_season_number = 2

        assert infer_auto_tmdb_season_number(sample_target) == 2

    def test_resolve_season_uses_existing_local_tmdb_season(self, sample_target):
        """CLANNAD After Story 在同一 TMDB 条目下存在 Season 2 时，应绑定 S2"""
        sample_target.series_group = "CLANNAD"
        sample_target.local_title = "CLANNAD"
        sample_target.scrape_title = "CLANNAD After Story"
        sample_target.source_subwork_dir = "2.CLANNAD.After.Story.2008"
        sample_target.local_season_number = 2

        client = MagicMock()
        client.get_tv_detail.return_value = {
            "seasons": [
                {"season_number": 1, "name": "CLANNAD", "episode_count": 22},
                {"season_number": 2, "name": "CLANNAD 〜AFTER STORY〜", "episode_count": 22},
            ]
        }

        assert resolve_tmdb_season_number(sample_target, 24835, "tv", tmdb_client=client) == 2

    def test_resolve_season_falls_back_to_one_for_separate_sequel_show(self, sample_target):
        """独立续作条目只有 Season 1 时，本地 S2 应绑定该条目的 S1"""
        sample_target.series_group = "Some Series"
        sample_target.local_title = "Some Series"
        sample_target.scrape_title = "Some Series Sequel"
        sample_target.source_subwork_dir = "2.Some.Series.Sequel.2008"
        sample_target.local_season_number = 2

        client = MagicMock()
        client.get_tv_detail.return_value = {
            "seasons": [
                {"season_number": 1, "name": "Season 1", "episode_count": 12},
            ]
        }

        assert resolve_tmdb_season_number(sample_target, 123, "tv", tmdb_client=client) == 1

    def test_resolve_strong_hinted_plain_main_series_later_season_keeps_local_number(self, sample_target):
        """普通主系列多季有父级 TMDB 强绑定时，不能把 S2/S3 静默降回 TMDB S1。"""
        sample_target.series_group = "Re：从零开始的异世界生活"
        sample_target.local_title = "Re：从零开始的异世界生活"
        sample_target.scrape_title = "Re：从零开始的异世界生活"
        sample_target.source_subwork_dir = ""
        sample_target.local_season_number = 2
        sample_target.tmdb_hint_id = 65942
        sample_target.tmdb_hint_type = "tv"

        client = MagicMock()
        client.get_tv_detail.return_value = {
            "seasons": [
                {"season_number": 1, "name": "Season 1", "episode_count": 25},
            ]
        }

        assert resolve_tmdb_season_number(sample_target, 65942, "tv", tmdb_client=client) == 2

    def test_resolve_absolute_numbered_tmdb_show_maps_later_local_season_to_one(self, sample_target, monkeypatch):
        """TMDB 只有一个正片 Season 1 且集数覆盖本地多季时，后续季应映射到 S1 绝对集数。"""
        sample_target.series_group = "Re：从零开始的异世界生活"
        sample_target.local_title = "Re：从零开始的异世界生活"
        sample_target.scrape_title = "Re：从零开始的异世界生活"
        sample_target.source_subwork_dir = ""
        sample_target.local_season_number = 2
        sample_target.local_episode_count = 25
        sample_target.tmdb_hint_id = 65942
        sample_target.tmdb_hint_type = "tv"

        plan = ImportPlan(plan_id="p1", items=[
            ImportPlanItem(
                id=f"s{season}e{episode}",
                action="generate_strm",
                group_type="season",
                season_number=season,
                episode_number=episode,
                series_group=sample_target.series_group,
                work_title=sample_target.local_title,
                work_id=sample_target.work_id,
            )
            for season in (1, 2)
            for episode in range(1, 26)
        ])
        monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id=None, source=None: plan)

        client = MagicMock()
        client.get_tv_detail.return_value = {
            "id": 65942,
            "seasons": [
                {"season_number": 0, "name": "Specials", "episode_count": 77},
                {"season_number": 1, "name": "Season 1", "episode_count": 85},
            ],
        }

        assert resolve_tmdb_season_number(sample_target, 65942, "tv", tmdb_client=client) == 1

    def test_manual_select_uses_existing_local_tmdb_season(self, sample_target, monkeypatch):
        """手动选择候选时也必须按选中 TMDB 条目的 seasons 推断"""
        sample_target.series_group = "CLANNAD"
        sample_target.local_title = "CLANNAD"
        sample_target.scrape_title = "CLANNAD After Story"
        sample_target.source_subwork_dir = "2.CLANNAD.After.Story.2008"
        sample_target.local_season_number = 2

        monkeypatch.setattr(
            "app.scrape.service.resolve_tmdb_season_number",
            lambda **kwargs: 2,
        )

        assert _resolve_tmdb_season_number(sample_target, 24835, None, "tv") == 2

    def test_normal_series_keeps_local_season_number(self, sample_target):
        """普通同一 TV 条目的多季，自动刮削保留本地季号"""
        sample_target.series_group = "冰海战记"
        sample_target.local_title = "冰海战记"
        sample_target.scrape_title = "冰海战记"
        sample_target.source_subwork_dir = ""
        sample_target.local_season_number = 2

        assert infer_auto_tmdb_season_number(sample_target) == 2


# ============================================================
# review_queue 测试
# ============================================================

class TestReviewQueue:
    """测试 review queue"""

    def test_add_to_queue(self, tmp_path, monkeypatch, sample_target, low_score_candidate):
        """低分候选应进入 review queue"""
        monkeypatch.setattr("app.scrape.review_queue._get_queue_path", lambda: tmp_path / "queue.json")

        from app.scrape.review_queue import add_to_review_queue, get_pending_review_items

        add_to_review_queue(
            target=sample_target,
            reason="分数不足",
            candidates=[low_score_candidate],
        )

        items = get_pending_review_items()
        assert len(items) == 1
        assert items[0].scrape_target_id == "t1"
        assert items[0].reason == "分数不足"
        assert len(items[0].candidates) == 1
        assert items[0].candidates[0]["provider"] == low_score_candidate.provider
        assert items[0].candidates[0]["poster_path"] == low_score_candidate.poster_path

    def test_no_duplicate_in_queue(self, tmp_path, monkeypatch, sample_target, low_score_candidate):
        """同一 target 不应重复添加"""
        monkeypatch.setattr("app.scrape.review_queue._get_queue_path", lambda: tmp_path / "queue.json")

        from app.scrape.review_queue import add_to_review_queue, get_pending_review_items

        add_to_review_queue(sample_target, "reason1", [low_score_candidate])
        add_to_review_queue(sample_target, "reason2", [low_score_candidate])

        items = get_pending_review_items()
        assert len(items) == 1
        assert items[0].reason == "reason2"  # 应更新为最新原因


class TestSearchVariants:
    """搜索标题变体测试"""

    def test_single_letter_prefix_fallback_search(self, sample_target):
        """B 86-不存在的战区 主查询 0 个时，应尝试清洗后的标题"""
        from app.scrape.service import search_candidates

        sample_target.scrape_title = "B 86-不存在的战区"
        sample_target.scrape_year = 2021
        calls = []

        class FakeClient:
            def search_tv(self, query, year=None):
                calls.append((query, year))
                if query == "86 不存在的战区":
                    return [{
                        "id": 136283,
                        "name": "86 不存在的战区",
                        "original_name": "８６―エイティシックス―",
                        "first_air_date": "2021-04-11",
                        "popularity": 80,
                        "vote_average": 8.0,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].title == "86 不存在的战区"
        assert candidates[0].score >= 70
        assert ("B 86-不存在的战区", 2021) in calls
        assert ("86 不存在的战区", 2021) in calls

    def test_search_fallback_without_year(self, sample_target):
        """带年份 0 候选时，应自动用同标题不带年份再搜"""
        from app.scrape.service import search_candidates

        sample_target.scrape_title = "CLANNAD After Story"
        sample_target.scrape_year = 2008
        calls = []

        class FakeClient:
            def search_tv(self, query, year=None):
                calls.append((query, year))
                if query == "CLANNAD After Story" and year is None:
                    return [{
                        "id": 46457,
                        "name": "CLANNAD",
                        "original_name": "CLANNAD 〜AFTER STORY〜",
                        "first_air_date": "2008-10-03",
                        "popularity": 20,
                        "vote_average": 8.3,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert calls[:2] == [("CLANNAD After Story", 2008), ("CLANNAD After Story", None)]

    def test_clannad_after_story_falls_back_to_series_title(self, sample_target):
        """After Story 子标题搜不到时，应继续尝试主标题 CLANNAD"""
        from app.scrape.service import search_candidates

        sample_target.scrape_title = "CLANNAD 〜AFTER STORY〜"
        sample_target.scrape_year = 2008
        calls = []

        class FakeClient:
            def search_tv(self, query, year=None):
                calls.append((query, year))
                if query == "CLANNAD":
                    return [{
                        "id": 24835,
                        "name": "CLANNAD",
                        "original_name": "CLANNAD",
                        "first_air_date": "2007-10-05",
                        "popularity": 28,
                        "vote_average": 8.3,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].tmdb_id == 24835
        assert ("CLANNAD", 2008) in calls or ("CLANNAD", None) in calls

    def test_release_group_brackets_do_not_become_search_queries(self, sample_target):
        """字幕组/压制参数方括号不能被点号拆成 H 这类错误查询"""

        sample_target.scrape_title = "Hyouka"
        sample_target.local_title = "Hyouka"
        sample_target.original_title = "[T.H.X&VCB-Studio] Hyouka [Ma10p_1080p]"

        queries = _build_target_search_title_variants(sample_target, sample_target.scrape_title)

        assert "Hyouka" in queries
        assert "H" not in queries
        assert "[T" not in queries
        assert not any("VCB-Studio" in query or "Ma10p" in query for query in queries)

    def test_release_and_tech_brackets_are_removed_from_search_queries(self, sample_target):
        """多集 WebRip 包应只拿作品名搜索，不拿字幕组和编码信息搜索"""

        sample_target.scrape_title = "Dandadan"
        sample_target.local_title = "Dandadan"
        sample_target.original_title = "[Nekomoe kissaten&LoliHouse] Dandadan [13-24][WebRip 1080p HEVC-10bit AAC]"

        queries = _build_target_search_title_variants(sample_target, sample_target.scrape_title)

        assert "Dandadan" in queries
        assert "Nekomoe kissaten&LoliHouse" not in queries
        assert "WebRip 1080p HEVC-10bit AAC" not in queries
        assert not any("WebRip" in query or "HEVC" in query for query in queries)

    def test_dot_separated_alias_title_is_still_searchable(self, sample_target):
        """中文名.英文名 这种有效别名仍然要保留英文合并查询"""

        sample_target.scrape_title = "玲音.Serial.Experiments.Lain"
        sample_target.local_title = "玲音.Serial.Experiments.Lain"
        sample_target.original_title = "玲音.Serial.Experiments.Lain"

        queries = _build_target_search_title_variants(sample_target, sample_target.scrape_title)

        assert "Serial Experiments Lain" in queries
        assert "1998" not in queries

    def test_movie_search_tries_theater_subtitle_variant(self, sample_target):
        """电影自动搜索应学习手动刮削：剧场版冒号后标题也要搜索"""
        from app.scrape.service import search_candidates

        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "钢之炼金术师 FA 剧场版：叹息之丘的圣星"
        sample_target.scrape_year = 2009
        calls = []

        class FakeClient:
            def search_movie(self, query, year=None):
                calls.append((query, year))
                if query == "叹息之丘的圣星":
                    return [{
                        "id": 50160,
                        "title": "钢之炼金术师：叹息之丘的圣星",
                        "original_title": "鋼の錬金術師 嘆きの丘（ミロス）の聖なる星",
                        "release_date": "2011-07-02",
                        "popularity": 18,
                        "vote_average": 6.8,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].tmdb_id == 50160
        assert ("叹息之丘的圣星", 2009) in calls

    def test_movie_search_continues_after_generic_candidate(self, sample_target):
        """电影不能因为完整标题先搜到泛候选就停止，应继续搜副标题"""
        from app.scrape.service import search_candidates

        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "魔法禁书目录剧场版：恩底弥翁的奇迹"
        sample_target.scrape_year = 2013
        calls = []

        class FakeClient:
            def search_movie(self, query, year=None):
                calls.append((query, year))
                if query == "魔法禁书目录剧场版：恩底弥翁的奇迹":
                    return [{
                        "id": 1,
                        "title": "魔法禁书目录",
                        "original_title": "とある魔術の禁書目録",
                        "release_date": "2013-01-01",
                        "popularity": 40,
                        "vote_average": 7.0,
                    }]
                if query == "恩底弥翁的奇迹":
                    return [{
                        "id": 246655,
                        "title": "魔法禁书目录 剧场版 恩底弥翁的奇迹",
                        "original_title": "劇場版 とある魔術の禁書目録 -エンデュミオンの奇蹟-",
                        "release_date": "2013-02-23",
                        "popularity": 20,
                        "vote_average": 7.1,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates[0].tmdb_id == 246655
        assert ("魔法禁书目录剧场版：恩底弥翁的奇迹", 2013) in calls
        assert ("恩底弥翁的奇迹", 2013) in calls

    def test_movie_search_strips_compilation_descriptor(self, sample_target):
        """总集篇这类说明词应从电影搜索变体里去掉"""
        from app.scrape.service import search_candidates

        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "刀剑神域 Extra Edition 总集篇"
        sample_target.scrape_year = 2013
        calls = []

        class FakeClient:
            def search_movie(self, query, year=None):
                calls.append((query, year))
                if query == "刀剑神域 Extra Edition":
                    return [{
                        "id": 162542,
                        "title": "刀剑神域 Extra Edition",
                        "original_title": "ソードアート・オンライン Extra Edition",
                        "release_date": "2013-12-31",
                        "popularity": 12,
                        "vote_average": 5.8,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].tmdb_id == 162542
        assert ("刀剑神域 Extra Edition", 2013) in calls

    def test_movie_search_tries_dotted_english_title(self, sample_target):
        """红猪.Porco.Rosso 这类双语点号标题要能回退到 Porco Rosso"""
        from app.scrape.service import search_candidates

        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "红猪.Porco.Rosso"
        sample_target.scrape_year = 1992
        calls = []

        class FakeClient:
            def search_movie(self, query, year=None):
                calls.append((query, year))
                if query == "Porco Rosso":
                    return [{
                        "id": 11621,
                        "title": "红猪",
                        "original_title": "紅の豚",
                        "release_date": "1992-07-18",
                        "popularity": 20,
                        "vote_average": 7.8,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].tmdb_id == 11621
        assert ("Porco Rosso", 1992) in calls

    def test_tv_search_falls_back_to_series_group_for_sequel(self, sample_target):
        """第四季子标题搜不到时，应回退主系列名再让 season resolver 决定季号"""
        from app.scrape.service import search_candidates

        sample_target.scrape_title = "刀剑神域Alicization篇 War of Underworld"
        sample_target.series_group = "刀剑神域"
        sample_target.scrape_year = 2019
        sample_target.local_season_number = 4
        calls = []

        class FakeClient:
            def search_tv(self, query, year=None):
                calls.append((query, year))
                if query == "刀剑神域" and year is None:
                    return [{
                        "id": 45782,
                        "name": "刀剑神域",
                        "original_name": "ソードアート・オンライン",
                        "first_air_date": "2012-07-08",
                        "popularity": 80,
                        "vote_average": 8.2,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].tmdb_id == 45782
        assert ("刀剑神域", None) in calls

    def test_standalone_spin_off_does_not_search_parent_series_group(self, sample_target):
        """Heya Camp 这类外传独立卡片不能回退搜索父系列 Yuru Camp。"""
        from app.scrape.service import build_candidate_search_queries, search_candidates

        sample_target.card_type = "standalone"
        sample_target.scrape_title = "Heya Camp"
        sample_target.local_title = "Heya Camp"
        sample_target.source_subwork_dir = "[VCB-Studio] Heya Camp"
        sample_target.series_group = "Yuru Camp"
        sample_target.scrape_year = 2020
        calls = []

        class FakeClient:
            def search_tv(self, query, year=None):
                calls.append((query, year))
                if query == "Yuru Camp":
                    return [{
                        "id": 76075,
                        "name": "Yuru Camp",
                        "original_name": "ゆるキャン△",
                        "first_air_date": "2018-01-04",
                        "popularity": 80,
                        "vote_average": 8.2,
                    }]
                return []

        queries = build_candidate_search_queries(sample_target)
        candidates = search_candidates(sample_target, tmdb_client=FakeClient(), anilist_client=False, bangumi_client=False)

        assert "Heya Camp" in queries
        assert "Yuru Camp" not in queries
        assert not candidates
        assert all(call[0] != "Yuru Camp" for call in calls)

    def test_bangumi_search_result_resolves_back_to_tmdb_candidate(self, sample_target):
        """手动搜索可用 Bangumi 辅助找名，再转回 TMDB 候选用于实际刮削。"""
        from app.scrape.service import search_candidates

        sample_target.card_type = "standalone"
        sample_target.scrape_title = "Heya Camp"
        sample_target.local_title = "Heya Camp"
        sample_target.series_group = "Yuru Camp"
        sample_target.scrape_year = 2020

        class FakeTMDBClient:
            def search_tv(self, query, year=None):
                if query == "へやキャン△":
                    return [{
                        "id": 93893,
                        "name": "房间露营△",
                        "original_name": "へやキャン△",
                        "first_air_date": "2020-01-06",
                        "popularity": 20,
                        "vote_average": 7.0,
                    }]
                return []

        class FakeBangumiClient:
            def search_subjects(self, keyword, limit=10, offset=0, subject_types=None):
                assert keyword == "Heya Camp"
                return {"data": [{
                    "id": 300000,
                    "type": 2,
                    "name": "へやキャン△",
                    "name_cn": "房间露营△",
                    "date": "2020-01-06",
                    "summary": "短篇外传",
                    "eps": 12,
                    "rating": {"score": 6.8},
                }]}

        candidates = search_candidates(
            sample_target,
            tmdb_client=FakeTMDBClient(),
            anilist_client=False,
            bangumi_client=FakeBangumiClient(),
        )

        assert candidates
        assert candidates[0].provider == "bangumi"
        assert candidates[0].tmdb_id == 93893
        assert "Bangumi 命中(300000)" in candidates[0].reasons[0]

    def test_movie_scoring_prefers_matching_latter_part(self, sample_target):
        """后篇搜索结果应压过同系列前篇和泛电影标题"""
        from app.scrape.service import search_candidates

        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "BanG Dream! It's MyGO!!!!! 后篇：歌唱着，由我们所作的歌 & FILM LIVE"
        sample_target.scrape_year = 2024

        class FakeClient:
            def search_movie(self, query, year=None):
                return [
                    {
                        "id": 1,
                        "title": "迷途之子!!!!! 前篇：春暖向阳，迷星之猫",
                        "original_title": "BanG Dream! It's MyGO!!!!! 前編：春の陽だまり、迷い猫",
                        "release_date": "2024-01-01",
                        "popularity": 20,
                        "vote_average": 10.0,
                    },
                    {
                        "id": 2,
                        "title": "Film Is Dead. Long Live Film!",
                        "original_title": "Film Is Dead. Long Live Film!",
                        "release_date": "2024-01-01",
                        "popularity": 20,
                        "vote_average": 8.7,
                    },
                    {
                        "id": 3,
                        "title": "迷途之子!!!!! 后篇：唱吧，成为我们羁绊的诗歌 & 电影演唱会",
                        "original_title": "BanG Dream! It's MyGO!!!!! 後編：うたう、僕らになれるうた & FILM LIVE",
                        "release_date": "2024-01-01",
                        "popularity": 10,
                        "vote_average": 0,
                    },
                ]

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates[0].tmdb_id == 3
        front = next(c for c in candidates if c.tmdb_id == 1)
        back = next(c for c in candidates if c.tmdb_id == 3)
        assert back.score > front.score

    def test_movie_scoring_distinguishes_bocchi_re_and_rere_parts(self, sample_target):
        from app.scrape.service import _movie_part_marker_score

        front_score, front_reason = _movie_part_marker_score(
            "剧场总集篇 孤独摇滚！Re-",
            "孤独摇滚 (上)",
            "劇場総集編ぼっち・ざ・ろっく！Re:",
        )
        wrong_score, wrong_reason = _movie_part_marker_score(
            "剧场总集篇 孤独摇滚！Re-",
            "孤独摇滚 (下)",
            "劇場総集編ぼっち・ざ・ろっく！Re:Re:",
        )

        assert front_score > 0
        assert front_reason == "篇章匹配"
        assert wrong_score < 0
        assert wrong_reason == "篇章不匹配"

    def test_title_prefix_match_beats_year_only_match(self, sample_target):
        """完整标题搜索时，候选标题是搜索词前缀应优先于单纯年份吻合。"""
        from app.scrape.service import search_candidates

        sample_target.scrape_type = "tv"
        sample_target.media_type = "tv"
        sample_target.group_type = "special"
        sample_target.scrape_title = "莉可丽丝：友谊是时间的窃贼"
        sample_target.local_title = "莉可丽丝：友谊是时间的窃贼"
        sample_target.scrape_year = 2008

        class FakeClient:
            def search_tv(self, query, year=None):
                return [
                    {
                        "id": 300000,
                        "name": "新安琪莉可",
                        "original_name": "Neo Angelique Abyss",
                        "first_air_date": "2008-04-06",
                        "media_type": "tv",
                        "popularity": 120,
                        "vote_average": 7.2,
                    },
                    {
                        "id": 154494,
                        "name": "莉可丽丝",
                        "original_name": "Lycoris Recoil",
                        "first_air_date": "2022-07-02",
                        "media_type": "tv",
                        "popularity": 30,
                        "vote_average": 8.1,
                    },
                ]

        candidates = search_candidates(sample_target, tmdb_client=FakeClient(), anilist_client=False, bangumi_client=False)

        assert candidates[0].tmdb_id == 154494
        prefix = next(c for c in candidates if c.tmdb_id == 154494)
        year_only = next(c for c in candidates if c.tmdb_id == 300000)
        assert prefix.score > year_only.score
        assert "候选标题前缀匹配" in prefix.reasons

    def test_auto_search_prefers_manual_success_query(self, tmp_path, monkeypatch, sample_target):
        """手动刮削成功的搜索词应优先用于后续自动搜索"""
        from app.scrape.models import ScrapeMapItem
        from app.scrape.service import search_candidates
        from app.scrape.store import upsert_scrape_map_item

        monkeypatch.setattr("app.scrape.store._get_scrape_dir", lambda: tmp_path)

        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "原始失败标题"
        sample_target.local_title = "原始失败标题"
        sample_target.source_subwork_dir = "动画电影/原始失败标题"
        sample_target.scrape_year = 2024

        upsert_scrape_map_item(ScrapeMapItem(
            scrape_target_id=sample_target.scrape_target_id,
            source=sample_target.source,
            source_subwork_dir=sample_target.source_subwork_dir,
            local_title=sample_target.local_title,
            search_query="手动成功标题",
            tmdb_id=12345,
            tmdb_type="movie",
            selected_by="manual",
        ))

        calls = []

        class FakeClient:
            def search_movie(self, query, year=None):
                calls.append((query, year))
                if query == "手动成功标题":
                    return [{
                        "id": 12345,
                        "title": "手动成功标题",
                        "original_title": "Manual Success Title",
                        "release_date": "2024-01-01",
                        "popularity": 10,
                        "vote_average": 7.5,
                    }]
                return []

        candidates = search_candidates(sample_target, tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].tmdb_id == 12345
        assert calls[0] == ("手动成功标题", 2024)

    def test_manual_search_query_still_uses_learned_fallback(self, tmp_path, monkeypatch, sample_target):
        """手动输入的词无结果时，也要继续尝试历史手动成功词。"""
        from app.scrape.models import ScrapeMapItem
        from app.scrape.service import search_candidates
        from app.scrape.store import upsert_scrape_map_item

        monkeypatch.setattr("app.scrape.store._get_scrape_dir", lambda: tmp_path)

        sample_target.scrape_type = "movie"
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_title = "原始失败标题"
        sample_target.local_title = "原始失败标题"
        sample_target.source_subwork_dir = "动画电影/原始失败标题"
        sample_target.scrape_year = 2024

        upsert_scrape_map_item(ScrapeMapItem(
            scrape_target_id=sample_target.scrape_target_id,
            source=sample_target.source,
            source_subwork_dir=sample_target.source_subwork_dir,
            local_title=sample_target.local_title,
            search_query="手动成功标题",
            tmdb_id=12345,
            tmdb_type="movie",
            selected_by="manual",
        ))

        calls = []

        class FakeClient:
            def search_movie(self, query, year=None):
                calls.append((query, year))
                if query == "手动成功标题":
                    return [{
                        "id": 12345,
                        "title": "手动成功标题",
                        "original_title": "Manual Success Title",
                        "release_date": "2024-01-01",
                        "popularity": 10,
                        "vote_average": 7.5,
                    }]
                return []

        candidates = search_candidates(sample_target, query="用户输入但搜不到", tmdb_client=FakeClient())
        assert candidates
        assert candidates[0].tmdb_id == 12345
        assert ("用户输入但搜不到", 2024) in calls
        assert ("手动成功标题", 2024) in calls


class TestAutoScrapeIncremental:
    """自动刮削增量跳过测试"""

    def test_existing_scrape_map_and_nfo_skip_search(self, sample_target, tmp_path, monkeypatch):
        """已有 ScrapeMap + NFO 时，全量自动刮削不应重复搜索"""
        from app.scrape.models import ScrapeMap, ScrapeMapItem

        nfo = tmp_path / "tvshow.nfo"
        nfo.write_text("<tvshow />", encoding="utf-8")
        (tmp_path / "poster.jpg").write_bytes(b"poster")
        (tmp_path / "fanart.jpg").write_bytes(b"fanart")
        sample_target.target_dir = str(tmp_path)
        sample_target.target_nfo_path = str(nfo)

        monkeypatch.setattr(
            "app.scrape.auto.load_scrape_map",
            lambda: ScrapeMap(items=[
                ScrapeMapItem(
                    scrape_target_id=sample_target.scrape_target_id,
                    source=sample_target.source,
                    tmdb_id=24835,
                    tmdb_type="tv",
                    local_season_number=1,
                    nfo_path=str(nfo),
                )
            ]),
        )
        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )

        class FailIfSearched:
            def search_tv(self, query, year=None):
                raise AssertionError("existing target should not be searched")

        result = run_auto_scrape("pan115", tmdb_client=FailIfSearched())
        assert result["auto_scraped"] == 0
        assert result["skipped_existing"] == 1
        assert result["results"][0]["status"] == "skipped"

    def test_transient_search_failure_is_retried_later(self, sample_target, high_score_candidate, monkeypatch):
        """外部服务临时失败时，应先跳过并在本轮末尾自动重试。"""
        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: type("M", (), {"items": []})())
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.auto.execute_scrape", lambda **kwargs: {"ok": True})
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )

        calls = {"count": 0}

        def flaky_search(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("TMDB 请求超时")
            return [high_score_candidate]

        monkeypatch.setattr("app.scrape.auto.search_candidates", flaky_search)

        result = run_auto_scrape("pan115")

        assert calls["count"] == 2
        assert result["auto_scraped"] == 1
        assert result["failed"] == 0
        assert result["total_targets"] == 1
        assert result["completed_targets"] == 1
        assert result["remaining_targets"] == 0
        assert any(item["status"] == "retry_scheduled" for item in result["results"])
        assert any(item["status"] == "auto_scraped" for item in result["results"])

    def test_successful_batch_does_not_finish_with_source_rescan(self, sample_target, high_score_candidate, monkeypatch):
        """作品已逐个局部发布后，整批结束不得再做来源级重扫。"""
        from app.scrape.models import ScrapeMap

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: ScrapeMap(items=[]))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr("app.scrape.auto.search_candidates", lambda *args, **kwargs: [high_score_candidate])
        monkeypatch.setattr("app.scrape.auto.execute_scrape", lambda **kwargs: {"ok": True})
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )
        monkeypatch.setattr(
            "app.import_plan.store.load_import_plan",
            lambda plan_id=None, source=None: ImportPlan(
                plan_id=plan_id or "p1",
                source="pan115",
                status="confirmed",
            ),
        )
        rescans = []
        monkeypatch.setattr(
            "app.library.service.rescan_library",
            lambda source=None: rescans.append(source) or {
                "mode": "source_rescan",
                "work_count": 1,
                "warnings": [],
            },
        )

        run_auto_scrape("pan115", tmdb_client=object())

        assert rescans == []

    def test_later_season_reuses_first_season_scrape_from_same_batch(self, sample_target, high_score_candidate, monkeypatch):
        """同一轮刚刮完第一季后，第二季应复用 TMDB ID，不再重新搜索。"""
        from app.scrape.models import ScrapeMap, ScrapeMapItem

        season1 = sample_target
        season1.scrape_target_id = "s1"
        season1.series_group = "刀剑神域"
        season1.local_title = "刀剑神域"
        season1.scrape_title = "刀剑神域"
        season1.local_season_number = 1

        season2 = ScrapeTarget(
            scrape_target_id="s2",
            source=season1.source,
            import_plan_id=season1.import_plan_id,
            work_id="w2",
            card_type="main_series",
            media_type="tv",
            group_type="season",
            series_group="刀剑神域",
            local_title="刀剑神域",
            scrape_title="刀剑神域",
            scrape_year=2014,
            scrape_type="tv",
            local_season_number=2,
            needs_review=False,
            warnings=[],
        )

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([season1, season2], None))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )

        map_calls = {"count": 0}

        def fake_load_scrape_map():
            map_calls["count"] += 1
            if map_calls["count"] == 1:
                return ScrapeMap(items=[])
            return ScrapeMap(items=[
                ScrapeMapItem(
                    scrape_target_id="s1",
                    source="pan115",
                    card_type="main_series",
                    series_group="刀剑神域",
                    local_season_number=1,
                    tmdb_id=45782,
                    tmdb_type="tv",
                )
            ])

        monkeypatch.setattr("app.scrape.auto.load_scrape_map", fake_load_scrape_map)

        search_calls = []

        def fake_search(target, tmdb_client=None):
            search_calls.append(target.local_season_number)
            if target.local_season_number == 2:
                raise AssertionError("second season should reuse first season TMDB id")
            candidate = high_score_candidate
            candidate.scrape_target_id = target.scrape_target_id
            candidate.tmdb_id = 45782
            candidate.tmdb_type = "tv"
            candidate.title = "刀剑神域"
            candidate.score = 90
            return [candidate]

        monkeypatch.setattr("app.scrape.auto.search_candidates", fake_search)

        executed = []

        def fake_execute_scrape(**kwargs):
            executed.append((kwargs["target"].local_season_number, kwargs["tmdb_id"], kwargs["tmdb_season_number"]))
            return {"ok": True}

        monkeypatch.setattr("app.scrape.auto.execute_scrape", fake_execute_scrape)

        class FakeClient:
            def get_tv_detail(self, tmdb_id):
                return {
                    "id": tmdb_id,
                    "name": "刀剑神域",
                    "first_air_date": "2012-07-08",
                    "seasons": [
                        {"season_number": 1, "air_date": "2012-07-08"},
                        {"season_number": 2, "air_date": "2014-07-05"},
                    ],
                }

        result = run_auto_scrape("pan115", tmdb_client=FakeClient())

        assert result["auto_scraped"] == 2
        assert search_calls == [1]
        assert executed == [(1, 45782, 1), (2, 45782, 2)]
        assert map_calls["count"] >= 2

    def test_main_series_special_reuses_confirmed_series_tmdb_id(self, sample_target, monkeypatch):
        """主系列 Season 0 特典应继承已确认剧集 ID，不因标题后缀进人工队列。"""
        from app.scrape.models import ScrapeMap, ScrapeMapItem

        target = ScrapeTarget(
            scrape_target_id="yuru-s0",
            source="local",
            import_plan_id="p-yuru",
            work_id="yuru",
            card_type="main_series",
            media_type="tv",
            show_type="anime_series",
            group_type="special",
            series_group="Yuru Camp",
            local_title="Yuru Camp",
            scrape_title="Yuru Camp Season 2",
            scrape_type="tv",
            local_season_number=0,
            item_ids=["sp1"],
            needs_review=True,
            warnings=["缺少年份"],
        )

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([target], None))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            "app.scrape.auto.load_scrape_map",
            lambda: ScrapeMap(items=[
                ScrapeMapItem(
                    scrape_target_id="heya-s1",
                    source="local",
                    card_type="standalone",
                    series_group="Yuru Camp",
                    local_title="Heya Camp",
                    local_season_number=1,
                    tmdb_id=95213,
                    tmdb_type="tv",
                ),
                ScrapeMapItem(
                    scrape_target_id="yuru-s1",
                    source="local",
                    card_type="main_series",
                    series_group="Yuru Camp",
                    local_season_number=1,
                    tmdb_id=76075,
                    tmdb_type="tv",
                )
            ]),
        )
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )
        monkeypatch.setattr(
            "app.scrape.auto.search_candidates",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("special should reuse series id")),
        )

        executed = []
        monkeypatch.setattr(
            "app.scrape.auto.execute_scrape",
            lambda **kwargs: executed.append((
                kwargs["target"].scrape_target_id,
                kwargs["tmdb_id"],
                kwargs["tmdb_season_number"],
            )) or {"ok": True},
        )

        class FakeClient:
            def get_tv_detail(self, tmdb_id):
                assert tmdb_id == 76075
                return {
                    "id": tmdb_id,
                    "name": "摇曳露营△",
                    "first_air_date": "2018-01-04",
                    "seasons": [
                        {"season_number": 0, "air_date": "2018-03-28"},
                        {"season_number": 1, "air_date": "2018-01-04"},
                        {"season_number": 2, "air_date": "2021-01-07"},
                    ],
                }

        result = run_auto_scrape("local", tmdb_client=FakeClient())

        assert result["auto_scraped"] == 1
        assert result["review_queued"] == 0
        assert executed == [("yuru-s0", 76075, 0)]

    def test_anime_target_rejects_non_animation_tmdb_tv_candidate(self, sample_target, monkeypatch):
        """动画目标不能自动采用缺少 Animation 类型的 TMDB TV 候选。"""
        sample_target.scrape_target_id = "bisque-s2"
        sample_target.series_group = "更衣人偶坠入爱河"
        sample_target.local_title = "更衣人偶坠入爱河"
        sample_target.scrape_title = "更衣人偶坠入爱河"
        sample_target.scrape_year = 2025
        sample_target.local_year = 2025
        sample_target.local_season_number = 2
        sample_target.show_type = "anime_series"

        candidate = ScrapeCandidate(
            candidate_id="wrong-live-tv",
            scrape_target_id=sample_target.scrape_target_id,
            provider="tmdb",
            tmdb_id=263121,
            tmdb_type="tv",
            title="更衣人偶坠入爱河",
            original_title="その着せ替え人形は恋をする",
            year=2024,
            score=82,
            popularity=12,
            vote_average=7.4,
            reasons=["标题完全匹配"],
        )

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: type("M", (), {"items": []})())
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto.search_candidates", lambda *args, **kwargs: [candidate])
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)

        def fail_if_executed(**kwargs):
            raise AssertionError("non-animation TMDB TV candidate should not be scraped")

        monkeypatch.setattr("app.scrape.auto.execute_scrape", fail_if_executed)

        queued = []
        monkeypatch.setattr(
            "app.scrape.review_queue.add_to_review_queue",
            lambda target, reason, candidates: queued.append((target.scrape_target_id, reason, candidates)),
        )

        class FakeClient:
            def get_tv_detail(self, tmdb_id):
                assert tmdb_id == 263121
                return {
                    "id": tmdb_id,
                    "name": "更衣人偶坠入爱河",
                    "genres": [{"id": 18, "name": "Drama"}],
                    "seasons": [{"season_number": 1, "episode_count": 10}],
                }

        result = run_auto_scrape("pan115", tmdb_client=FakeClient())

        assert result["auto_scraped"] == 0
        assert result["review_queued"] == 1
        assert queued
        assert "Animation" in queued[0][1] or "动画" in queued[0][1]

    def test_anime_target_filters_non_animation_candidate_before_adopting_next(self, sample_target, monkeypatch):
        """错误真人候选分数更高时，应先剔除再采用后面的动画候选。"""
        sample_target.scrape_target_id = "bisque-s2-filter"
        sample_target.series_group = "更衣人偶坠入爱河"
        sample_target.local_title = "更衣人偶坠入爱河"
        sample_target.scrape_title = "更衣人偶坠入爱河"
        sample_target.scrape_year = 2025
        sample_target.local_year = 2025
        sample_target.local_season_number = 2
        sample_target.show_type = "anime_series"

        wrong = ScrapeCandidate(
            candidate_id="wrong-live-tv",
            scrape_target_id=sample_target.scrape_target_id,
            provider="tmdb",
            tmdb_id=263121,
            tmdb_type="tv",
            title="更衣人偶坠入爱河",
            year=2024,
            score=95,
            popularity=30,
            raw={"genre_ids": [18]},
        )
        correct = ScrapeCandidate(
            candidate_id="correct-anime-tv",
            scrape_target_id=sample_target.scrape_target_id,
            provider="tmdb",
            tmdb_id=196437,
            tmdb_type="tv",
            title="更衣人偶坠入爱河",
            year=2022,
            score=82,
            popularity=20,
            raw={"genre_ids": [16, 35]},
        )

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: type("M", (), {"items": []})())
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto.search_candidates", lambda *args, **kwargs: [wrong, correct])
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 2)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )

        executed = []
        monkeypatch.setattr(
            "app.scrape.auto.execute_scrape",
            lambda **kwargs: executed.append(kwargs["tmdb_id"]),
        )

        result = run_auto_scrape("pan115", tmdb_client=object())

        assert result["auto_scraped"] == 1
        assert result["review_queued"] == 0
        assert executed == [196437]

    def test_auto_scrape_publishes_each_completed_target_without_full_rescan(self, sample_target, high_score_candidate, monkeypatch):
        """每个成功目标都应局部发布，且批内和批末都不得全量重扫。"""
        target1 = sample_target
        target1.scrape_target_id = "refresh-1"
        target1.work_id = "w-refresh-1"
        target2 = ScrapeTarget(
            scrape_target_id="refresh-2",
            source=target1.source,
            import_plan_id=target1.import_plan_id,
            work_id="w-refresh-2",
            card_type="main_series",
            media_type="tv",
            group_type="season",
            series_group="测试番剧 2",
            local_title="测试番剧 2",
            scrape_title="测试番剧 2",
            scrape_year=2025,
            scrape_type="tv",
            local_season_number=1,
        )

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([target1, target2], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: type("M", (), {"items": []})())
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        refresh_calls = []

        def refresh_targets(targets):
            refresh_calls.append([target.scrape_target_id for target in targets])
            return {"work_count": len(targets), "mode": "partial", "warnings": []}

        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            refresh_targets,
        )

        def fake_search(target, tmdb_client=None):
            candidate = high_score_candidate
            candidate.scrape_target_id = target.scrape_target_id
            candidate.tmdb_id = 9000 + len(target.scrape_target_id)
            candidate.tmdb_type = target.scrape_type
            candidate.title = target.scrape_title
            candidate.year = target.scrape_year
            candidate.score = 90
            return [candidate]

        monkeypatch.setattr("app.scrape.auto.search_candidates", fake_search)
        monkeypatch.setattr("app.scrape.auto.execute_scrape", lambda **kwargs: {"ok": True})

        monkeypatch.setattr(
            "app.import_plan.store.load_import_plan",
            lambda plan_id=None, source=None: ImportPlan(
                plan_id=plan_id or "p1",
                source="pan115",
                status="confirmed",
            ),
        )
        monkeypatch.setattr(
            "app.library.service.rescan_library",
            lambda source=None: (_ for _ in ()).throw(AssertionError("should not rescan after scrape")),
        )
        progress_patches = []

        result = run_auto_scrape(
            "pan115",
            tmdb_client=object(),
            progress_callback=lambda progress, message, patch=None: progress_patches.append(patch or {}),
        )

        assert result["auto_scraped"] == 2
        assert refresh_calls == [
            ["refresh-1"],
            ["refresh-2"],
        ]
        revisions = [
            int(patch["library_refresh_revision"])
            for patch in progress_patches
            if "library_refresh_revision" in patch
        ]
        assert 1 in revisions
        assert 2 in revisions
        assert result["library_refresh"]["mode"] == "partial"
        assert result["library_refresh"]["work_count"] == 1

    def test_auto_scrape_publishes_multi_season_work_only_after_all_targets_complete(self, monkeypatch):
        """同一卡片的多个季度未全部完成前不得重复写入媒体库索引。"""
        items = [
            ImportPlanItem(
                id=f"season-{season}",
                plan_id="multi-season-plan",
                source="pan115",
                relative_path=f"动画库/同一作品/Season {season}/S{season:02d}E01.mkv",
                resource_type="video",
                action="generate_strm",
                work_id=f"raw-season-{season}",
                work_title="同一作品",
                series_group="同一作品",
                card_type="main_series",
                media_type="tv",
                group_type="season",
                season_number=season,
                episode_number=1,
            )
            for season in (1, 2)
        ]
        plan = ImportPlan(
            plan_id="multi-season-plan",
            source="pan115",
            status="confirmed",
            items=items,
        )
        targets = [
            ScrapeTarget(
                scrape_target_id=f"multi-season-{season}",
                source="pan115",
                import_plan_id=plan.plan_id,
                work_id=f"raw-season-{season}",
                card_type="main_series",
                media_type="tv",
                group_type="season",
                series_group="同一作品",
                local_title="同一作品",
                scrape_title="同一作品",
                scrape_type="tv",
                local_season_number=season,
                item_ids=[f"season-{season}"],
            )
            for season in (1, 2)
        ]
        completed = set()
        refresh_calls = []

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: (targets, None))
        monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id=None, source=None: plan)
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: SimpleNamespace(items=[]))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr(
            "app.scrape.auto._target_already_scraped",
            lambda target, *args, **kwargs: target.scrape_target_id in completed,
        )
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "app.scrape.auto.search_candidates",
            lambda target, tmdb_client=None: [ScrapeCandidate(
                candidate_id=f"candidate-{target.scrape_target_id}",
                scrape_target_id=target.scrape_target_id,
                provider="tmdb",
                tmdb_id=100,
                tmdb_type="tv",
                title="同一作品",
                score=100,
            )],
        )

        def execute(**kwargs):
            completed.add(kwargs["target"].scrape_target_id)
            return {"ok": True}

        monkeypatch.setattr("app.scrape.auto.execute_scrape", execute)
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda work_targets: refresh_calls.append(
                [target.scrape_target_id for target in work_targets]
            ) or {"work_count": 1, "mode": "partial", "warnings": []},
        )

        result = run_auto_scrape("pan115", plan_id=plan.plan_id, tmdb_client=object())

        assert result["auto_scraped"] == 2
        assert refresh_calls == [["multi-season-1", "multi-season-2"]]
        assert result["library_refresh"]["work_count"] == 1

    def test_auto_scrape_routes_incomplete_outputs_to_failed_cases(self, sample_target, high_score_candidate, monkeypatch):
        """执行返回后仍缺关键产物时必须进入失败处理，不能留下永久隐藏卡片。"""
        plan = ImportPlan(
            plan_id=sample_target.import_plan_id,
            source=sample_target.source,
            status="confirmed",
        )
        failed_cases = []

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id=None, source=None: plan)
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: SimpleNamespace(items=[]))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr("app.scrape.auto.search_candidates", lambda *args, **kwargs: [high_score_candidate])
        monkeypatch.setattr("app.scrape.auto.execute_scrape", lambda **kwargs: {"ok": True})
        monkeypatch.setattr("app.scrape.auto.save_failed_case", failed_cases.append)
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: (_ for _ in ()).throw(AssertionError("incomplete work must not publish")),
        )

        result = run_auto_scrape("pan115", plan_id=plan.plan_id, tmdb_client=object())

        assert result["auto_scraped"] == 0
        assert result["failed"] == 1
        assert len(failed_cases) == 1
        assert "刮削产物不完整" in failed_cases[0]["error"]

    def test_auto_scrape_keeps_scrape_result_when_partial_publish_fails(self, sample_target, high_score_candidate, monkeypatch):
        """局部发布失败不能回退全量重扫，也不能抹掉已经完成的刮削产物。"""
        sample_target.scrape_target_id = "refresh-failed"
        sample_target.work_id = "refresh-failed-work"
        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: type("M", (), {"items": []})())
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)

        high_score_candidate.scrape_target_id = sample_target.scrape_target_id
        high_score_candidate.title = sample_target.scrape_title
        high_score_candidate.year = sample_target.scrape_year
        high_score_candidate.score = 90
        monkeypatch.setattr("app.scrape.auto.search_candidates", lambda *args, **kwargs: [high_score_candidate])
        monkeypatch.setattr("app.scrape.auto.execute_scrape", lambda **kwargs: {"ok": True})
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: (_ for _ in ()).throw(RuntimeError("局部发布失败")),
        )
        monkeypatch.setattr(
            "app.import_plan.store.load_import_plan",
            lambda plan_id=None, source=None: ImportPlan(
                plan_id=plan_id or "p1",
                source="pan115",
                status="confirmed",
            ),
        )
        monkeypatch.setattr(
            "app.library.service.rescan_library",
            lambda source=None: (_ for _ in ()).throw(RuntimeError("没有生成任何可展示作品")),
        )

        result = run_auto_scrape("pan115", tmdb_client=object())

        assert result["auto_scraped"] == 1
        assert result["failed"] == 0
        assert result["library_refresh"]["mode"] == "partial_failed"
        assert "局部发布失败" in result["library_refresh"]["warnings"][0]

    def test_auto_scrape_publishes_completed_work_when_all_targets_already_scraped(
        self, sample_target, monkeypatch
    ):
        """已有完整资料仍需局部发布，修复因重试跳过而隐藏的作品卡片。"""
        sample_target.scrape_target_id = "existing-unpublished"
        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: type("M", (), {"items": []})())
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._target_already_scraped", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)

        refresh_calls = []
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: refresh_calls.append(
                [target.scrape_target_id for target in targets]
            ) or {"work_count": 1, "mode": "partial", "warnings": []},
        )
        monkeypatch.setattr(
            "app.import_plan.store.load_import_plan",
            lambda plan_id=None, source=None: ImportPlan(
                plan_id=plan_id or "p1",
                source="pan115",
                status="confirmed",
            ),
        )
        monkeypatch.setattr(
            "app.library.service.rescan_library",
            lambda source=None: (_ for _ in ()).throw(AssertionError("should not rescan after scrape")),
        )
        progress_patches = []

        result = run_auto_scrape(
            "pan115",
            tmdb_client=object(),
            progress_callback=lambda progress, message, patch=None: progress_patches.append(patch or {}),
        )

        assert result["skipped_existing"] == 1
        revisions = [patch.get("library_refresh_revision", 0) for patch in progress_patches]
        assert refresh_calls == [["existing-unpublished"]]
        assert 1 in revisions
        assert result["library_refresh"]["mode"] == "partial"

    def test_existing_show_nfo_repairs_missing_episode_nfos(self, sample_target, tmp_path):
        """已有 tvshow.nfo 但缺少单集 NFO 时，不应跳过整季刮削"""
        from app.scrape.models import ScrapeMapItem

        nfo = tmp_path / "tvshow.nfo"
        nfo.write_text("<tvshow />", encoding="utf-8")
        sample_target.target_dir = str(tmp_path)
        sample_target.target_nfo_path = str(nfo)
        sample_target.item_ids = ["e1", "e2"]

        item = ScrapeMapItem(
            scrape_target_id=sample_target.scrape_target_id,
            source=sample_target.source,
            tmdb_id=24835,
            tmdb_type="tv",
            local_season_number=1,
            nfo_path=str(nfo),
        )

        assert _target_already_scraped(sample_target, {sample_target.scrape_target_id: item}) is False

        (tmp_path / "S01E01.nfo").write_text("<episodedetails />", encoding="utf-8")
        (tmp_path / "S01E02.nfo").write_text("<episodedetails />", encoding="utf-8")
        (tmp_path / "poster.jpg").write_bytes(b"poster")
        (tmp_path / "fanart.jpg").write_bytes(b"fanart")
        assert _target_already_scraped(sample_target, {sample_target.scrape_target_id: item}) is True

    def test_existing_nfo_without_required_artwork_is_not_complete(self, sample_target, tmp_path):
        """只有 NFO、缺少海报或背景图时必须重新刮削图片。"""
        from app.scrape.models import ScrapeMapItem

        nfo = tmp_path / "tvshow.nfo"
        nfo.write_text("<tvshow />", encoding="utf-8")
        sample_target.target_dir = str(tmp_path)
        sample_target.target_nfo_path = str(nfo)
        sample_target.item_ids = []
        item = ScrapeMapItem(
            scrape_target_id=sample_target.scrape_target_id,
            tmdb_id=24835, tmdb_type="tv", nfo_path=str(nfo),
        )

        assert _target_already_scraped(
            sample_target, {sample_target.scrape_target_id: item}
        ) is False

        (tmp_path / "poster.jpg").write_bytes(b"poster")
        (tmp_path / "fanart.jpg").write_bytes(b"fanart")
        assert _target_already_scraped(
            sample_target, {sample_target.scrape_target_id: item}
        ) is True

    def test_existing_show_nfo_repairs_wrong_season_nfos(
        self, sample_target, tmp_path, monkeypatch
    ):
        """NFO 数量相同但季号错误时，仍应重新补刮正确分集。"""
        from app.scrape.models import ScrapeMapItem

        nfo = tmp_path / "tvshow.nfo"
        nfo.write_text("<tvshow />", encoding="utf-8")
        sample_target.target_dir = str(tmp_path)
        sample_target.target_nfo_path = str(nfo)
        sample_target.item_ids = ["e1", "e2"]
        plan = ImportPlan(
            plan_id=sample_target.import_plan_id,
            items=[
                ImportPlanItem(
                    id="e1", group_type="season", season_number=1, episode_number=1
                ),
                ImportPlanItem(
                    id="e2", group_type="season", season_number=1, episode_number=2
                ),
            ],
        )
        monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda **_: plan)
        (tmp_path / "S02E01.nfo").write_text("<episodedetails />", encoding="utf-8")
        (tmp_path / "S02E02.nfo").write_text("<episodedetails />", encoding="utf-8")

        item = ScrapeMapItem(
            scrape_target_id=sample_target.scrape_target_id,
            source=sample_target.source,
            tmdb_id=24835,
            tmdb_type="tv",
            local_season_number=1,
            nfo_path=str(nfo),
        )

        assert _target_already_scraped(
            sample_target, {sample_target.scrape_target_id: item}
        ) is False

    def test_existing_plain_series_later_season_map_to_tmdb_s1_is_stale(
        self, sample_target, monkeypatch
    ):
        """父级强绑定的普通主系列 S2/S3 旧映射成 TMDB S1 时，应允许自动刮削修复。"""
        import app.scrape.completeness as completeness_module
        from app.scrape.models import ScrapeMapItem

        monkeypatch.setattr(completeness_module.Path, "is_file", lambda self: True)
        monkeypatch.setattr(
            completeness_module.Path,
            "glob",
            lambda self, pattern: [completeness_module.Path("S02E01.nfo")] if pattern == "S??E*.nfo" else [],
        )

        sample_target.target_dir = "virtual"
        sample_target.target_nfo_path = "virtual/tvshow.nfo"
        sample_target.item_ids = []
        sample_target.series_group = "Re：从零开始的异世界生活"
        sample_target.local_title = "Re：从零开始的异世界生活"
        sample_target.scrape_title = "Re：从零开始的异世界生活"
        sample_target.local_season_number = 2
        sample_target.source_subwork_dir = ""
        sample_target.tmdb_hint_id = 65942
        sample_target.tmdb_hint_type = "tv"

        stale_item = ScrapeMapItem(
            scrape_target_id=sample_target.scrape_target_id,
            source=sample_target.source,
            tmdb_id=65942,
            tmdb_type="tv",
            local_season_number=2,
            tmdb_season_number=1,
            nfo_path="virtual/tvshow.nfo",
            poster_path="https://image.example/poster.jpg",
            fanart_path="https://image.example/fanart.jpg",
        )
        assert _target_already_scraped(
            sample_target, {sample_target.scrape_target_id: stale_item}
        ) is False

        repaired_item = ScrapeMapItem(
            scrape_target_id=sample_target.scrape_target_id,
            source=sample_target.source,
            tmdb_id=65942,
            tmdb_type="tv",
            local_season_number=2,
            tmdb_season_number=2,
            nfo_path="virtual/tvshow.nfo",
            poster_path="https://image.example/poster.jpg",
            fanart_path="https://image.example/fanart.jpg",
        )
        assert _target_already_scraped(
            sample_target, {sample_target.scrape_target_id: repaired_item}
        ) is True

    def test_season_two_search_variants_include_parent_series(self, sample_target):
        """第二季标题有后缀时，应尽早回退到父系列名搜索"""
        sample_target.scrape_title = "路人女主的养成方法b"
        sample_target.local_title = "路人女主的养成方法"
        sample_target.series_group = "路人女主的养成方法"
        sample_target.source_subwork_dir = "2.路人女主的养成方法b.[S2].2017"
        sample_target.local_season_number = 2

        variants = _build_target_search_title_variants(sample_target, sample_target.scrape_title)

        assert "路人女主的养成方法" in variants
        assert variants.index("路人女主的养成方法") < 8

    def test_movie_search_variants_drop_theatrical_prefix_and_colon(self, sample_target):
        """动画电影搜不到全名时，应能回退到去掉“剧场版：”后的具体电影名"""
        sample_target.media_type = "movie"
        sample_target.group_type = "movie"
        sample_target.scrape_type = "movie"
        sample_target.series_group = "刀剑神域"
        sample_target.local_title = "剧场版：进击篇 无星之夜的咏叹调"
        sample_target.scrape_title = "刀剑神域 剧场版：进击篇 无星之夜的咏叹调"
        sample_target.source_subwork_dir = "剧场版：进击篇 无星之夜的咏叹调.2021"
        sample_target.original_title = "剧场版：进击篇 无星之夜的咏叹调"
        sample_target.scrape_year = 2021

        variants = _build_target_search_title_variants(sample_target, sample_target.scrape_title)

        assert "进击篇 无星之夜的咏叹调" in variants
        assert "无星之夜的咏叹调" in variants


# ============================================================
# API 测试
# ============================================================

class TestAutoScrapeAPI:
    """测试自动刮削 API"""

    def test_auto_rejects_removed_dry_run(self):
        """预检已删除，旧 dry_run 请求必须被拒绝，不能误执行真实刮削。"""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.core.config import invalidate_config_cache

        invalidate_config_cache()
        client = TestClient(app)

        resp = client.post("/api/scrape/auto", json={
            "source": "pan115",
            "dry_run": True,
        })
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["body", "dry_run"]

    def test_review_queue_endpoint(self):
        """review queue 端点应返回队列"""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.core.config import invalidate_config_cache

        invalidate_config_cache()
        client = TestClient(app)

        resp = client.get("/api/scrape/review-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data


def test_plain_multiseason_parent_tmdb_hint_is_kept_for_later_seasons(tmp_path):
    """Plain Season 2/3 folders under the same hinted show should keep the hint."""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.scrape.target_builder import build_scrape_targets

    plan = ImportPlan(
        plan_id="p-rezero",
        source="baidu",
        status="confirmed",
        items=[
            ImportPlanItem(
                id="s2e01",
                plan_id="p-rezero",
                source="baidu",
                relative_path=(
                    "动画/Re：从零开始的异世界生活 (2016) {tmdb-65942}/Season 2/"
                    "Re：从零开始的异世界生活.S02E01.2020.2160P.BDRIP.mkv"
                ),
                resource_type="video",
                action="generate_strm",
                work_id="rezero",
                work_title="Re：从零开始的异世界生活",
                year=2016,
                media_type="tv",
                show_type="anime_series",
                tmdb_hint_id=65942,
                tmdb_hint_type="tv",
                series_group="Re：从零开始的异世界生活",
                card_type="main_series",
                group_type="season",
                season_number=2,
                episode_number=1,
                target_dir=str(tmp_path / "Re：从零开始的异世界生活" / "Season 2"),
            ),
            ImportPlanItem(
                id="s3e01",
                plan_id="p-rezero",
                source="baidu",
                relative_path=(
                    "动画/Re：从零开始的异世界生活 (2016) {tmdb-65942}/Season 3/"
                    "Re：从零开始的异世界生活.S03E01.2024.2160P.BDRIP.mkv"
                ),
                resource_type="video",
                action="generate_strm",
                work_id="rezero",
                work_title="Re：从零开始的异世界生活",
                year=2016,
                media_type="tv",
                show_type="anime_series",
                tmdb_hint_id=65942,
                tmdb_hint_type="tv",
                series_group="Re：从零开始的异世界生活",
                card_type="main_series",
                group_type="season",
                season_number=3,
                episode_number=1,
                target_dir=str(tmp_path / "Re：从零开始的异世界生活" / "Season 3"),
            ),
        ],
    )

    targets = build_scrape_targets(plan)
    season_2 = next(t for t in targets if t.local_season_number == 2)
    season_3 = next(t for t in targets if t.local_season_number == 3)

    assert season_2.scrape_year == 2020
    assert season_3.scrape_year == 2024
    assert season_2.tmdb_hint_id == 65942
    assert season_3.tmdb_hint_id == 65942
    assert season_2.target_poster_path == str(tmp_path / "Re：从零开始的异世界生活" / "poster.jpg")
    assert season_3.target_fanart_path == str(tmp_path / "Re：从零开始的异世界生活" / "fanart.jpg")


def test_subwork_later_season_does_not_inherit_parent_tmdb_hint(tmp_path):
    """具体子作品目录仍需自己的 hint，避免独立续作被父系列强绑。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.scrape.target_builder import build_scrape_targets

    plan = ImportPlan(
        plan_id="p-sequel",
        source="baidu",
        status="confirmed",
        items=[
            ImportPlanItem(
                id="sequel-e01",
                plan_id="p-sequel",
                source="baidu",
                relative_path=(
                    "动画/Some Series (2016) {tmdb-111}/2.Some Series Sequel.2020/"
                    "Some Series Sequel.S02E01.mkv"
                ),
                resource_type="video",
                action="generate_strm",
                work_id="some-series",
                work_title="Some Series",
                year=2016,
                media_type="tv",
                show_type="anime_series",
                tmdb_hint_id=111,
                tmdb_hint_type="tv",
                series_group="Some Series",
                card_type="main_series",
                group_type="season",
                season_number=2,
                episode_number=1,
                reasons=["子作品目录: 2.Some Series Sequel.2020"],
                target_dir=str(tmp_path / "Some Series" / "Season 2"),
            ),
        ],
    )

    target = build_scrape_targets(plan)[0]

    assert target.source_subwork_dir == "2.Some Series Sequel.2020"
    assert target.tmdb_hint_id is None


def test_episode_nfo_is_rewritten_when_rescraping(tmp_path, monkeypatch):
    """重刮分集时必须覆盖旧错 NFO，否则前端会一直显示历史错误标题。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.scrape.models import ScrapeTarget
    from app.scrape.service import _generate_episode_nfos

    season_dir = tmp_path / "CLANNAD" / "Season 2"
    season_dir.mkdir(parents=True)
    old_nfo = season_dir / "S02E01.nfo"
    old_nfo.write_text(
        "<episodedetails><title>错误旧标题</title><season>2</season><episode>1</episode></episodedetails>",
        encoding="utf-8",
    )

    item = ImportPlanItem(
        id="s2e01",
        plan_id="plan-clannad",
        source="pan115",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/3.CLANNAD After Story.[S02].2008/CLANNAD After Story 102.mkv",
        resource_type="video",
        action="generate_strm",
        work_id="clannad",
        work_title="CLANNAD",
        media_type="tv",
        show_type="anime_series",
        series_group="CLANNAD",
        card_type="main_series",
        group_type="season",
        season_number=2,
        episode_number=1,
        target_strm_path=str(season_dir / "CLANNAD - S02E01.strm"),
    )
    plan = ImportPlan(
        plan_id="plan-clannad",
        source="pan115",
        status="confirmed",
        items=[item],
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class FakeClient:
        def get_tv_season_detail(self, tmdb_id, season_number):
            assert season_number == 2
            return {
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "在夏日终结的小镇",
                        "overview": "After Story episode 1",
                        "air_date": "2008-10-03",
                        "runtime": 24,
                    }
                ]
            }

    target = ScrapeTarget(
        scrape_target_id="target-clannad-s2",
        source="pan115",
        import_plan_id="plan-clannad",
        work_id="clannad",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="CLANNAD",
        local_title="CLANNAD",
        scrape_title="CLANNAD After Story",
        scrape_type="tv",
        local_season_number=2,
        item_ids=["s2e01"],
        needs_review=False,
        warnings=[],
    )

    results = _generate_episode_nfos(
        target=target,
        tmdb_id=24835,
        tmdb_season_number=2,
        target_dir=str(season_dir),
        client=FakeClient(),
    )

    assert results[0]["status"] == "success"
    rewritten = old_nfo.read_text(encoding="utf-8")
    assert "在夏日终结的小镇" in rewritten
    assert "错误旧标题" not in rewritten


def test_episode_nfo_preserves_explicit_local_episode_title(tmp_path, monkeypatch):
    """目录树已有明确中文集标题时，不应被提供方的另一语言标题覆盖。"""
    from app.scrape.service import _generate_episode_nfos

    season_dir = tmp_path / "更衣人偶坠入爱河" / "Season 1"
    season_dir.mkdir(parents=True)
    item = ImportPlanItem(
        id="dress-up-s1e1",
        plan_id="plan-dress-up",
        source="baidu",
        resource_type="video",
        action="generate_strm",
        work_title="更衣人偶坠入爱河",
        series_group="更衣人偶坠入爱河",
        group_type="season",
        season_number=1,
        episode_number=1,
        title="和自己生活在截然不同世界的人",
        target_strm_path=str(season_dir / "更衣人偶坠入爱河 - S01E01.strm"),
    )
    plan = ImportPlan(plan_id="plan-dress-up", source="baidu", items=[item])
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class FakeClient:
        def get_tv_season_detail(self, tmdb_id, season_number):
            return {"episodes": [{"episode_number": 1, "name": "Someone from a different world"}]}

    target = ScrapeTarget(
        scrape_target_id="target-dress-up-s1",
        source="baidu",
        import_plan_id=plan.plan_id,
        group_type="season",
        scrape_type="tv",
        local_season_number=1,
        item_ids=[item.id],
    )

    _generate_episode_nfos(target, 123, 1, str(season_dir), FakeClient())

    text = (season_dir / "S01E01.nfo").read_text(encoding="utf-8")
    assert "和自己生活在截然不同世界的人" in text
    assert "Someone from a different world" not in text


def test_episode_nfo_ignores_release_metadata_title(tmp_path, monkeypatch):
    """压制参数不能覆盖 TMDB 的真实分集标题。"""
    from app.scrape.service import _generate_episode_nfos

    season_dir = tmp_path / "Lycoris Recoil" / "Season 1"
    season_dir.mkdir(parents=True)
    item = ImportPlanItem(
        id="lycoris-s1e1",
        plan_id="plan-lycoris",
        source="pan115",
        resource_type="video",
        action="generate_strm",
        work_title="莉可丽丝",
        series_group="莉可丽丝",
        group_type="season",
        season_number=1,
        episode_number=1,
        title="[JP.BD.Remux]",
        target_strm_path=str(season_dir / "莉可丽丝 - S01E01.strm"),
    )
    plan = ImportPlan(plan_id="plan-lycoris", source="pan115", items=[item])
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class FakeClient:
        def get_tv_season_detail(self, tmdb_id, season_number):
            return {"episodes": [{"episode_number": 1, "name": "Easy does it"}]}

    target = ScrapeTarget(
        scrape_target_id="target-lycoris-s1",
        source="pan115",
        import_plan_id=plan.plan_id,
        group_type="season",
        scrape_type="tv",
        local_season_number=1,
        item_ids=[item.id],
    )

    _generate_episode_nfos(target, 125910, 1, str(season_dir), FakeClient())

    text = (season_dir / "S01E01.nfo").read_text(encoding="utf-8")
    assert "Easy does it" in text
    assert "JP.BD.Remux" not in text


def test_episode_nfo_generation_deduplicates_same_local_episode(tmp_path, monkeypatch):
    """同一集的原版/重编版文件不能重复请求并覆盖同一个 NFO。"""
    from app.scrape.service import _generate_episode_nfos

    season_dir = tmp_path / "ReZero" / "Season 1"
    season_dir.mkdir(parents=True)
    items = [
        ImportPlanItem(
            id=item_id,
            plan_id="plan-rezero",
            action="generate_strm",
            group_type="season",
            season_number=1,
            episode_number=1,
            target_strm_path=str(season_dir / "ReZero - S01E01.strm"),
        )
        for item_id in ("original-e1", "recut-e1")
    ]
    plan = ImportPlan(plan_id="plan-rezero", items=items)
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class FakeClient:
        calls = 0

        def get_tv_season_detail(self, tmdb_id, season_number):
            self.calls += 1
            return {"episodes": [{"episode_number": 1, "name": "第一集"}]}

    client = FakeClient()
    target = ScrapeTarget(
        scrape_target_id="target-rezero-s1",
        import_plan_id=plan.plan_id,
        group_type="season",
        scrape_type="tv",
        local_season_number=1,
        item_ids=[item.id for item in items],
    )

    results = _generate_episode_nfos(target, 65942, 1, str(season_dir), client)

    assert len(results) == 1
    assert results[0]["episode"] == "S01E01"


def test_episode_nfo_logs_one_work_range_instead_of_each_episode(tmp_path, monkeypatch):
    """分集文件仍逐集生成，但任务日志只发布一次作品级集数范围。"""
    from app.scrape.service import _generate_episode_nfos

    season_dir = tmp_path / "测试番剧" / "Season 1"
    season_dir.mkdir(parents=True)
    items = [
        ImportPlanItem(
            id=f"episode-{episode}",
            plan_id="plan-log-range",
            source="pan115",
            resource_type="video",
            action="generate_strm",
            work_title="测试番剧",
            series_group="测试番剧",
            group_type="season",
            season_number=1,
            episode_number=episode,
            target_strm_path=str(season_dir / f"测试番剧 - S01E{episode:02d}.strm"),
        )
        for episode in range(1, 4)
    ]
    plan = ImportPlan(plan_id="plan-log-range", source="pan115", items=items)
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class FakeClient:
        def get_tv_season_detail(self, tmdb_id, season_number):
            return {
                "episodes": [
                    {"episode_number": episode, "name": f"第 {episode} 话"}
                    for episode in range(1, 4)
                ]
            }

    target = ScrapeTarget(
        scrape_target_id="target-log-range",
        source="pan115",
        import_plan_id=plan.plan_id,
        series_group="测试番剧",
        local_title="测试番剧",
        scrape_title="测试番剧",
        group_type="season",
        scrape_type="tv",
        local_season_number=1,
        item_ids=[item.id for item in items],
    )
    logs = []

    results = _generate_episode_nfos(
        target,
        123,
        1,
        str(season_dir),
        FakeClient(),
        log_callback=lambda message, kind="info": logs.append((message, kind)),
    )

    assert len(results) == 3
    assert sum("正在刮削《测试番剧》分集 1-3（共 3 集）" in message for message, _ in logs) == 1
    assert not any("生成分集 NFO" in message for message, _ in logs)
    assert not any("读取分集详情" in message for message, _ in logs)


def test_rejects_wrong_work_sharing_only_common_cjk_words():
    target = ScrapeTarget(
        scrape_target_id="wrong-cjk-1", source="baidu", import_plan_id="p1", work_id="w1",
        card_type="main_series", media_type="tv", group_type="season",
        series_group="路人女主的养成方法", local_title="路人女主的养成方法",
        scrape_title="路人女主的养成方法", scrape_type="tv", local_season_number=1,
    )
    candidate = ScrapeCandidate(
        candidate_id="c-wrong-1", scrape_target_id=target.scrape_target_id,
        provider="tmdb", tmdb_id=250003, tmdb_type="tv",
        title="从路人角色开始的探索英雄谭", original_title="モブから始まる探索英雄譚",
        year=2024, score=91, popularity=50, vote_average=7.3,
        reasons=["标题相似", "类型匹配", "高热度"],
    )

    selected, reason = decide_auto_candidate(target, [candidate], threshold=70)

    assert selected is None
    assert "不是同一作品" in reason


def test_rejects_wrong_work_sharing_short_cjk_fragment():
    target = ScrapeTarget(
        scrape_target_id="wrong-cjk-2", source="baidu", import_plan_id="p1", work_id="w2",
        card_type="main_series", media_type="tv", group_type="season",
        series_group="明日同学的水手服", local_title="明日同学的水手服",
        scrape_title="明日同学的水手服", scrape_type="tv", local_season_number=1,
    )
    candidate = ScrapeCandidate(
        candidate_id="c-wrong-2", scrape_target_id=target.scrape_target_id,
        provider="tmdb", tmdb_id=35904, tmdb_type="tv",
        title="明日的与一", original_title="明日のよいち!",
        year=2009, score=88, popularity=40, vote_average=7.0,
        reasons=["标题部分匹配", "类型匹配", "高热度"],
    )

    selected, reason = decide_auto_candidate(target, [candidate], threshold=70)

    assert selected is None
    assert "不是同一作品" in reason


# ============================================================
# 取消与进度元数据测试
# ============================================================

class TestAutoScrapeCancelAndProgress:
    """自动刮削取消与进度元数据测试"""

    def test_should_cancel_stops_before_next_target(self, sample_target, high_score_candidate, monkeypatch):
        """取消请求发出后，不能继续处理下一部作品。"""
        from app.scrape.models import ScrapeMap

        target1 = sample_target
        target1.scrape_target_id = "cancel-test-1"
        target1.work_id = "w-cancel-1"

        target2 = ScrapeTarget(
            scrape_target_id="cancel-test-2",
            source=target1.source,
            import_plan_id=target1.import_plan_id,
            work_id="w-cancel-2",
            card_type="main_series",
            media_type="tv",
            group_type="season",
            series_group="不应处理的作品",
            local_title="不应处理的作品",
            scrape_title="不应处理的作品",
            scrape_year=2025,
            scrape_type="tv",
            local_season_number=1,
        )

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([target1, target2], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: ScrapeMap(items=[]))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )

        def fake_search(target, tmdb_client=None):
            candidate = high_score_candidate
            candidate.scrape_target_id = target.scrape_target_id
            candidate.tmdb_id = 9999 + len(target.scrape_target_id)
            candidate.tmdb_type = target.scrape_type
            candidate.title = target.scrape_title
            candidate.score = 90
            return [candidate]

        monkeypatch.setattr("app.scrape.auto.search_candidates", fake_search)

        executed_targets = []

        def fake_execute_scrape(**kwargs):
            executed_targets.append(kwargs["target"].scrape_target_id)
            return {"ok": True}

        monkeypatch.setattr("app.scrape.auto.execute_scrape", fake_execute_scrape)

        cancel_state = {"cancelled": False}

        def _should_cancel():
            return cancel_state["cancelled"]

        def delayed_cancel():
            time.sleep(0.1)
            cancel_state["cancelled"] = True

        cancel_thread = threading.Thread(target=delayed_cancel)
        cancel_thread.start()

        result = run_auto_scrape(
            "pan115",
            tmdb_client=object(),
            progress_callback=lambda *args, **kwargs: None,
            should_cancel=_should_cancel,
        )

        cancel_thread.join()

        assert len(executed_targets) == 1
        assert executed_targets[0] == "cancel-test-1"
        assert result["auto_scraped"] == 1

    def test_progress_patch_has_completed_and_remaining(self, sample_target, high_score_candidate, monkeypatch):
        """每次进度回调都包含 completed_targets / remaining_targets。"""
        from app.scrape.models import ScrapeMap

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: ScrapeMap(items=[]))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto._work_targets_complete", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto._target_outputs_complete_after_scrape", lambda *args, **kwargs: True)
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "app.library.service.refresh_library_for_scrape_targets",
            lambda targets: {"work_count": len(targets), "mode": "partial"},
        )

        def fake_search(target, tmdb_client=None):
            candidate = high_score_candidate
            candidate.scrape_target_id = target.scrape_target_id
            return [candidate]

        monkeypatch.setattr("app.scrape.auto.search_candidates", fake_search)
        monkeypatch.setattr("app.scrape.auto.execute_scrape", lambda **kwargs: {"ok": True})

        progress_patches = []

        def _progress_callback(progress, message, patch=None):
            progress_patches.append(patch or {})

        result = run_auto_scrape(
            "pan115",
            tmdb_client=object(),
            progress_callback=_progress_callback,
        )

        assert result["auto_scraped"] == 1
        assert result["completed_targets"] == 1
        assert result["remaining_targets"] == 0

        patches_with_meta = [
            p for p in progress_patches
            if "completed_targets" in p and "remaining_targets" in p and "total_targets" in p
        ]
        assert patches_with_meta, "progress patches should include completed/remaining/total"
        assert patches_with_meta[-1]["completed_targets"] == 1
        assert patches_with_meta[-1]["remaining_targets"] == 0

        for patch in patches_with_meta:
            c = int(patch["completed_targets"])
            r = int(patch["remaining_targets"])
            t = int(patch["total_targets"])
            assert c + r == t, f"progress meta mismatch: {c} + {r} != {t}"

    def test_completed_remaining_total_consistency(self, sample_target, high_score_candidate, monkeypatch):
        """completed_targets + remaining_targets === total_targets always holds."""
        from app.scrape.models import ScrapeMap

        monkeypatch.setattr("app.scrape.service.get_targets", lambda source, plan_id=None: ([sample_target], None))
        monkeypatch.setattr("app.scrape.auto.load_scrape_map", lambda: ScrapeMap(items=[]))
        monkeypatch.setattr("app.scrape.auto._build_existing_scrape_index", lambda: {})
        monkeypatch.setattr("app.scrape.auto.resolve_tmdb_season_number", lambda *args, **kwargs: 1)
        monkeypatch.setattr("app.scrape.auto._validate_auto_candidate", lambda *args, **kwargs: [])
        monkeypatch.setattr("app.scrape.review_queue.resolve_review_item", lambda *args, **kwargs: None)

        def fake_search(target, tmdb_client=None):
            candidate = high_score_candidate
            candidate.scrape_target_id = target.scrape_target_id
            return [candidate]

        monkeypatch.setattr("app.scrape.auto.search_candidates", fake_search)
        monkeypatch.setattr("app.scrape.auto.execute_scrape", lambda **kwargs: {"ok": True})

        progress_patches = []

        def _progress_callback(progress, message, patch=None):
            progress_patches.append(patch or {})

        run_auto_scrape(
            "pan115",
            tmdb_client=object(),
            progress_callback=_progress_callback,
        )

        for patch in progress_patches:
            if "total_targets" in patch and "completed_targets" in patch and "remaining_targets" in patch:
                total = int(patch["total_targets"])
                completed = int(patch["completed_targets"])
                remaining = int(patch["remaining_targets"])
                assert completed + remaining == total, (
                    f"inconsistent: completed {completed} + remaining {remaining} = {completed + remaining}, expected {total}"
                )
