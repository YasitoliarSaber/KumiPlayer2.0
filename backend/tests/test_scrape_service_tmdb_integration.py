# -*- coding: utf-8 -*-
"""Scrape service 与新版 TMDBClient 契约回归测试"""

from pathlib import Path
import time

import pytest

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.scrape.models import ScrapeTarget
from app.scrape.service import execute_scrape, search_candidates
from app.scrape.tmdb_client import TMDBAuthError
import app.scrape.service as scrape_service


class NewStyleTMDBClient:
    """只实现新版 TMDBClient 契约，不提供 get_tv_images/get_movie_images。"""

    def get_tv_detail(self, tmdb_id):
        return {
            "id": tmdb_id,
            "name": "测试番剧",
            "original_name": "Test Anime",
            "first_air_date": "2024-01-01",
            "overview": "剧情简介",
            "vote_average": 8.6,
            "genres": [{"name": "Animation"}, {"name": "Drama"}],
            "production_companies": [{"name": "Kyoto Animation"}],
            "episode_run_time": [24],
            "images": {
                "posters": [{"file_path": "/poster.jpg", "iso_639_1": "zh"}],
                "backdrops": [{"file_path": "/fanart.jpg", "iso_639_1": None}],
                "logos": [{"file_path": "/logo.svg", "iso_639_1": "zh", "file_type": ".svg"}],
            },
        }

    def get_movie_detail(self, tmdb_id):
        return {
            "id": tmdb_id,
            "title": "测试电影",
            "original_title": "Test Movie",
            "release_date": "2024-02-03",
            "overview": "电影简介",
            "vote_average": 7.8,
            "genres": [{"name": "Science Fiction"}],
            "production_companies": [{"name": "Test Studio"}],
            "runtime": 96,
            "images": {
                "posters": [{"file_path": "/movie-poster.jpg", "iso_639_1": "zh"}],
                "backdrops": [{"file_path": "/movie-fanart.jpg", "iso_639_1": None}],
                "logos": [],
            },
        }

    def get_tv_season_detail(self, tmdb_id, season_number):
        return {
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "时间上的跳跃者",
                    "overview": "第一集简介",
                    "runtime": 24,
                    "air_date": "2024-01-01",
                    "still_path": "/still-01.jpg",
                }
            ]
        }

    def select_best_poster(self, images):
        return images["posters"][0]["file_path"]

    def select_best_backdrop(self, images):
        return images["backdrops"][0]["file_path"]

    def select_best_logo(self, images):
        logos = images.get("logos") or []
        return logos[0]["file_path"] if logos else None

    def download_image(self, file_path, dest: Path, size=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"downloaded:{file_path}".encode("utf-8"))
        return True

    def search_tv(self, query, year=None):
        return []

    def search_movie(self, query, year=None):
        return []


def test_execute_scrape_uses_detail_images_and_new_selection_methods(tmp_path, monkeypatch):
    """execute_scrape 不应再依赖旧的 get_tv_images 方法。"""

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    target = ScrapeTarget(
        scrape_target_id="t1",
        source="pan115",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(tmp_path),
    )

    emitted_logs = []
    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=NewStyleTMDBClient(),
        artwork_mode="local",
        log_callback=lambda message, kind="info": emitted_logs.append((message, kind)),
    )

    assert result["poster_path"].endswith("poster.jpg")
    assert result["fanart_path"].endswith("fanart.jpg")
    assert result["clearlogo_path"].endswith("clearlogo.svg")
    assert (tmp_path / "poster.jpg").exists()
    assert (tmp_path / "fanart.jpg").exists()
    assert (tmp_path / "clearlogo.svg").exists()
    assert (tmp_path / "tvshow.nfo").exists()
    nfo = (tmp_path / "tvshow.nfo").read_text(encoding="utf-8")
    assert "<rating>8.6</rating>" in nfo
    assert "<genre>Animation</genre>" in nfo
    assert "<genre>Drama</genre>" in nfo
    assert "<studio>Kyoto Animation</studio>" in nfo
    assert "<premiered>2024-01-01</premiered>" in nfo
    assert "<runtime>24</runtime>" in nfo
    messages = [message for message, _ in emitted_logs]
    assert "TMDB 详情完成：测试番剧" in messages
    assert "海报完成：测试番剧" in messages
    assert "背景图完成：测试番剧" in messages
    assert "Logo 完成：测试番剧" in messages
    assert "剧集 NFO 完成：测试番剧" in messages
    assert "刮削映射完成：测试番剧" in messages
    assert not any(message.startswith(("获取 ", "下载", "写入", "记录")) for message in messages)


