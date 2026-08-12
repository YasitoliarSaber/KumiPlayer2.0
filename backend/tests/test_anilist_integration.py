# -*- coding: utf-8 -*-
"""AniList mixed scrape integration tests."""

from app.scrape.models import ScrapeCandidate
from app.scrape.models import ScrapeTarget
import app.scrape.service as scrape_service
from app.scrape.service import search_candidates
from app.scrape.anilist_candidates import search_anilist_candidates


class EmptyTMDBClient:
    def search_tv(self, query, year=None):
        return []

    def search_movie(self, query, year=None):
        return []


class ResolvingTMDBClient:
    def search_tv(self, query, year=None):
        return [
            {
                "id": 280366,
                "name": "正相反的你与我",
                "original_name": "正反対な君と僕",
                "first_air_date": "2026-01-01",
                "overview": "测试简介",
                "poster_path": "/tmdb.jpg",
                "popularity": 8,
                "vote_average": 8.4,
                "media_type": "tv",
            }
        ]

    def search_movie(self, query, year=None):
        return []


class FakeAniListClient:
    def __init__(self, media):
        self.media = media
        self.called = False
        self.closed = False

    def search_anime(self, query, year=None, per_page=10):
        self.called = True
        return self.media

    def close(self):
        self.closed = True


class RaisingAniListClient:
    def search_anime(self, query, year=None, per_page=10):
        raise AssertionError("AniList should not be called")


def _anime_target() -> ScrapeTarget:
    return ScrapeTarget(
        scrape_target_id="anime-1",
        source="baidu",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        show_type="anime_series",
        group_type="season",
        series_group="正相反的你与我",
        local_title="正相反的你与我",
        scrape_title="Seihantai na Kimi to Boku",
        scrape_year=2026,
        scrape_type="tv",
        item_ids=["e1", "e2"],
    )


def test_anilist_search_stops_after_confident_direct_tmdb_match():
    """首个查询已有高置信度直连候选时，不应继续重复搜索其他标题变体。"""
    target = _anime_target()

    class RecordingAniListClient(FakeAniListClient):
        calls = 0

        def search_anime(self, query, year=None, per_page=10):
            self.calls += 1
            return self.media

    anilist = RecordingAniListClient([{
        "id": 185660,
        "title": {"native": "正反対な君と僕", "userPreferred": "正反対な君と僕"},
        "format": "TV",
        "seasonYear": 2026,
        "externalLinks": [{"site": "The Movie Database", "url": "https://www.themoviedb.org/tv/280366"}],
    }])

    def candidate_factory(result, scrape_target, tmdb_type):
        return ScrapeCandidate(
            scrape_target_id=scrape_target.scrape_target_id,
            tmdb_id=result["id"],
            tmdb_type=tmdb_type,
            title=result.get("name") or "",
            score=80,
        )

    candidates = search_anilist_candidates(
        target,
        None,
        None,
        EmptyTMDBClient(),
        anilist,
        search_queries=["正相反的你与我", "Seihantai", "正反対な君と僕"],
        tmdb_result_to_candidate=candidate_factory,
    )

    assert candidates
    assert anilist.calls == 1


def test_anilist_fallback_tmdb_resolution_has_request_budget():
    """AniList 无 TMDB 直链时，回查 TMDB 必须有固定上限。"""
    target = _anime_target()
    media = [
        {
            "id": index,
            "title": {
                "native": f"候选 {index}",
                "romaji": f"Candidate {index}",
                "english": f"English {index}",
            },
            "format": "TV",
            "seasonYear": 2026,
            "externalLinks": [],
        }
        for index in range(8)
    ]
    anilist = FakeAniListClient(media)

    class CountingTMDBClient(EmptyTMDBClient):
        calls = 0

        def search_tv(self, query, year=None):
            self.calls += 1
            return []

    tmdb = CountingTMDBClient()
    search_anilist_candidates(
        target,
        None,
        None,
        tmdb,
        anilist,
        search_queries=["查询一", "查询二", "查询三"],
        tmdb_result_to_candidate=lambda *_: ScrapeCandidate(),
    )

    assert tmdb.calls <= 8


def test_anilist_candidate_with_tmdb_external_link_is_executable():
    target = _anime_target()
    anilist = FakeAniListClient([
        {
            "id": 185660,
            "title": {
                "romaji": "Seihantai na Kimi to Boku",
                "english": "You and I Are Polar Opposites",
                "native": "正反対な君と僕",
                "userPreferred": "正反対な君と僕",
            },
            "synonyms": ["正相反的你与我"],
            "format": "TV",
            "seasonYear": 2026,
            "startDate": {"year": 2026},
            "episodes": 12,
            "averageScore": 84,
            "popularity": 3000,
            "description": "动画简介",
            "coverImage": {"large": "https://img.anilist.co/poster.jpg"},
            "externalLinks": [
                {
                    "site": "The Movie Database",
                    "url": "https://www.themoviedb.org/tv/280366",
                }
            ],
        }
    ])

    candidates = search_candidates(
        target,
        tmdb_client=EmptyTMDBClient(),
        anilist_client=anilist,
    )

    assert anilist.called
    assert candidates
    assert candidates[0].provider == "anilist"
    assert candidates[0].tmdb_id == 280366
    assert candidates[0].tmdb_type == "tv"
    assert candidates[0].poster_path.startswith("https://")
    assert candidates[0].raw["canonical_assets"]["poster.jpg"].startswith("https://")
    assert any("AniList 命中" in reason for reason in candidates[0].reasons)


