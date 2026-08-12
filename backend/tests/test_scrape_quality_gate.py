"""刮削质量门禁：锁定既有规则库的已确认正确行为。"""

from app.scrape.auto import (
    _build_auto_decision_evidence,
    _candidate_identity_evidence,
    decide_auto_candidate,
)
from app.scrape.models import ScrapeCandidate, ScrapeTarget
from app.scrape.quality_gate import ScrapeQualityCase, evaluate_auto_decision_quality


def _target(
    title: str,
    *,
    scrape_type: str = "tv",
    year: int | None = None,
    tmdb_hint_id: int | None = None,
) -> ScrapeTarget:
    return ScrapeTarget(
        scrape_target_id=f"quality-{title}",
        source="local",
        card_type="main_series" if scrape_type == "tv" else "standalone",
        group_type="season" if scrape_type == "tv" else "movie",
        local_title=title,
        scrape_title=title,
        scrape_type=scrape_type,
        local_season_number=1 if scrape_type == "tv" else None,
        scrape_year=year,
        tmdb_hint_id=tmdb_hint_id,
        tmdb_hint_type=scrape_type if tmdb_hint_id else "",
    )


def _candidate(
    tmdb_id: int,
    title: str,
    *,
    tmdb_type: str = "tv",
    original_title: str = "",
    year: int | None = None,
    score: float = 80,
    popularity: float = 10,
) -> ScrapeCandidate:
    return ScrapeCandidate(
        candidate_id=f"candidate-{tmdb_id}",
        scrape_target_id="quality-target",
        provider="tmdb",
        tmdb_id=tmdb_id,
        tmdb_type=tmdb_type,
        title=title,
        original_title=original_title or title,
        year=year,
        score=score,
        popularity=popularity,
        reasons=["标题完全匹配"] if title else [],
    )


def _quality_cases() -> list[ScrapeQualityCase]:
    """人工确认的代表性案例；新增规则前应先扩充此语料而非改阈值。"""
    correct_tv = _candidate(101, "CLANNAD", year=2007, score=85)
    safe_lower = _candidate(102, "孤独摇滚！", year=2022, score=82)
    wrong_higher = _candidate(103, "孤独的美食家", year=2012, score=99, popularity=100)
    correct_movie = _candidate(104, "剧场版 少女与战车", tmdb_type="movie", year=2015, score=88)
    romanized = _candidate(105, "Bocchi the Rock!", year=2022, score=45)
    hinted = _candidate(106, "完全不同的搜索标题", year=2020, score=5)
    wrong_tv = _candidate(107, "明日的与一", year=2009, score=99, popularity=300)
    wrong_movie = _candidate(108, "无关电影", tmdb_type="movie", year=2023, score=99)
    movie_type_mismatch = _candidate(109, "CLANNAD", tmdb_type="movie", year=2007, score=90)
    year_conflict = _candidate(110, "CLANNAD", year=2020, score=95)

    return [
        ScrapeQualityCase("tv_exact_title_year", _target("CLANNAD", year=2007), [correct_tv], 101),
        ScrapeQualityCase(
            "tv_identity_safe_beats_higher_lookalike",
            _target("孤独摇滚！", year=2022),
            [wrong_higher, safe_lower],
            102,
        ),
        ScrapeQualityCase(
            "movie_exact_title",
            _target("剧场版 少女与战车", scrape_type="movie", year=2015),
            [correct_movie],
            104,
        ),
        ScrapeQualityCase(
            "romanized_tv_exact_title",
            _target("Bocchi the Rock!", year=2022),
            [romanized],
            105,
        ),
        ScrapeQualityCase(
            "tmdb_hint_overrides_noisy_title",
            _target("Season 2", year=2020, tmdb_hint_id=106),
            [hinted],
            106,
        ),
        ScrapeQualityCase("no_candidates_requires_review", _target("CLANNAD"), [], None),
        ScrapeQualityCase(
            "tv_type_mismatch_requires_review",
            _target("CLANNAD", year=2007),
            [movie_type_mismatch],
            None,
        ),
        ScrapeQualityCase(
            "unrelated_high_score_tv_requires_review",
            _target("明日同学的水手服", year=2022),
            [wrong_tv],
            None,
        ),
        ScrapeQualityCase(
            "unrelated_high_score_movie_requires_review",
            _target("正确电影", scrape_type="movie", year=2023),
            [wrong_movie],
            None,
        ),
        ScrapeQualityCase(
            "first_season_year_conflict_requires_review",
            _target("CLANNAD", year=2007),
            [year_conflict],
            None,
        ),
    ]


def test_existing_auto_scrape_rules_meet_numeric_quality_gate():
    """规则库必须同时满足高召回与高精确率，不能为简化而牺牲任一指标。"""
    report = evaluate_auto_decision_quality(_quality_cases(), decide_auto_candidate)

    assert report.recall >= 0.95, report
    assert report.precision >= 0.98, report
    assert report.failed_cases == (), report


def test_auto_decision_evidence_records_observations_without_changing_decision():
    target = _target("孤独摇滚！", year=2022)
    selected = _candidate(201, "孤独摇滚！", year=2022, score=80)
    higher_lookalike = _candidate(202, "孤独的美食家", year=2012, score=95)

    decision, reason = decide_auto_candidate(target, [higher_lookalike, selected])
    assert decision is selected

    evidence = _build_auto_decision_evidence(target, [higher_lookalike, selected], decision, reason)
    persisted = _candidate_identity_evidence(decision, auto_decision=evidence)

    assert evidence == {
        "schema_version": 1,
        "candidate_count": 2,
        "selected_tmdb_id": 201,
        "selected_tmdb_type": "tv",
        "selected_score": 80,
        "selected_rank_by_score": 2,
        "top_score": 95,
        "runner_up_score": 95,
        "selected_score_margin": -15,
        "tmdb_hint_matched": False,
        "identity_verified": True,
        "identity_verification_path": "标题完全一致",
        "adoption_reason": "最高分候选，自动采用",
    }
    assert persisted["auto_decision"] == evidence