def test_execute_scrape_refreshes_existing_artwork_and_episode_still(tmp_path, monkeypatch):
    """重新刮削当前 TMDB 身份时，旧错图不能因文件已存在而被继续复用。"""

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    season_dir = tmp_path / "Season 1"
    season_dir.mkdir()
    poster = tmp_path / "poster.jpg"
    fanart = tmp_path / "fanart.jpg"
    logo = tmp_path / "clearlogo.svg"
    thumb = season_dir / "S01E01-thumb.jpg"
    for path in (poster, fanart, logo, thumb):
        path.write_bytes(b"stale-wrong-identity")

    plan = ImportPlan(
        plan_id="p-refresh-artwork",
        source="baidu",
        status="confirmed",
        items=[
            ImportPlanItem(
                id="ep-refresh-01",
                plan_id="p-refresh-artwork",
                source="baidu",
                resource_type="video",
                action="generate_strm",
                work_id="w-refresh",
                work_title="测试番剧",
                group_type="season",
                season_number=1,
                episode_number=1,
                series_group="测试番剧",
                card_type="main_series",
                media_type="tv",
                target_strm_path=str(season_dir / "S01E01.strm"),
            )
        ],
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    target = ScrapeTarget(
        scrape_target_id="t-refresh-artwork",
        source="baidu",
        import_plan_id=plan.plan_id,
        work_id="w-refresh",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(season_dir),
        item_ids=["ep-refresh-01"],
    )

    execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=NewStyleTMDBClient(),
        artwork_mode="local",
        include_episode=True,
    )

    assert poster.read_bytes() == b"downloaded:/poster.jpg"
    assert fanart.read_bytes() == b"downloaded:/fanart.jpg"
    assert logo.read_bytes() == b"downloaded:/logo.svg"
    assert thumb.read_bytes() == b"downloaded:/still-01.jpg"


def test_execute_scrape_uses_remote_artwork_for_cloud_source_in_auto_mode(tmp_path, monkeypatch):
    """auto 模式下网盘来源保存远程图片 URL，避免测试依赖全局配置缓存。"""

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    target = ScrapeTarget(
        scrape_target_id="t-remote-artwork",
        source="pan115",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(tmp_path),
    )

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=NewStyleTMDBClient(),
        artwork_mode="auto",
    )

    assert result["poster_path"].endswith("/w780/poster.jpg")
    assert result["fanart_path"].endswith("/original/fanart.jpg")
    assert result["clearlogo_path"].endswith("/original/logo.svg")
    assert not (tmp_path / "poster.jpg").exists()
    assert not (tmp_path / "fanart.jpg").exists()
    assert not (tmp_path / "clearlogo.svg").exists()
    assert (tmp_path / "tvshow.nfo").exists()


def test_execute_scrape_writes_second_season_episode_nfo(tmp_path, monkeypatch):
    """本地第二季应读取 TMDB Season 2，并写成 S02E01.nfo。"""

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    plan = ImportPlan(
        plan_id="p-s2",
        source="pan115",
        items=[
            ImportPlanItem(
                id="ep-s2-01",
                plan_id="p-s2",
                source="pan115",
                resource_type="video",
                action="generate_strm",
                group_type="season",
                season_number=2,
                episode_number=1,
                title="S02E01",
                target_strm_path=str(tmp_path / "S02E01.strm"),
            )
        ],
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class SecondSeasonClient(NewStyleTMDBClient):
        def __init__(self):
            self.season_numbers = []

        def get_tv_detail(self, tmdb_id):
            detail = super().get_tv_detail(tmdb_id)
            detail["seasons"] = [
                {"season_number": 1, "air_date": "2022-01-01"},
                {"season_number": 2, "air_date": "2024-01-01"},
            ]
            return detail

        def get_tv_season_detail(self, tmdb_id, season_number):
            self.season_numbers.append(season_number)
            return {
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "第二季第一集标题",
                        "overview": "第二季第一集简介",
                        "runtime": 24,
                        "air_date": "2024-01-08",
                        "still_path": "/s2e1.jpg",
                    }
                ]
            }

    target = ScrapeTarget(
        scrape_target_id="s2",
        source="pan115",
        import_plan_id="p-s2",
        work_id="w-s2",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=2,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(tmp_path),
        item_ids=["ep-s2-01"],
    )
    client = SecondSeasonClient()

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=2,
        tmdb_client=client,
        include_episode=True,
    )

    episode_nfo = tmp_path / "S02E01.nfo"
    assert client.season_numbers
    assert set(client.season_numbers) == {2}
    assert result["episode_count"] == 1
    assert episode_nfo.exists()
    content = episode_nfo.read_text(encoding="utf-8")
    assert "<title>第二季第一集标题</title>" in content
    assert "<season>2</season>" in content
    assert "<episode>1</episode>" in content


