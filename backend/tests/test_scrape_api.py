# -*- coding: utf-8 -*-
"""M08 Scrape API 测试（增强版）"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_PROJECT_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DATA_DIR = Path(tempfile.gettempdir()) / "kumiplayer_tests" / "test_scrape_api_data"
os.environ["KUMIPLAYER_DATA_DIR"] = str(_DATA_DIR)


def _cleanup():
    from app.tasks.registry import reset_task_manager
    import app.api.scrape as scrape_mod
    reset_task_manager()
    scrape_mod._targets_cache.clear()
    time.sleep(0.5)
    if _DATA_DIR.exists():
        if _DATA_DIR.resolve() == _PROJECT_DATA_DIR.resolve():
            raise RuntimeError("Refusing to delete project data directory from tests")
        try:
            shutil.rmtree(_DATA_DIR)
        except OSError:
            shutil.rmtree(_DATA_DIR, ignore_errors=True)


def _setup_plan(source="pan115", plan_id="p1", status="confirmed"):
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    items = [
        ImportPlanItem(
            id="v1", plan_id=plan_id, raw_file_id="r1", source=source,
            relative_path="动画/test/视频.mkv", real_path="H:\\test.mkv",
            resource_type="video", action="generate_strm",
            work_id="work-test",
            work_title="测试作品", year=2024, group_type="season",
            season_number=1, episode_number=1, series_group="测试作品",
            card_type="main_series", media_type="tv", confidence="high",
        ),
    ]
    plan = ImportPlan(plan_id=plan_id, source=source, status=status, items=items)
    save_import_plan(plan)
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    save_library_index(LibraryIndex(works=[WorkIndex(
        work_id="work-test", source=source, title="测试作品",
    )]))
    return plan


class MockTMDBClient:
    """成功返回候选的 mock TMDB"""
    def search_tv(self, query, year=None):
        return [{"id": 12189, "name": "Hyouka", "original_name": "Hyouka",
                 "first_air_date": "2012-04-22", "overview": "test",
                 "popularity": 50, "vote_average": 8.0}]
    def search_movie(self, query, year=None):
        return []
    def get_tv_detail(self, tmdb_id):
        return {"id": tmdb_id, "name": "Hyouka", "original_name": "Hyouka",
                "first_air_date": "2012-04-22", "overview": "test"}
    def get_tv_images(self, tmdb_id):
        return {"posters": [], "backdrops": [], "logos": []}
    def get_movie_detail(self, tmdb_id):
        return {}
    def get_movie_images(self, tmdb_id):
        return {"posters": [], "backdrops": [], "logos": []}
    def download_image(self, path, dest):
        return False


def test_get_targets():
    """GET /api/scrape/targets 返回 targets 和 summary"""
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        _setup_plan()
        client = TestClient(app)
        resp = client.get("/api/scrape/targets?source=pan115&plan_id=p1")
        assert resp.status_code == 200
        data = resp.json()
        assert "targets" in data
        assert "summary" in data
        assert data["summary"]["total"] >= 1
        # 检查 target 包含路径字段
        t = data["targets"][0]
        assert "target_dir" in t
        assert "target_nfo_path" in t
        assert "target_poster_path" in t
    finally:
        _cleanup()


def test_get_targets_source_mismatch():
    """source 不匹配时返回 400"""
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        _setup_plan(source="pan115", plan_id="p2")
        client = TestClient(app)
        resp = client.get("/api/scrape/targets?source=local&plan_id=p2")
        assert resp.status_code == 400
        assert "不匹配" in resp.json()["detail"]
    finally:
        _cleanup()


def test_get_target_by_work_restores_latest_plan_target():
    """详情页手动刮削可按 work_id 找回后端 ScrapeTarget，不依赖前端已有缓存"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.scrape import _targets_cache

    _cleanup()
    try:
        _setup_plan()
        _targets_cache.clear()
        client = TestClient(app)
        resp = client.get("/api/scrape/target-by-work?work_id=work-test&source=pan115")

        assert resp.status_code == 200
        data = resp.json()
        assert data["target"]["work_id"] == "work-test"
        assert data["target"]["source"] == "pan115"
    finally:
        _cleanup()


