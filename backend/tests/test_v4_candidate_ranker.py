"""共享 CandidateRanker 合同（D2/D3）。

语料来自 D1 只读诊断的代表性最小案例（全部为公开作品名，不含真实路径
或凭据）：中文译名↔日文原名、正确候选非热度第一名、年份差一、同名不同
年份、父系列与外传、电影篇章相反、短通用标题。锁定排序、score reasons
与安全自动采用结果；语料级回放满足 recall ≥ 95%、auto-adoption
precision ≥ 98%。
"""

from __future__ import annotations

from app.media_v4.resolution.ranker import CandidateRanker, rank_candidates


def _candidate(**overrides) -> dict:
    base = {
        "provider": "tmdb",
        "provider_id": "1",
        "media_type": "tv",
        "title": "",
        "original_title": "",
        "aliases": (),
        "year": None,
        "popularity": 10.0,
    }
    base.update(overrides)
    return base


def _target(**overrides) -> dict:
    base = {
        "preferred_title": "",
        "queries": [],
        "media_type": "tv",
        "year": None,
        "aliases": (),
    }
    base.update(overrides)
    return base


def test_main_title_does_not_tie_with_spinoff_prefixes():
    target = _target(preferred_title="辉夜大小姐想让我告白", show_type="anime")
    candidates = [
        _candidate(provider_id=str(index), title=title, genre_ids=[16])
        for index, title in enumerate((
            "辉夜大小姐想让我告白", "辉夜大小姐想让我告白：通往大人的阶梯",
            "辉夜大小姐想让我告白：初吻不会结束",
        ))
    ]
    ranked = CandidateRanker().rank(target, candidates)
    adopted, _reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is not None and adopted.provider_id == "0"
    assert not any(item.identity_safe for item in ranked if item.provider_id != "0")


def test_only_spinoff_prefix_is_not_auto_adopted_as_main_work():
    ranked = CandidateRanker().rank(
        _target(preferred_title="辉夜大小姐想让我告白", show_type="anime", year=2026),
        [_candidate(title="辉夜大小姐想让我告白：通往大人的阶梯", genre_ids=[16], year=2026)],
    )
    assert CandidateRanker().auto_adopt(ranked)[0] is None


# ── 语料：身份安全 + 自动采用 ────────────────────────────────────────────────


def test_chinese_title_matches_japanese_original_via_alias_evidence():
    """中文译名查询 ↔ 日文原名候选：可信别名链完整等值即身份安全。"""

    target = _target(
        preferred_title="更衣人偶坠入爱河",
        queries=["更衣人偶坠入爱河"],
    )
    candidate = _candidate(
        provider_id="130053",
        title="My Dress-Up Darling",
        original_title="その着せ替え人形は恋をする",
        aliases=("更衣人偶坠入爱河",),
        year=2022,
    )
    ranked = rank_candidates(target, [candidate])
    assert ranked[0].score >= 60
    assert ranked[0].recommended is True
    assert any("别名" in reason or "等值" in reason for reason in ranked[0].reasons)
    adopted, reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is not None and adopted.provider_id == "130053"


def test_identity_safe_candidate_beats_higher_popularity_wrong_match():
    """热度更高但身份不安全的候选不得登顶；推荐位属于身份安全者。"""

    target = _target(preferred_title="天国大魔境", queries=["天国大魔境"], year=2023)
    popular_wrong = _candidate(provider_id="900", title="天国大魔境 第二季", year=2026, popularity=99.0)
    right = _candidate(provider_id="208891", title="天国大魔境", original_title="天国大魔境", year=2023, popularity=3.0)
    ranked = rank_candidates(target, [popular_wrong, right])
    assert ranked[0].provider_id == "208891"
    assert ranked[0].recommended is True