def test_search_candidates_prefers_tmdb_hint_id():
    """{tmdb-...} hint 应直接生成高分候选，避免标题猜错季。"""
    target = ScrapeTarget(
        scrape_target_id="t-hint",
        source="baidu",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="Re: 从零开始的异世界生活",
        local_title="Re: 从零开始的异世界生活",
        local_season_number=2,
        scrape_title="从零开始的异世界生活",
        scrape_year=2016,
        scrape_type="tv",
        tmdb_hint_id=65942,
        tmdb_hint_type="tv",
    )

    candidates = search_candidates(target, tmdb_client=NewStyleTMDBClient())

    assert candidates
    assert candidates[0].tmdb_id == 65942
    assert candidates[0].tmdb_type == "tv"
    assert candidates[0].score >= 150
    assert any("TMDB ID 命中" in reason for reason in candidates[0].reasons)


def test_search_candidates_propagates_tmdb_auth_error(monkeypatch):
    """TMDB 认证错误不能被并发候选搜索吞成“无候选”。"""
    target = ScrapeTarget(
        scrape_target_id="t-auth",
        source="baidu",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        show_type="anime_series",
        group_type="season",
        series_group="Angel Beats!",
        local_title="Angel Beats!",
        local_season_number=1,
        scrape_title="Angel Beats!",
        scrape_year=2010,
        scrape_type="tv",
    )

    class AuthFailingTMDBClient:
        def search_tv(self, query, year=None):
            raise TMDBAuthError("未配置 tmdb_bearer_token")

        def search_movie(self, query, year=None):
            raise TMDBAuthError("未配置 tmdb_bearer_token")

    monkeypatch.setattr(scrape_service, "_should_use_anilist", lambda t: True)
    monkeypatch.setattr(scrape_service, "_search_anilist_candidates", lambda *args, **kwargs: [])

    with pytest.raises(TMDBAuthError, match="tmdb_bearer_token"):
        search_candidates(target, tmdb_client=AuthFailingTMDBClient())


def test_search_candidates_reuses_trusted_cached_candidate_without_network(monkeypatch):
    """自动刮削应复用同目标的强候选，避免外部服务波动被误报为无候选。"""
    target = ScrapeTarget(
        scrape_target_id="t-tsuki-ga-kirei",
        source="pan115",
        import_plan_id="p1",
        work_id="w-tsuki-ga-kirei",
        card_type="main_series",
        media_type="tv",
        show_type="anime_series",
        group_type="season",
        series_group="月色真美",
        local_title="月色真美",
        local_season_number=1,
        scrape_title="月色真美",
        scrape_year=2017,
        scrape_type="tv",
    )
    monkeypatch.setattr("app.db.candidates.list_candidates", lambda target_id: [{
        "scrape_target_id": target_id,
        "provider": "tmdb",
        "tmdb_id": 70880,
        "tmdb_type": "tv",
        "title": "月色真美",
        "original_title": "月がきれい",
        "year": 2017,
        "overview": "青春期之恋",
        "poster_path": "/poster.jpg",
        "popularity": 8.0,
        "vote_average": 8.0,
        "score": 101.7,
        "reasons": '["标题完全匹配", "年份匹配"]',
        "cached_at": scrape_service._now_iso(),
    }])

    class NetworkMustNotRun:
        def search_tv(self, query, year=None):
            raise AssertionError("强缓存存在时不应再次联网")

        def search_movie(self, query, year=None):
            raise AssertionError("强缓存存在时不应再次联网")

    candidates = search_candidates(
        target,
        tmdb_client=NetworkMustNotRun(),
        anilist_client=False,
        bangumi_client=False,
    )

    assert len(candidates) == 1
    assert candidates[0].tmdb_id == 70880
    assert candidates[0].poster_path == "/poster.jpg"
    assert "复用本地候选缓存" in candidates[0].reasons