def test_high_confidence_tmdb_candidate_still_runs_anilist_for_anime():
    target = _anime_target()

    class FastTMDBClient:
        def search_tv(self, title, year=None):
            return [{
                "id": 280366,
                "name": "Seihantai na Kimi to Boku",
                "original_name": "正反対な君と僕",
                "first_air_date": "2026-01-01",
                "popularity": 30,
                "vote_average": 8.0,
            }]

    class RecordingAniListClient:
        def __init__(self):
            self.called = False

        def search_anime(self, *args, **kwargs):
            self.called = True
            return []

        def close(self):
            pass

    anilist = RecordingAniListClient()
    candidates = search_candidates(
        target,
        tmdb_client=FastTMDBClient(),
        anilist_client=anilist,
    )

    assert anilist.called is True
    assert candidates
    assert candidates[0].provider == "tmdb"
    assert candidates[0].tmdb_id == 280366


def test_low_confidence_tmdb_candidate_uses_anilist_fallback():
    target = _anime_target()

    class WeakTMDBClient:
        def search_tv(self, title, year=None):
            if title == "AniList Resolved":
                return [{
                    "id": 280366,
                    "name": "AniList Resolved",
                    "original_name": "AniList Resolved",
                    "first_air_date": "2026-01-01",
                    "popularity": 20,
                    "vote_average": 8.0,
                }]
            return [{
                "id": 1,
                "name": "Unrelated",
                "original_name": "Unrelated",
                "first_air_date": "2026-01-01",
                "popularity": 1,
                "vote_average": 5.0,
            }]

    class FallbackAniListClient:
        called = False

        def search_anime(self, *args, **kwargs):
            self.called = True
            return [{
                "id": 999,
                "title": {"romaji": "AniList Resolved", "native": "AniList Resolved"},
                "format": "TV",
                "seasonYear": 2026,
                "description": "",
                "coverImage": {"large": "https://img.anilist.co/poster.jpg"},
                "popularity": 10,
                "averageScore": 80,
            }]

        def close(self):
            pass

    anilist = FallbackAniListClient()
    candidates = search_candidates(
        target,
        tmdb_client=WeakTMDBClient(),
        anilist_client=anilist,
    )

    assert anilist.called
    assert any(candidate.provider == "anilist" and candidate.tmdb_id == 280366 for candidate in candidates)


def test_anilist_candidate_can_resolve_tmdb_by_title():
    target = _anime_target()
    anilist = FakeAniListClient([
        {
            "id": 185660,
            "title": {
                "romaji": "Seihantai na Kimi to Boku",
                "native": "正反対な君と僕",
                "userPreferred": "正反対な君と僕",
            },
            "synonyms": ["正相反的你与我"],
            "format": "TV",
            "seasonYear": 2026,
            "startDate": {"year": 2026},
            "episodes": 12,
            "averageScore": 84,
            "popularity": 3000,
            "description": "动画简介",
            "coverImage": {"large": "https://img.anilist.co/poster.jpg"},
            "externalLinks": [],
        }
    ])

    candidates = search_candidates(
        target,
        tmdb_client=ResolvingTMDBClient(),
        anilist_client=anilist,
    )

    assert candidates
    assert candidates[0].provider == "anilist"
    assert candidates[0].tmdb_id == 280366


def test_live_action_target_skips_anilist():
    target = ScrapeTarget(
        scrape_target_id="live-1",
        source="local",
        media_type="tv",
        show_type="live_series",
        group_type="season",
        series_group="Some Live Show",
        local_title="Some Live Show",
        scrape_title="Some Live Show",
        scrape_year=2024,
        scrape_type="tv",
    )

    candidates = search_candidates(
        target,
        tmdb_client=EmptyTMDBClient(),
        anilist_client=RaisingAniListClient(),
    )

    assert candidates == []


def test_tmdb_failure_does_not_hide_anilist_candidates(monkeypatch):
    target = _anime_target()
    anilist_candidate = ScrapeCandidate(
        candidate_id="ani-1",
        scrape_target_id=target.scrape_target_id,
        provider="anilist",
        tmdb_id=280366,
        tmdb_type="tv",
        title="正反対な君と僕",
        year=2026,
        score=82,
    )

    monkeypatch.setattr(scrape_service, "_candidate_search_timeout", lambda: 1)
    monkeypatch.setattr(scrape_service, "_cache_candidates", lambda candidates: None)
    monkeypatch.setattr(scrape_service, "save_failed_case", lambda case: None)
    monkeypatch.setattr(
        scrape_service,
        "_search_tmdb_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("TMDB 请求超时")),
    )
    monkeypatch.setattr(
        scrape_service,
        "_search_anilist_candidates",
        lambda *args, **kwargs: [anilist_candidate],
    )

    candidates = search_candidates(
        target,
        tmdb_client=EmptyTMDBClient(),
        anilist_client=FakeAniListClient([]),
    )

    assert candidates == [anilist_candidate]