def test_year_off_by_one_with_strong_title_still_auto_adopts():
    """年份差一但标题强匹配：允许自动采用（记录年份理由）。"""

    target = _target(preferred_title="葬送的芙莉莲", queries=["葬送的芙莉莲"], year=2023)
    candidate = _candidate(provider_id="209867", title="葬送的芙莉莲", original_title="葬送のフリーレン", year=2024)
    ranked = rank_candidates(target, [candidate])
    adopted, _reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is not None and adopted.provider_id == "209867"


def test_unique_candidate_with_exact_title_auto_adopts():
    """唯一候选且完整标题等值：自动采用并写明采用理由。"""

    target = _target(preferred_title="虫师", queries=["虫师"], year=2005)
    candidate = _candidate(provider_id="26867", title="虫师", original_title="蟲師", year=2005)
    ranked = rank_candidates(target, [candidate])
    adopted, reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is not None
    assert "唯一" in reason or "等值" in reason or "采用" in reason


def test_cjk_punctuation_difference_still_counts_as_the_same_title():
    """中文全角标点不能把唯一正确候选降成仅有类型分。"""

    target = _target(
        preferred_title="上伊那牡丹醉姿如百合",
        queries=["上伊那牡丹醉姿如百合"],
    )
    candidate = _candidate(
        provider_id="283905",
        title="上伊那牡丹，醉姿如百合",
        original_title="上伊那ぼたん、酔へる姿は百合の花",
        year=2026,
    )

    ranked = rank_candidates(target, [candidate])
    adopted, _reason = CandidateRanker().auto_adopt(ranked)

    assert ranked[0].identity_safe is True
    assert adopted is not None and adopted.provider_id == "283905"


def test_anime_target_rejects_same_title_live_action_runner_up():
    """动画库同名搜索结果中，真人剧不能制造需要人工处理的假歧义。"""

    target = _target(
        preferred_title="Yuru Camp",
        queries=["Yuru Camp"],
        show_type="anime_series",
    )
    anime = _candidate(
        provider_id="76075",
        title="摇曳露营△",
        original_title="ゆるキャン△",
        aliases=("Yuru Camp",),
        year=2018,
        popularity=22.5,
        genre_ids=(16, 35),
    )
    live_action = _candidate(
        provider_id="95623",
        title="摇曳露营△",
        original_title="ゆるキャン△",
        aliases=("Yuru Camp",),
        year=2020,
        popularity=3.5,
        genre_ids=(18,),
    )

    ranked = rank_candidates(target, [anime, live_action])
    adopted, _reason = CandidateRanker().auto_adopt(ranked)

    assert adopted is not None and adopted.provider_id == "76075"
    assert next(item for item in ranked if item.provider_id == "95623").blocked is True


def test_same_domain_same_title_candidates_still_require_review():
    """两个同域同名候选没有其他区分证据时仍保留人工确认。"""

    target = _target(
        preferred_title="Same Anime",
        queries=["Same Anime"],
        show_type="anime_series",
    )
    candidates = [
        _candidate(provider_id="1", title="Same Anime", genre_ids=(16,)),
        _candidate(provider_id="2", title="Same Anime", genre_ids=(16,)),
    ]

    ranked = rank_candidates(target, candidates)
    adopted, _reason = CandidateRanker().auto_adopt(ranked)

    assert adopted is None


# ── 语料：硬阻断（必须进入人工确认）────────────────────────────────────────


def test_same_title_with_far_year_conflict_is_hard_blocked():
    """同名但年份差 ≥2：硬阻断，不得自动采用。"""

    target = _target(preferred_title="同一部作品", queries=["同一部作品"], year=2010)
    candidate = _candidate(provider_id="77", title="同一部作品", year=2020)
    ranked = rank_candidates(target, [candidate])
    assert ranked[0].recommended is False
    adopted, _reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is None


def test_parent_series_candidate_does_not_absorb_the_spin_off():
    """父系列候选不得吸收外传：外传查询不会推荐父系列身份。"""

    target = _target(
        preferred_title="房间露营",
        queries=["房间露营"],
        aliases=("摇曳露营",),
        year=2020,
    )
    parent = _candidate(provider_id="73373", title="摇曳露营△", original_title="ゆるキャン△", year=2018, popularity=50.0)
    spin_off = _candidate(provider_id="112502", title="房间露营", original_title="へやキャン△", year=2020, popularity=5.0)
    ranked = rank_candidates(target, [parent, spin_off])
    assert ranked[0].provider_id == "112502"
    adopted, _reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is not None and adopted.provider_id == "112502"