def test_search_candidates_does_not_trust_weak_cached_candidate(monkeypatch):
    """低分缓存不能阻止正常联网搜索，避免固化历史误匹配。"""
    target = ScrapeTarget(
        scrape_target_id="t-weak-cache",
        source="pan115",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        show_type="anime_series",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
    )
    monkeypatch.setattr("app.db.candidates.list_candidates", lambda target_id: [{
        "scrape_target_id": target_id,
        "provider": "tmdb",
        "tmdb_id": 999,
        "tmdb_type": "tv",
        "title": "错误作品",
        "original_title": "Wrong Show",
        "year": 2024,
        "overview": "",
        "poster_path": "/wrong.jpg",
        "popularity": 1.0,
        "vote_average": 1.0,
        "score": 69,
        "reasons": "[]",
        "cached_at": scrape_service._now_iso(),
    }])
    monkeypatch.setattr(scrape_service, "_cache_candidates", lambda candidates: None)
    monkeypatch.setattr(scrape_service, "save_failed_case", lambda case: None)

    client = NewStyleTMDBClient()
    calls = []
    original_search = client.search_tv

    def record_search(query, year=None):
        calls.append((query, year))
        return original_search(query, year)

    client.search_tv = record_search
    candidates = search_candidates(
        target,
        tmdb_client=client,
        anilist_client=False,
        bangumi_client=False,
    )

    assert candidates == []
    assert calls


def test_search_candidates_recovers_tmdb_hint_from_sibling_target_cache(monkeypatch):
    """S00 等新目标可按明确 TMDB hint 复用同作品其他季度的缓存。"""
    target = ScrapeTarget(
        scrape_target_id="t-railgun-specials",
        source="pan115",
        import_plan_id="p1",
        work_id="w-railgun",
        card_type="main_series",
        media_type="tv",
        show_type="anime_series",
        group_type="special",
        series_group="某科学的超电磁炮",
        local_title="某科学的超电磁炮",
        local_season_number=0,
        scrape_title="某科学的超电磁炮",
        scrape_year=2009,
        scrape_type="tv",
        tmdb_hint_id=30977,
        tmdb_hint_type="tv",
    )
    monkeypatch.setattr("app.db.candidates.list_candidates", lambda target_id: [])
    monkeypatch.setattr("app.db.candidates.list_candidates_by_tmdb_identity", lambda tmdb_id, tmdb_type: [{
        "scrape_target_id": "t-railgun-season-1",
        "provider": "tmdb",
        "tmdb_id": tmdb_id,
        "tmdb_type": tmdb_type,
        "title": "某科学的超电磁炮",
        "original_title": "とある科学の超電磁砲",
        "year": 2009,
        "overview": "学园都市",
        "poster_path": "/railgun.jpg",
        "popularity": 27.7,
        "vote_average": 7.4,
        "score": 103.3,
        "reasons": '["标题完全匹配", "年份匹配"]',
        "cached_at": scrape_service._now_iso(),
    }])

    candidates = search_candidates(
        target,
        tmdb_client=object(),
        anilist_client=False,
        bangumi_client=False,
    )

    assert len(candidates) == 1
    assert candidates[0].scrape_target_id == target.scrape_target_id
    assert candidates[0].tmdb_id == target.tmdb_hint_id
    assert candidates[0].score >= 150
    assert any("TMDB ID 命中" in reason for reason in candidates[0].reasons)