def test_get_target_by_aggregated_movie_uses_mirror_directory():
    """电影卡片没有季信息时，应通过镜像目录找回原始刮削目标。"""
    from fastapi.testclient import TestClient
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    from app.main import app

    _cleanup()
    try:
        movie_dir = _DATA_DIR / "mirror" / "115" / "百米 (2025)"
        plan = ImportPlan(plan_id="p-movie", source="pan115", status="executed", items=[
            ImportPlanItem(
                id="movie-1", plan_id="p-movie", source="pan115",
                relative_path="动画电影/百米/百米.2025.mkv", real_path="H:\\百米.2025.mkv",
                resource_type="video", action="generate_strm", work_id="raw-movie",
                work_title="百米", series_group="百米", year=2025,
                group_type="movie", card_type="standalone", media_type="movie",
                show_type="anime_movie", target_dir=str(movie_dir),
                target_strm_path=str(movie_dir / "百米 (2025).strm"),
            )
        ])
        save_import_plan(plan)
        save_library_index(LibraryIndex(works=[WorkIndex(
            work_id="series-aggregate", title="百米。", source="pan115",
            media_type="movie", show_type="anime_movie", dir_path=str(movie_dir),
            episodes=[EpisodeIndex(group_type="movie", strm_path=str(movie_dir / "百米 (2025).strm"))],
        )]))

        response = TestClient(app).get(
            "/api/scrape/target-by-work?work_id=series-aggregate&source=pan115"
        )

        assert response.status_code == 200
        assert response.json()["target"]["work_id"] == "raw-movie"
        assert response.json()["target"]["group_type"] == "movie"
    finally:
        _cleanup()


def test_get_target_by_aggregated_library_work_uses_season_scrape_target_id():
    """聚合后的系列 work_id 应通过 LibraryIndex season.scrape_target_id 找回真实 target。"""
    from fastapi.testclient import TestClient
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.models import LibraryIndex, SeasonIndex, WorkIndex
    from app.library.store import invalidate_library_index_cache, save_library_index
    from app.main import app

    _cleanup()
    try:
        plan = ImportPlan(plan_id="p-agg", source="local", status="executed", items=[
            ImportPlanItem(
                id="s1e1", plan_id="p-agg", raw_file_id="r1", source="local",
                relative_path="Yuru Camp/S1/01.mkv", real_path="D:\\media\\01.mkv",
                resource_type="video", action="generate_strm",
                work_id="raw-yuru-s1", work_title="Yuru Camp", year=2018,
                group_type="season", season_number=1, episode_number=1,
                series_group="Yuru Camp", card_type="main_series", media_type="tv",
                show_type="anime_series", confidence="high",
            ),
            ImportPlanItem(
                id="s2e1", plan_id="p-agg", raw_file_id="r2", source="local",
                relative_path="Yuru Camp/S2/01.mkv", real_path="D:\\media\\s2e1.mkv",
                resource_type="video", action="generate_strm",
                work_id="raw-yuru-s2", work_title="Yuru Camp Season 2", year=2021,
                group_type="season", season_number=2, episode_number=1,
                series_group="Yuru Camp", card_type="main_series", media_type="tv",
                show_type="anime_series", confidence="high",
            ),
        ])
        save_import_plan(plan)
        targets, error = __import__("app.scrape.service", fromlist=["get_targets"]).get_targets("local")
        assert error is None
        target_by_season = {target.local_season_number: target for target in targets}
        save_library_index(LibraryIndex(works=[
            WorkIndex(
                work_id="series-yuru",
                source="local",
                title="摇曳露营△",
                show_type="anime_series",
                seasons=[
                    SeasonIndex(
                        season_id="series-yuru-s1",
                        work_id="series-yuru",
                        season_number=1,
                        group_type="season",
                        label="第1季",
                        episode_count=1,
                        scrape_target_id=target_by_season[1].scrape_target_id,
                    ),
                    SeasonIndex(
                        season_id="series-yuru-s2",
                        work_id="series-yuru",
                        season_number=2,
                        group_type="season",
                        label="第2季",
                        episode_count=1,
                        scrape_target_id=target_by_season[2].scrape_target_id,
                    ),
                ],
            )
        ]))
        invalidate_library_index_cache()

        client = TestClient(app)
        resp = client.get("/api/scrape/target-by-work?work_id=series-yuru&source=local&season_number=2&group_type=season")

        assert resp.status_code == 200
        data = resp.json()
        assert data["target"]["scrape_target_id"] == target_by_season[2].scrape_target_id
        assert data["target"]["work_id"] == "raw-yuru-s2"
        from app.api.scrape import _get_targets_for_library_work
        card_targets = _get_targets_for_library_work("series-yuru", preferred_source="local")
        assert [target.scrape_target_id for target in card_targets] == [
            target_by_season[1].scrape_target_id,
            target_by_season[2].scrape_target_id,
        ]
    finally:
        _cleanup()