def test_movie_part_mismatch_is_blocked():
    """电影篇章相反（查询后篇、候选前篇）：必须阻断。"""

    target = _target(
        preferred_title="剧场版作品 后篇",
        queries=["剧场版作品 后篇"],
        media_type="movie",
        year=2021,
    )
    part_one = _candidate(
        provider_id="8001",
        media_type="movie",
        title="剧场版作品 前篇",
        year=2020,
        popularity=80.0,
    )
    ranked = rank_candidates(target, [part_one])
    adopted, _reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is None
    assert any("篇章" in reason for reason in ranked[0].reasons)


def test_generic_short_title_requires_manual_review():
    """短通用标题（如“路人”）不构成身份证据：必须人工确认。"""

    target = _target(preferred_title="路人", queries=["路人"], year=None)
    candidate = _candidate(provider_id="400", title="路人超能100", year=2016)
    ranked = rank_candidates(target, [candidate])
    adopted, _reason = CandidateRanker().auto_adopt(ranked)
    assert adopted is None


# ── 语料级回放：recall / precision 门槛 ────────────────────────────────────


CORPUS = [
    # (target, candidates, 期望自动采用的 provider_id 或 None)
    (
        _target(preferred_title="更衣人偶坠入爱河", queries=["更衣人偶坠入爱河"], year=2022),
        [_candidate(provider_id="130053", title="My Dress-Up Darling", original_title="その着せ替え人形は恋をする", aliases=("更衣人偶坠入爱河",), year=2022, popularity=40.0)],
        "130053",
    ),
    (
        _target(preferred_title="路人女主的养成方法", queries=["路人女主的养成方法"], year=2015),
        [_candidate(provider_id="63635", title="路人女主的养成方法", original_title="冴えない彼女の育てかた", year=2015, popularity=20.0)],
        "63635",
    ),
    (
        _target(preferred_title="天国大魔境", queries=["天国大魔境"], year=2023),
        [
            _candidate(provider_id="900", title="天国大魔境 第二季", year=2026, popularity=99.0),
            _candidate(provider_id="208891", title="天国大魔境", original_title="天国大魔境", year=2023, popularity=3.0),
        ],
        "208891",
    ),
    (
        _target(preferred_title="虫师", queries=["虫师"], year=2005),
        [_candidate(provider_id="26867", title="虫师", original_title="蟲師", year=2005, popularity=15.0)],
        "26867",
    ),
    (
        _target(preferred_title="同一部作品", queries=["同一部作品"], year=2010),
        [_candidate(provider_id="77", title="同一部作品", year=2020, popularity=30.0)],
        None,
    ),
    (
        _target(preferred_title="路人", queries=["路人"], year=None),
        [_candidate(provider_id="400", title="路人超能100", year=2016, popularity=25.0)],
        None,
    ),
]


def test_corpus_replay_meets_recall_and_precision_thresholds():
    ranker = CandidateRanker()
    expected_positive = sum(1 for _t, _c, want in CORPUS if want is not None)
    recalled = 0
    correct_adoptions = 0
    adoptions = 0
    for target, candidates, want in CORPUS:
        ranked = rank_candidates(target, candidates)
        adopted, _reason = ranker.auto_adopt(ranked)
        if want is not None:
            if adopted is not None and adopted.provider_id == want:
                recalled += 1
                correct_adoptions += 1
                adoptions += 1
        else:
            assert adopted is None, "硬阻断语料被错误自动采用"
    recall = recalled / expected_positive if expected_positive else 1.0
    precision = correct_adoptions / adoptions if adoptions else 1.0
    assert recall >= 0.95
    assert precision >= 0.98