def test_search_candidates_does_not_leave_timed_out_provider_running(monkeypatch):
    """候选搜索返回时，超时提供方线程也必须已经退出，不能污染下一任务。"""

    target = ScrapeTarget(
        scrape_target_id="t-timeout",
        source="baidu",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        show_type="anime_series",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
    )
    candidate = scrape_service.ScrapeCandidate(
        candidate_id="c-fast",
        scrape_target_id=target.scrape_target_id,
        provider="tmdb",
        tmdb_id=123,
        tmdb_type="tv",
        title="测试番剧",
        year=2024,
        score=80,
    )

    monkeypatch.setattr(scrape_service, "_should_use_anilist", lambda t: True)
    monkeypatch.setattr(scrape_service, "_candidate_search_timeout", lambda: 0.05)
    monkeypatch.setattr(scrape_service, "_cache_candidates", lambda candidates: None)
    monkeypatch.setattr(scrape_service, "save_failed_case", lambda case: None)
    monkeypatch.setattr(scrape_service, "_search_tmdb_candidates", lambda *args, **kwargs: [candidate])

    anilist_finished = False

    def slow_anilist(*args, **kwargs):
        nonlocal anilist_finished
        time.sleep(0.25)
        anilist_finished = True
        return []

    monkeypatch.setattr(scrape_service, "_search_anilist_candidates", slow_anilist)

    started = time.monotonic()
    candidates = search_candidates(target, tmdb_client=NewStyleTMDBClient(), anilist_client=object())
    elapsed = time.monotonic() - started

    assert elapsed < 0.15
    assert anilist_finished is False
    assert candidates == [candidate]


def test_execute_scrape_writes_enriched_movie_nfo(tmp_path, monkeypatch):
    """movie.nfo 应写入 TMDB 详情中的增强字段。"""

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    target = ScrapeTarget(
        scrape_target_id="m1",
        source="pan115",
        import_plan_id="p1",
        work_id="movie1",
        card_type="standalone",
        media_type="movie",
        group_type="movie",
        series_group="测试系列",
        local_title="测试电影",
        scrape_title="测试电影",
        scrape_year=2024,
        scrape_type="movie",
        target_dir=str(tmp_path),
    )

    result = execute_scrape(
        target=target,
        tmdb_id=456,
        tmdb_type="movie",
        tmdb_client=NewStyleTMDBClient(),
    )

    assert result["nfo_path"].endswith("movie.nfo")
    nfo = (tmp_path / "movie.nfo").read_text(encoding="utf-8")
    assert "<rating>7.8</rating>" in nfo
    assert "<genre>Science Fiction</genre>" in nfo
    assert "<studio>Test Studio</studio>" in nfo
    assert "<releasedate>2024-02-03</releasedate>" in nfo
    assert "<runtime>96</runtime>" in nfo


def test_execute_scrape_uses_series_root_assets_for_tv_season(tmp_path, monkeypatch):
    """Season targets should share series-level artwork instead of duplicating it per season."""

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    series_dir = tmp_path / "Re：从零开始的异世界生活"
    season_dir = series_dir / "Season 2"

    target = ScrapeTarget(
        scrape_target_id="t-shared-assets",
        source="baidu",
        import_plan_id="p1",
        work_id="rezero",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="Re：从零开始的异世界生活",
        local_title="Re：从零开始的异世界生活",
        local_season_number=2,
        scrape_title="Re：从零开始的异世界生活",
        scrape_year=2020,
        scrape_type="tv",
        target_dir=str(season_dir),
    )

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=NewStyleTMDBClient(),
        artwork_mode="local",
        include_episode=False,
    )

    assert result["poster_path"] == str(series_dir / "poster.jpg")
    assert result["fanart_path"] == str(series_dir / "fanart.jpg")
    assert result["clearlogo_path"] == str(series_dir / "clearlogo.svg")
    assert (series_dir / "poster.jpg").exists()
    assert (series_dir / "fanart.jpg").exists()
    assert (series_dir / "clearlogo.svg").exists()
    assert not (season_dir / "poster.jpg").exists()
    assert not (season_dir / "fanart.jpg").exists()
    assert not (season_dir / "clearlogo.svg").exists()
    assert (season_dir / "tvshow.nfo").exists()