def test_get_target_by_aggregated_library_work_without_scrape_map_uses_plan_work_id():
    """未刮削的聚合系列也应能通过 ImportPlan 找回真实 target。"""
    from fastapi.testclient import TestClient
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.index import _library_work_id
    from app.library.models import LibraryIndex, SeasonIndex, WorkIndex
    from app.library.store import invalidate_library_index_cache, save_library_index
    from app.main import app

    _cleanup()
    try:
        plan = ImportPlan(plan_id="p-unscraped", source="local", status="executed", items=[
            ImportPlanItem(
                id="s1e1", plan_id="p-unscraped", raw_file_id="r1", source="local",
                relative_path="Yuru Camp/S1/01.mkv", real_path="D:\\media\\01.mkv",
                resource_type="video", action="generate_strm",
                work_id="raw-yuru-s1", work_title="Yuru Camp", year=2018,
                group_type="season", season_number=1, episode_number=1,
                series_group="Yuru Camp", card_type="main_series", media_type="tv",
                show_type="anime_series", confidence="high",
            ),
            ImportPlanItem(
                id="s2e1", plan_id="p-unscraped", raw_file_id="r2", source="local",
                relative_path="Yuru Camp/S2/01.mkv", real_path="D:\\media\\s2e1.mkv",
                resource_type="video", action="generate_strm",
                work_id="raw-yuru-s2", work_title="Yuru Camp Season 2", year=2021,
                group_type="season", season_number=2, episode_number=1,
                series_group="Yuru Camp", card_type="main_series", media_type="tv",
                show_type="anime_series", confidence="high",
            ),
        ])
        save_import_plan(plan)
        aggregated_work_id = _library_work_id(plan.items[0])
        save_library_index(LibraryIndex(works=[
            WorkIndex(
                work_id=aggregated_work_id,
                source="local",
                title="摇曳露营△",
                show_type="anime_series",
                seasons=[
                    SeasonIndex(
                        season_id="series-yuru-s1",
                        work_id=aggregated_work_id,
                        season_number=1,
                        group_type="season",
                        label="第1季",
                        episode_count=1,
                    ),
                    SeasonIndex(
                        season_id="series-yuru-s2",
                        work_id=aggregated_work_id,
                        season_number=2,
                        group_type="season",
                        label="第2季",
                        episode_count=1,
                    ),
                ],
            )
        ]))
        invalidate_library_index_cache()

        client = TestClient(app)
        resp = client.get(
            "/api/scrape/target-by-work"
            f"?work_id={aggregated_work_id}&source=local&season_number=2&group_type=season"
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["target"]["work_id"] == "raw-yuru-s2"
        assert data["target"]["local_season_number"] == 2
    finally:
        _cleanup()


def test_get_candidates_with_mock():
    """GET /api/scrape/candidates 使用 mock TMDB 返回候选"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.scrape import _targets_cache
    from app.scrape import service as scrape_service
    from app.scrape.models import ScrapeCandidate

    _cleanup()
    try:
        _setup_plan()
        client = TestClient(app)
        client.get("/api/scrape/targets?source=pan115&plan_id=p1")
        target_id = list(_targets_cache.keys())[0]

        # 注入 mock search_candidates
        original_func = scrape_service.search_candidates
        def _mock_search(target, query=None, year=None, tmdb_client=None):
            return [ScrapeCandidate(
                candidate_id="c1", scrape_target_id=target.scrape_target_id,
                provider="anilist", tmdb_id=12189, tmdb_type="tv", title="Hyouka",
                original_title="Hyouka", year=2012, overview="test",
                poster_path="/test.jpg", popularity=50, vote_average=8.0,
                raw={
                    "anilist": {
                        "id": 12189,
                        "format": "TV",
                        "seasonYear": 2012,
                        "episodes": 22,
                        "averageScore": 82,
                        "bannerImage": "https://img.anilist.co/banner.jpg",
                    },
                    "canonical_assets": {"poster.jpg": "https://img.anilist.co/poster.jpg"},
                },
            )]
        scrape_service.search_candidates = _mock_search
        try:
            resp = client.get(f"/api/scrape/candidates?target_id={target_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert "target" in data
            assert "candidates" in data
            assert len(data["candidates"]) == 1
            assert data["candidates"][0]["tmdb_id"] == 12189
            assert data["candidates"][0]["provider"] == "anilist"
            assert data["candidates"][0]["source_meta"]["provider"] == "anilist"
            assert data["candidates"][0]["source_meta"]["episodes"] == 22
            assert data["candidates"][0]["source_meta"]["canonical_assets"]["poster.jpg"].startswith("https://")
        finally:
            scrape_service.search_candidates = original_func
    finally:
        _cleanup()


def test_select_returns_task():
    """POST /api/scrape/select 返回 task_id"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.scrape import _targets_cache

    _cleanup()
    try:
        _setup_plan()
        client = TestClient(app)
        client.get("/api/scrape/targets?source=pan115&plan_id=p1")
        target_id = list(_targets_cache.keys())[0]

        resp = client.post("/api/scrape/select", json={
            "target_id": target_id, "tmdb_id": 123, "tmdb_type": "tv",
            "library_work_id": "work-test",
        })
        assert resp.status_code == 200
        assert "task_id" in resp.json()
    finally:
        _cleanup()


def test_select_task_completes():
    """select 任务完成后可查询结果"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.scrape import _targets_cache
    from app.scrape import service as scrape_service

    _cleanup()
    try:
        _setup_plan()
        client = TestClient(app)
        client.get("/api/scrape/targets?source=pan115&plan_id=p1")
        target_id = list(_targets_cache.keys())[0]

        # 注入 mock execute_scrape
        original_func = scrape_service.execute_scrape
        scrape_service.execute_scrape = lambda **kwargs: {
            "scrape_target_id": kwargs.get("target", type("", (), {"scrape_target_id": ""})()).scrape_target_id,
            "tmdb_id": kwargs.get("tmdb_id"),
            "nfo_path": "/test/tvshow.nfo",
            "poster_path": "", "fanart_path": "", "clearlogo_path": "",
            "scrape_map_path": "/test/scrape_map.json", "warnings": [],
        }
        try:
            resp = client.post("/api/scrape/select", json={
                "target_id": target_id, "tmdb_id": 123, "tmdb_type": "tv",
                "library_work_id": "work-test",
            })
            task_id = resp.json()["task_id"]
            time.sleep(2)

            resp = client.get(f"/api/scrape/tasks/{task_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] in ("succeeded", "failed")
        finally:
            scrape_service.execute_scrape = original_func
    finally:
        _cleanup()


def test_manual_select_requires_current_library_card_anchor():
    """人工选择必须明确来自哪张详情卡，禁止退化成无锚点的新增式刷新。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.scrape import _targets_cache

    _cleanup()
    try:
        _setup_plan()
        client = TestClient(app)
        client.get("/api/scrape/targets?source=pan115&plan_id=p1")
        target_id = list(_targets_cache.keys())[0]

        response = client.post("/api/scrape/select", json={
            "target_id": target_id,
            "tmdb_id": 123,
            "tmdb_type": "tv",
            "selected_by": "manual_replace",
        })

        assert response.status_code == 422
        assert "library_work_id" in response.text
    finally:
        _cleanup()


def test_manual_work_select_submits_all_targets_from_current_card(monkeypatch):
    """整部作品模式必须只编排当前卡片的全部季度，不能退化为单季任务。"""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.api.scrape as scrape_api
    from app.main import app
    from app.scrape.models import ScrapeTarget

    selected = ScrapeTarget(
        scrape_target_id="railgun-s1",
        source="local",
        import_plan_id="plan-railgun",
        work_id="raw-railgun-s1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="某科学的超电磁炮",
        local_title="某科学的超电磁炮",
        local_season_number=1,
        scrape_title="某科学的超电磁炮",
        scrape_type="tv",
    )
    sibling = ScrapeTarget(
        scrape_target_id="railgun-s2",
        source="local",
        import_plan_id="plan-railgun",
        work_id="raw-railgun-s2",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="某科学的超电磁炮",
        local_title="某科学的超电磁炮S",
        local_season_number=2,
        scrape_title="某科学的超电磁炮S",
        scrape_type="tv",
    )
    submitted = {}

    class FakeManager:
        def submit_queued(self, **kwargs):
            submitted.update(kwargs)
            return SimpleNamespace(task_id="task-work-scrape", status="pending")

    monkeypatch.setattr(scrape_api, "_get_target_or_restore", lambda _target_id: selected)
    monkeypatch.setattr(scrape_api, "_target_belongs_to_library_work", lambda *_args: True)
    monkeypatch.setattr(
        scrape_api,
        "_get_targets_for_library_work",
        lambda _work_id, preferred_source=None: [selected, sibling],
    )
    monkeypatch.setattr(scrape_api, "get_task_manager", lambda: FakeManager())

    response = TestClient(app).post("/api/scrape/select", json={
        "target_id": selected.scrape_target_id,
        "tmdb_id": 30977,
        "tmdb_type": "tv",
        "tmdb_season_number": 1,
        "selected_by": "manual_replace",
        "library_work_id": "library-railgun",
        "scope": "work",
    })

    assert response.status_code == 200
    assert submitted["task_type"] == "scrape_manual_work"
    assert [target.scrape_target_id for target in submitted["work_targets"]] == [
        "railgun-s1",
        "railgun-s2",
    ]
    assert submitted["library_work_id"] == "library-railgun"


def test_manual_season_select_keeps_existing_single_target_task(monkeypatch):
    """单季度模式继续走原有单目标任务，作为局部修复入口。"""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.api.scrape as scrape_api
    from app.main import app
    from app.scrape.models import ScrapeTarget

    target = ScrapeTarget(
        scrape_target_id="railgun-s2",
        source="local",
        import_plan_id="plan-railgun",
        work_id="raw-railgun-s2",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="某科学的超电磁炮",
        local_title="某科学的超电磁炮S",
        local_season_number=2,
        scrape_title="某科学的超电磁炮S",
        scrape_type="tv",
    )
    submitted = {}

    class FakeManager:
        def submit_queued(self, **kwargs):
            submitted.update(kwargs)
            return SimpleNamespace(task_id="task-season-scrape", status="pending")

    monkeypatch.setattr(scrape_api, "_get_target_or_restore", lambda _target_id: target)
    monkeypatch.setattr(scrape_api, "_target_belongs_to_library_work", lambda *_args: True)
    monkeypatch.setattr(scrape_api, "get_task_manager", lambda: FakeManager())

    response = TestClient(app).post("/api/scrape/select", json={
        "target_id": target.scrape_target_id,
        "tmdb_id": 30977,
        "tmdb_type": "tv",
        "selected_by": "manual_replace",
        "library_work_id": "library-railgun",
        "scope": "season",
    })

    assert response.status_code == 200
    assert submitted["task_type"] == "scrape_select"
    assert submitted["target"].scrape_target_id == "railgun-s2"


def test_manual_work_worker_processes_siblings_and_refreshes_card_once(monkeypatch):
    """整卡任务先落实人工候选，再处理其余季度，最后按原 work_id 原位刷新。"""
    import app.api.scrape as scrape_api
    import app.library.service as library_service
    import app.scrape.auto as scrape_auto
    from app.scrape.models import ScrapeTarget

    selected = ScrapeTarget(
        scrape_target_id="railgun-s1",
        source="local",
        import_plan_id="plan-railgun",
        work_id="raw-railgun-s1",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="某科学的超电磁炮",
        local_season_number=1,
        scrape_title="某科学的超电磁炮",
        scrape_type="tv",
    )
    sibling = ScrapeTarget(
        scrape_target_id="railgun-s2",
        source="local",
        import_plan_id="plan-railgun",
        work_id="raw-railgun-s2",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group="某科学的超电磁炮",
        local_season_number=2,
        scrape_title="某科学的超电磁炮S",
        scrape_type="tv",
    )
    calls = {"execute": [], "auto": [], "refresh": []}

    monkeypatch.setattr(
        scrape_api.scrape_service,
        "execute_scrape",
        lambda **kwargs: calls["execute"].append(kwargs) or {"scrape_target_id": "railgun-s1"},
    )
    monkeypatch.setattr(
        scrape_auto,
        "run_auto_scrape",
        lambda **kwargs: calls["auto"].append(kwargs) or {
            "auto_scraped": 1,
            "skipped_existing": 0,
            "review_queued": 0,
            "failed": 0,
            "results": [{"target_id": "railgun-s2", "status": "auto_scraped"}],
        },
    )
    monkeypatch.setattr(
        library_service,
        "refresh_library_for_scrape_targets",
        lambda targets, library_work_id="": calls["refresh"].append(
            ([target.scrape_target_id for target in targets], library_work_id)
        ) or {"mode": "partial", "work_count": 1},
    )
    monkeypatch.setattr(scrape_api, "resolve_review_item", lambda *_args: True)
    monkeypatch.setattr(scrape_api, "_mark_plan_ready_when_review_complete", lambda *_args: None)

    result = scrape_api._run_selected_work_scrape(
        target=selected,
        work_targets=[selected, sibling],
        tmdb_id=30977,
        tmdb_type="tv",
        tmdb_season_number=1,
        library_work_id="library-railgun",
    )

    assert calls["execute"][0]["target"].scrape_target_id == "railgun-s1"
    assert calls["auto"][0]["target_ids"] == {"railgun-s2"}
    assert calls["auto"][0]["library_work_id"] == "library-railgun"
    assert calls["refresh"] == [(["railgun-s1", "railgun-s2"], "library-railgun")]
    assert result["manual_scraped"] == 1
    assert result["auto_scraped"] == 1
    assert result["failed"] == 0


def test_auto_rejects_removed_dry_run_parameter():
    """POST /api/scrape/auto 不再接受已经删除的预检参数。"""
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    client = TestClient(app)
    try:
        resp = client.post("/api/scrape/auto", json={"source": "pan115", "dry_run": True})
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["body", "dry_run"]
    finally:
        _cleanup()


def test_tasks_not_found():
    """GET /api/scrape/tasks/{task_id} 不存在返回 404"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/api/scrape/tasks/nonexistent")
    assert resp.status_code == 404


def test_cancel_task_endpoint():
    """POST /api/scrape/tasks/{task_id}/cancel 可停止任务"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.tasks.registry import get_task_manager

    _cleanup()
    try:
        client = TestClient(app)
        manager = get_task_manager()
        started = threading.Event()
        release = threading.Event()

        def wait_for_cancel(should_cancel=None):
            started.set()
            while not should_cancel():
                time.sleep(0.01)
            release.wait(timeout=1)
            return {}

        record = manager.submit(
            "scrape_auto",
            "pan115",
            wait_for_cancel,
            message="test",
        )
        assert started.wait(timeout=1)
        resp = client.post(f"/api/scrape/tasks/{record.task_id}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["message"] == "正在停止"
        release.set()

        deadline = time.time() + 2
        while time.time() < deadline:
            current = client.get(f"/api/scrape/tasks/{record.task_id}").json()
            if current["status"] == "cancelled":
                break
            time.sleep(0.01)
        assert current["status"] == "cancelled"
        assert current["message"] == "已停止"
        assert current["error"] == ""
    finally:
        _cleanup()


def test_failures_returns_list():
    """GET /api/scrape/failures 返回列表"""
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        client = TestClient(app)
        resp = client.get("/api/scrape/failures")
        assert resp.status_code == 200
        assert "failures" in resp.json()
    finally:
        _cleanup()


def test_review_queue_filters_source_and_prunes_stale_targets():
    """人工确认队列应只显示当前来源仍存在的 scrape target。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.scrape.review_queue import ReviewQueue, ReviewQueueItem, load_review_queue, save_review_queue
    from app.scrape.service import get_targets

    _cleanup()
    try:
        _setup_plan(source="baidu", plan_id="p-baidu")
        targets, error = get_targets("baidu", "p-baidu")
        assert error is None
        current_target_id = targets[0].scrape_target_id
        save_review_queue(ReviewQueue(items=[
            ReviewQueueItem(scrape_target_id=current_target_id, source="baidu", import_plan_id="p-baidu", local_title="当前百度", status="pending"),
            ReviewQueueItem(scrape_target_id="stale-baidu", source="baidu", local_title="旧百度", status="pending"),
            ReviewQueueItem(scrape_target_id="stale-pan115", source="pan115", local_title="旧115", status="pending"),
        ]))

        client = TestClient(app)
        resp = client.get("/api/scrape/review-queue?source=baidu")

        assert resp.status_code == 200
        data = resp.json()
        assert [item["scrape_target_id"] for item in data["items"]] == [current_target_id]
        assert data["items"][0]["import_plan_id"] == "p-baidu"
        assert data["total"] == 1
        remaining = {(item.scrape_target_id, item.source, item.status) for item in load_review_queue().items}
        assert (current_target_id, "baidu", "pending") in remaining
        assert ("stale-baidu", "baidu", "stale") in remaining
        assert ("stale-pan115", "pan115", "pending") in remaining
    finally:
        _cleanup()


def test_review_queue_clears_source_without_import_plan():
    """某来源 ImportPlan 已清空时，该来源人工确认不应继续显示。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.scrape.review_queue import ReviewQueue, ReviewQueueItem, load_review_queue, save_review_queue

    _cleanup()
    try:
        save_review_queue(ReviewQueue(items=[
            ReviewQueueItem(scrape_target_id="old-pan115", source="pan115", local_title="旧115", status="pending"),
            ReviewQueueItem(scrape_target_id="old-baidu", source="baidu", local_title="旧百度", status="pending"),
        ]))

        client = TestClient(app)
        resp = client.get("/api/scrape/review-queue?source=pan115")

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        remaining = {(item.scrape_target_id, item.source, item.status) for item in load_review_queue().items}
        assert ("old-pan115", "pan115", "stale") in remaining
        assert ("old-baidu", "baidu", "pending") in remaining
    finally:
        _cleanup()


def test_failures_after_real_error():
    """刮削失败后 task 记录错误信息"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.scrape import _targets_cache
    from app.scrape import service as scrape_service

    _cleanup()
    try:
        _setup_plan()
        client = TestClient(app)
        client.get("/api/scrape/targets?source=pan115&plan_id=p1")
        target_id = list(_targets_cache.keys())[0]

        # mock execute_scrape 抛异常
        def _fail(**kwargs):
            raise RuntimeError("模拟刮削失败")
        original_func = scrape_service.execute_scrape
        scrape_service.execute_scrape = _fail
        try:
            resp = client.post("/api/scrape/select", json={
                "target_id": target_id, "tmdb_id": 999, "tmdb_type": "tv",
            })
            task_id = resp.json()["task_id"]
            time.sleep(3)

            # 检查 task 状态和错误信息
            resp = client.get(f"/api/scrape/tasks/{task_id}")
            data = resp.json()
            assert data["status"] == "failed"
            assert "模拟" in data.get("error", "")
        finally:
            scrape_service.execute_scrape = original_func
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_get_targets, test_get_targets_source_mismatch,
        test_get_candidates_with_mock, test_select_returns_task,
        test_select_task_completes,
        test_tasks_not_found, test_failures_returns_list,
        test_failures_after_real_error,
    ]
    passed = failed = 0
    for t in tests:
        try:
            _cleanup()
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        finally:
            _cleanup()
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