def test_execute_scrape_writes_episode_nfo_without_downloading_still(tmp_path, monkeypatch):
    """分集只写 NFO 标题，不下载本地剧照图片。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    season_dir = tmp_path / "Season 1"
    season_dir.mkdir()
    strm_path = season_dir / "测试番剧 - S01E01.strm"
    strm_path.write_text("H:\\test\\ep01.mkv", encoding="utf-8")

    plan = ImportPlan(
        plan_id="p1",
        source="pan115",
        status="confirmed",
        items=[
            ImportPlanItem(
                id="ep1",
                plan_id="p1",
                raw_file_id="r1",
                source="pan115",
                relative_path="动画/测试番剧/01.mkv",
                real_path="H:\\test\\ep01.mkv",
                resource_type="video",
                action="generate_strm",
                work_id="w1",
                work_title="测试番剧",
                group_type="season",
                season_number=1,
                episode_number=1,
                series_group="测试番剧",
                card_type="main_series",
                media_type="tv",
                target_strm_path=str(strm_path),
            )
        ],
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    target = ScrapeTarget(
        scrape_target_id="t_episode",
        source="pan115",
        import_plan_id="p1",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(tmp_path),
        item_ids=["ep1"],
    )

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=NewStyleTMDBClient(),
        include_episode=True,
    )

    episode_nfo = season_dir / "S01E01.nfo"
    assert episode_nfo.exists()
    text = episode_nfo.read_text(encoding="utf-8")
    assert "<title>时间上的跳跃者</title>" in text
    assert "<plot>第一集简介</plot>" in text
    episode_thumb = season_dir / "S01E01-thumb.jpg"
    remote_thumb = "https://image.tmdb.org/t/p/w500/still-01.jpg"
    assert not episode_thumb.exists()
    assert result["episode_nfos"][0]["thumb_path"] == remote_thumb
    assert f"<thumb>{remote_thumb}</thumb>" in text


def test_execute_scrape_uses_work_fanart_when_episode_still_is_missing(tmp_path, monkeypatch):
    """TMDB 未提供新集剧照时，NFO 缩略图应回退到已保存的作品背景图。"""
    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    season_dir = tmp_path / "测试番剧" / "Season 1"
    season_dir.mkdir(parents=True)
    plan = ImportPlan(
        plan_id="p-fanart-fallback",
        source="local",
        status="confirmed",
        items=[ImportPlanItem(
            id="ep8",
            plan_id="p-fanart-fallback",
            source="local",
            resource_type="video",
            action="generate_strm",
            work_id="w1",
            work_title="测试番剧",
            group_type="season",
            season_number=1,
            episode_number=8,
            series_group="测试番剧",
            card_type="main_series",
            media_type="tv",
            target_strm_path=str(season_dir / "测试番剧 - S01E08.strm"),
        )],
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class MissingStillClient(NewStyleTMDBClient):
        def get_tv_season_detail(self, tmdb_id, season_number):
            return {"episodes": [{
                "episode_number": 8,
                "name": "第八集标题",
                "overview": "第八集简介",
                "runtime": 24,
                "air_date": "2024-02-19",
                "still_path": "",
            }]}

    target = ScrapeTarget(
        scrape_target_id="t-fanart-fallback",
        source="local",
        import_plan_id=plan.plan_id,
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="测试番剧",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(season_dir),
        item_ids=["ep8"],
    )

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=MissingStillClient(),
        artwork_mode="local",
        include_episode=True,
    )

    fanart = tmp_path / "测试番剧" / "fanart.jpg"
    nfo = season_dir / "S01E08.nfo"
    assert fanart.exists()
    assert nfo.exists()
    assert result["episode_nfos"][0]["thumb_path"] == str(fanart)
    assert f"<thumb>{fanart}</thumb>" in nfo.read_text(encoding="utf-8")


def test_episode_nfo_filename_uses_local_season_when_tmdb_season_differs(tmp_path, monkeypatch):
    """本地 S2 映射独立 TMDB 条目 S1 时，NFO 文件名仍必须跟本地 S02E01 对齐。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    season_dir = tmp_path / "Season 2"
    season_dir.mkdir()
    strm_path = season_dir / "续作 - S02E01.strm"
    strm_path.write_text("H:\\test\\s2e01.mkv", encoding="utf-8")

    plan = ImportPlan(
        plan_id="p2",
        source="pan115",
        status="confirmed",
        items=[
            ImportPlanItem(
                id="ep-s2-1",
                plan_id="p2",
                raw_file_id="r-s2-1",
                source="pan115",
                relative_path="动画/测试系列/S2/01.mkv",
                real_path="H:\\test\\s2e01.mkv",
                resource_type="video",
                action="generate_strm",
                work_id="w1",
                work_title="测试系列",
                group_type="season",
                season_number=2,
                episode_number=1,
                series_group="测试系列",
                card_type="main_series",
                media_type="tv",
                target_strm_path=str(strm_path),
            )
        ],
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class SeasonOneOnlyClient(NewStyleTMDBClient):
        def get_tv_detail(self, tmdb_id):
            detail = super().get_tv_detail(tmdb_id)
            detail["seasons"] = [{"season_number": 1, "name": "Season 1", "episode_count": 1}]
            return detail

        def get_tv_season_detail(self, tmdb_id, season_number):
            assert season_number == 1
            return super().get_tv_season_detail(tmdb_id, season_number)

    target = ScrapeTarget(
        scrape_target_id="t_local_s2_tmdb_s1",
        source="pan115",
        import_plan_id="p2",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="测试系列",
        local_title="测试系列",
        local_season_number=2,
        scrape_title="测试系列 续作",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(tmp_path),
        item_ids=["ep-s2-1"],
    )

    result = execute_scrape(
        target=target,
        tmdb_id=999,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=SeasonOneOnlyClient(),
        include_episode=True,
    )

    local_nfo = season_dir / "S02E01.nfo"
    assert local_nfo.exists()
    assert not (season_dir / "S01E01.nfo").exists()
    text = local_nfo.read_text(encoding="utf-8")
    assert "<season>2</season>" in text
    assert "<episode>1</episode>" in text
    assert "<title>时间上的跳跃者</title>" in text
    assert result["episode_nfos"][0]["episode"] == "S02E01"
    assert result["episode_nfos"][0]["tmdb_episode"] == "S01E01"


def test_episode_nfo_uses_absolute_tmdb_episode_when_local_seasons_are_split(tmp_path, monkeypatch):
    """本地按季拆分、TMDB 按绝对集数连续时，应把 S02E01 映射到 TMDB E26。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    season1_dir = tmp_path / "Season 1"
    season2_dir = tmp_path / "Season 2"
    season1_dir.mkdir()
    season2_dir.mkdir()

    items = []
    for episode_number in range(1, 26):
        items.append(ImportPlanItem(
            id=f"ep-s1-{episode_number:02d}",
            plan_id="p-absolute",
            source="pan115",
            resource_type="video",
            action="generate_strm",
            work_id="w1",
            work_title="长篇测试番剧",
            group_type="season",
            season_number=1,
            episode_number=episode_number,
            series_group="长篇测试番剧",
            card_type="main_series",
            media_type="tv",
            target_strm_path=str(season1_dir / f"S01E{episode_number:02d}.strm"),
        ))
    items.append(ImportPlanItem(
        id="ep-s2-01",
        plan_id="p-absolute",
        source="pan115",
        resource_type="video",
        action="generate_strm",
        work_id="w1",
        work_title="长篇测试番剧",
        group_type="season",
        season_number=2,
        episode_number=1,
        series_group="长篇测试番剧",
        card_type="main_series",
        media_type="tv",
        target_strm_path=str(season2_dir / "S02E01.strm"),
    ))

    plan = ImportPlan(
        plan_id="p-absolute",
        source="pan115",
        status="confirmed",
        items=items,
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class AbsoluteSeasonClient(NewStyleTMDBClient):
        def get_tv_detail(self, tmdb_id):
            detail = super().get_tv_detail(tmdb_id)
            detail["seasons"] = [{"season_number": 1, "name": "Season 1", "episode_count": 50}]
            return detail

        def get_tv_season_detail(self, tmdb_id, season_number):
            assert season_number == 1
            return {
                "episodes": [
                    {
                        "episode_number": episode_number,
                        "name": f"绝对第{episode_number}集标题",
                        "overview": f"绝对第{episode_number}集简介",
                        "runtime": 24,
                        "air_date": "2024-01-01",
                        "still_path": "",
                    }
                    for episode_number in range(1, 51)
                ]
            }

    target = ScrapeTarget(
        scrape_target_id="t-absolute-s2",
        source="pan115",
        import_plan_id="p-absolute",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="长篇测试番剧",
        local_title="长篇测试番剧",
        local_season_number=2,
        scrape_title="长篇测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(season2_dir),
        item_ids=["ep-s2-01"],
    )

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=AbsoluteSeasonClient(),
        include_episode=True,
    )

    local_nfo = season2_dir / "S02E01.nfo"
    assert local_nfo.exists()
    text = local_nfo.read_text(encoding="utf-8")
    assert "<season>2</season>" in text
    assert "<episode>1</episode>" in text
    assert "<title>绝对第26集标题</title>" in text
    assert "<plot>绝对第26集简介</plot>" in text
    assert result["episode_nfos"][0]["episode"] == "S02E01"
    assert result["episode_nfos"][0]["tmdb_episode"] == "S01E26"


def test_episode_nfo_does_not_add_offset_when_local_episode_is_already_absolute(tmp_path, monkeypatch):
    """本地已经按 26、27 连续编号时，不应再把 S02E26 映射成 TMDB E51。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)

    season1_dir = tmp_path / "Season 1"
    season2_dir = tmp_path / "Season 2"
    season1_dir.mkdir()
    season2_dir.mkdir()

    items = [
        ImportPlanItem(
            id=f"ep-s1-{episode_number:02d}",
            plan_id="p-absolute-local",
            source="pan115",
            resource_type="video",
            action="generate_strm",
            work_id="w1",
            work_title="长篇测试番剧",
            group_type="season",
            season_number=1,
            episode_number=episode_number,
            series_group="长篇测试番剧",
            card_type="main_series",
            media_type="tv",
            target_strm_path=str(season1_dir / f"S01E{episode_number:02d}.strm"),
        )
        for episode_number in range(1, 26)
    ]
    items.append(ImportPlanItem(
        id="ep-s2-26",
        plan_id="p-absolute-local",
        source="pan115",
        resource_type="video",
        action="generate_strm",
        work_id="w1",
        work_title="长篇测试番剧",
        group_type="season",
        season_number=2,
        episode_number=26,
        series_group="长篇测试番剧",
        card_type="main_series",
        media_type="tv",
        target_strm_path=str(season2_dir / "S02E26.strm"),
    ))

    plan = ImportPlan(
        plan_id="p-absolute-local",
        source="pan115",
        status="confirmed",
        items=items,
    )
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class AbsoluteSeasonClient(NewStyleTMDBClient):
        def get_tv_detail(self, tmdb_id):
            detail = super().get_tv_detail(tmdb_id)
            detail["seasons"] = [{"season_number": 1, "name": "Season 1", "episode_count": 50}]
            return detail

        def get_tv_season_detail(self, tmdb_id, season_number):
            assert season_number == 1
            return {
                "episodes": [
                    {
                        "episode_number": episode_number,
                        "name": f"绝对第{episode_number}集标题",
                        "overview": f"绝对第{episode_number}集简介",
                        "runtime": 24,
                        "air_date": "2024-01-01",
                        "still_path": "",
                    }
                    for episode_number in range(1, 51)
                ]
            }

    target = ScrapeTarget(
        scrape_target_id="t-absolute-local-s2",
        source="pan115",
        import_plan_id="p-absolute-local",
        work_id="w1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="长篇测试番剧",
        local_title="长篇测试番剧",
        local_season_number=2,
        scrape_title="长篇测试番剧",
        scrape_year=2024,
        scrape_type="tv",
        target_dir=str(season2_dir),
        item_ids=["ep-s2-26"],
    )

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=AbsoluteSeasonClient(),
        include_episode=True,
    )

    local_nfo = season2_dir / "S02E26.nfo"
    assert local_nfo.exists()
    text = local_nfo.read_text(encoding="utf-8")
    assert "<title>绝对第26集标题</title>" in text
    assert result["episode_nfos"][0]["episode"] == "S02E26"
    assert result["episode_nfos"][0]["tmdb_episode"] == "S01E26"
