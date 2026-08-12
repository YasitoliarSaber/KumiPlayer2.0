# -*- coding: utf-8 -*-
"""M09 媒体库 API 测试"""

import shutil
import sys
import time
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_PROJECT_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DATA_DIR = Path(tempfile.gettempdir()) / "kumiplayer_tests" / "test_library_api_data"
os.environ["KUMIPLAYER_DATA_DIR"] = str(_DATA_DIR)


def _cleanup():
    from app.tasks.registry import reset_task_manager
    reset_task_manager()
    time.sleep(0.5)
    if _DATA_DIR.exists():
        if _DATA_DIR.resolve() == _PROJECT_DATA_DIR.resolve():
            raise RuntimeError("Refusing to delete project data directory during tests")
        try:
            shutil.rmtree(_DATA_DIR)
        except OSError:
            shutil.rmtree(_DATA_DIR, ignore_errors=True)


def test_library_empty():
    """GET /api/library 无 cache 返回 needs_rescan=true"""
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        client = TestClient(app)
        resp = client.get("/api/library")
        assert resp.status_code == 200
        data = resp.json()
        assert data["needs_rescan"] is True
        assert data["works"] == []
    finally:
        _cleanup()


def test_rescan_returns_task():
    """POST /api/library/rescan 返回 task_id"""
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        client = TestClient(app)
        resp = client.post("/api/library/rescan", json={})
        assert resp.status_code == 200
        assert "task_id" in resp.json()
    finally:
        _cleanup()


def test_task_not_found():
    """GET /api/library/tasks/{task_id} 不存在返回 404"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/api/library/tasks/nonexistent")
    assert resp.status_code == 404


def test_work_not_found():
    """GET /api/library/works/{work_id} 不存在返回 404"""
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        client = TestClient(app)
        resp = client.get("/api/library/works/nonexistent")
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_library_serializers_include_all_sources():
    """列表摘要和详情都必须返回混合卡的全部实际来源。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.library.service import _work_summary_to_dict, _work_to_dict

    work = WorkIndex(
        work_id="seasonal-mixed", source="pan115",
        sources=["pan115", "local"], import_scope="seasonal",
        episodes=[EpisodeIndex(episode_id="local-episode", source="local")],
        source_locations={
            "pan115": {"episode_id": "pan-episode", "strm_path": "pan.strm"},
            "local": {"episode_id": "local-episode", "strm_path": "local.strm"},
        },
    )

    assert _work_summary_to_dict(work)["sources"] == ["pan115", "local"]
    assert _work_to_dict(work)["sources"] == ["pan115", "local"]
    assert _work_to_dict(work)["episodes"][0]["source"] == "local"
    assert _work_to_dict(work)["source_episode_ids"] == {
        "pan115": "pan-episode",
        "local": "local-episode",
    }


def test_library_compact_uses_season_artwork_when_work_artwork_is_empty():
    """列表摘要复用季度索引图片，避免详情返回后才短暂显示海报。"""
    from app.library.models import SeasonIndex, WorkIndex
    from app.library.service import _work_summary_to_dict

    work = WorkIndex(
        work_id="baidu-series",
        title="测试番剧",
        source="baidu",
        poster_path="",
        fanart_path="",
        clearlogo_path="",
        seasons=[
            SeasonIndex(
                season_id="season-1",
                season_number=1,
                poster_path="Season 1/poster.jpg",
                fanart_path="Season 1/fanart.jpg",
                clearlogo_path="Season 1/clearlogo.png",
            ),
        ],
    )

    compact = _work_summary_to_dict(work)

    assert compact["poster_path"] == "Season 1/poster.jpg"
    assert compact["fanart_path"] == "Season 1/fanart.jpg"
    assert compact["clearlogo_path"] == "Season 1/clearlogo.png"


def test_library_compact_finds_work_root_artwork_for_legacy_season_index(tmp_path):
    """旧索引指向 Season 目录时，列表应复用作品根目录的现有图片。"""
    from app.library.models import SeasonIndex, WorkIndex
    from app.library.service import _work_summary_to_dict

    work_root = tmp_path / "百度番剧"
    season_dir = work_root / "Season 2"
    season_dir.mkdir(parents=True)
    poster = work_root / "poster.jpg"
    fanart = work_root / "fanart.jpg"
    clearlogo = work_root / "clearlogo.png"
    poster.write_bytes(b"poster")
    fanart.write_bytes(b"fanart")
    clearlogo.write_bytes(b"logo")

    work = WorkIndex(
        work_id="legacy-baidu-series",
        title="百度番剧",
        source="baidu",
        dir_path=str(season_dir),
        seasons=[SeasonIndex(season_id="season-2", season_number=2)],
    )

    compact = _work_summary_to_dict(work)

    assert compact["poster_path"] == str(poster)
    assert compact["fanart_path"] == str(fanart)
    assert compact["clearlogo_path"] == str(clearlogo)


def test_library_compact_omits_episode_details_but_keeps_counts():
    """GET /api/library?compact=true 只返回列表必要字段，避免启动展开所有剧集。"""
    from fastapi.testclient import TestClient
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.store import save_library_index
    from app.main import app

    _cleanup()
    try:
        save_library_index(LibraryIndex(version=2, works=[
            WorkIndex(
                work_id="work-1",
                title="测试作品",
                show_type="anime_series",
                source="local",
                seasons=[SeasonIndex(season_id="s1", season_number=1, group_type="season", label="第1季", episode_count=2)],
                episodes=[
                    EpisodeIndex(episode_id="e1", season_number=1, episode_number=1, title="第一集", group_type="season"),
                    EpisodeIndex(episode_id="e2", season_number=1, episode_number=2, title="第二集", group_type="season"),
                ],
            )
        ]))
        client = TestClient(app)
        compact = client.get("/api/library?compact=true").json()["works"][0]
        detail = client.get("/api/library/works/work-1").json()

        assert compact["episodes"] == []
        assert compact["episode_count"] == 2
        assert compact["seasons"][0]["episode_count"] == 2
        assert len(detail["episodes"]) == 2
    finally:
        _cleanup()


def test_library_source_filter_returns_only_requested_source():
    """媒体库 API 的来源筛选必须准确区分本地与网盘作品。"""
    from fastapi.testclient import TestClient
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    from app.main import app

    _cleanup()
    try:
        save_library_index(LibraryIndex(version=2, works=[
            WorkIndex(work_id="local-work", title="本地作品", source="local"),
            WorkIndex(work_id="pan-work", title="网盘作品", source="pan115"),
        ]))
        client = TestClient(app)

        response = client.get("/api/library?source=local&compact=true")

        assert response.status_code == 200
        assert [item["work_id"] for item in response.json()["works"]] == ["local-work"]
    finally:
        _cleanup()


def test_library_hides_pending_metadata_works_from_lists_and_summary():
    """正常刮削中的半成品保留在底层索引，但不得进入正式媒体列表和统计。"""
    from fastapi.testclient import TestClient
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import load_library_index, save_library_index
    from app.main import app

    _cleanup()
    try:
        save_library_index(LibraryIndex(version=2, works=[
            WorkIndex(
                work_id="ready-work", title="已完成作品", source="local",
                metadata_state="ready",
                episodes=[EpisodeIndex(episode_id="ready-e1")],
            ),
            WorkIndex(
                work_id="pending-work", title="刮削中作品", source="local",
                metadata_state="waiting_metadata",
                episodes=[
                    EpisodeIndex(episode_id="pending-e1"),
                    EpisodeIndex(episode_id="pending-e2"),
                ],
            ),
        ]))
        with TestClient(app) as client:
            for compact in ("true", "false"):
                payload = client.get(f"/api/library?compact={compact}").json()
                assert [work["work_id"] for work in payload["works"]] == ["ready-work"]
                assert payload["summary"]["work_count"] == 1
                assert payload["summary"]["episode_count"] == 1

        assert {work.work_id for work in load_library_index().works} == {
            "ready-work", "pending-work",
        }
    finally:
        _cleanup()


def test_legacy_index_keeps_ready_movies_visible_despite_stale_episode_pending():
    """v1 缓存的作品级 ready 应覆盖电影剧集上遗留的 pending 标记。"""
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import _visible_library_works

    ready_movie = WorkIndex(
        work_id="ready-movie",
        title="已完成动画电影",
        source="pan115",
        show_type="anime_movie",
        media_type="movie",
        metadata_state="ready",
        poster_path="https://example.com/poster.jpg",
        fanart_path="https://example.com/fanart.jpg",
        episodes=[EpisodeIndex(
            episode_id="movie-file",
            group_type="movie",
            metadata_pending=True,
        )],
    )
    pending_movie = WorkIndex(
        work_id="pending-movie",
        title="仍在处理的动画电影",
        source="pan115",
        show_type="anime_movie",
        media_type="movie",
        metadata_state="waiting_metadata",
        poster_path="https://example.com/poster.jpg",
        fanart_path="https://example.com/fanart.jpg",
        episodes=[EpisodeIndex(episode_id="pending-file", group_type="movie")],
    )
    index = LibraryIndex(version=1, works=[ready_movie, pending_movie])

    visible = _visible_library_works(index, index.works)

    assert [work.work_id for work in visible] == ["ready-movie"]


def test_new_library_index_uses_current_schema_version():
    """局部发布从空库开始时不得再次生成 v1 索引。"""
    from app.library.models import LibraryIndex

    assert LibraryIndex().version == 2


def test_library_keeps_attention_state_cards_visible():
    """已进入人工确认或来源异常流程的作品必须保留无图处理入口。"""
    from fastapi.testclient import TestClient
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    from app.main import app

    _cleanup()
    try:
        save_library_index(LibraryIndex(version=2, works=[
            WorkIndex(
                work_id="pending", title="普通刮削中", source="pan115",
                metadata_state="waiting_metadata",
            ),
            WorkIndex(
                work_id="review", title="待人工确认", source="pan115",
                metadata_state="waiting_review",
            ),
            WorkIndex(
                work_id="offline", title="来源不可用", source="pan115",
                metadata_state="source_unavailable",
            ),
        ]))

        with TestClient(app) as client:
            payload = client.get("/api/library?compact=true").json()

        assert [work["work_id"] for work in payload["works"]] == ["review", "offline"]
    finally:
        _cleanup()


def test_pending_review_queue_makes_only_its_incomplete_work_visible(tmp_path):
    """普通导入的 review queue 应复用目标身份显示对应空卡，而非放出全批半成品。"""
    from fastapi.testclient import TestClient
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.index import _library_work_id
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    from app.main import app
    from app.scrape.review_queue import ReviewQueue, ReviewQueueItem, save_review_queue
    from app.scrape.target_builder import build_scrape_targets

    review_item = ImportPlanItem(
        id="review-item", plan_id="review-plan", source="baidu",
        resource_type="video", action="generate_strm", work_id="review-raw",
        work_title="待确认作品", media_type="tv", card_type="standalone",
        group_type="season", season_number=1, episode_number=1,
        target_dir=str(tmp_path / "review"),
    )
    pending_item = ImportPlanItem(
        id="pending-item", plan_id="review-plan", source="baidu",
        resource_type="video", action="generate_strm", work_id="pending-raw",
        work_title="普通刮削中", media_type="tv", card_type="standalone",
        group_type="season", season_number=1, episode_number=1,
        target_dir=str(tmp_path / "pending"),
    )
    plan = ImportPlan(
        plan_id="review-plan", source="baidu", status="executed",
        items=[review_item, pending_item],
    )
    save_import_plan(plan)
    target = next(
        target for target in build_scrape_targets(plan)
        if target.work_id == "review-raw"
    )
    save_review_queue(ReviewQueue(items=[ReviewQueueItem(
        scrape_target_id=target.scrape_target_id,
        source="baidu",
        import_plan_id=plan.plan_id,
        status="pending",
    )]))
    save_library_index(LibraryIndex(version=2, works=[
        WorkIndex(
            work_id=_library_work_id(review_item), title="待确认作品",
            source="baidu", metadata_state="waiting_metadata",
        ),
        WorkIndex(
            work_id=_library_work_id(pending_item), title="普通刮削中",
            source="baidu", metadata_state="waiting_metadata",
        ),
    ]))

    with TestClient(app) as client:
        payload = client.get("/api/library?compact=true").json()

    assert [work["work_id"] for work in payload["works"]] == [
        _library_work_id(review_item),
    ]


def test_failed_scrape_case_makes_only_its_incomplete_work_visible(tmp_path):
    """真实刮削失败记录应保留对应空卡，不能放出同批仍在正常处理的作品。"""
    from fastapi.testclient import TestClient
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.index import _library_work_id
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    from app.main import app
    from app.scrape.store import build_failed_case, save_failed_case
    from app.scrape.target_builder import build_scrape_targets

    failed_item = ImportPlanItem(
        id="failed-item", plan_id="failed-plan", source="local",
        resource_type="video", action="generate_strm", work_id="failed-raw",
        work_title="失败作品", media_type="tv", card_type="standalone",
        group_type="season", season_number=1, episode_number=1,
        target_dir=str(tmp_path / "failed"),
    )
    pending_item = ImportPlanItem(
        id="normal-item", plan_id="failed-plan", source="local",
        resource_type="video", action="generate_strm", work_id="normal-raw",
        work_title="正常处理中", media_type="tv", card_type="standalone",
        group_type="season", season_number=1, episode_number=1,
        target_dir=str(tmp_path / "normal"),
    )
    plan = ImportPlan(
        plan_id="failed-plan", source="local", status="executed",
        items=[failed_item, pending_item],
    )
    save_import_plan(plan)
    target = next(
        target for target in build_scrape_targets(plan)
        if target.work_id == "failed-raw"
    )
    save_failed_case(build_failed_case(target=target, error="provider unavailable", stage="search"))
    save_library_index(LibraryIndex(version=2, works=[
        WorkIndex(
            work_id=_library_work_id(failed_item), title="失败作品",
            source="local", metadata_state="waiting_metadata",
        ),
        WorkIndex(
            work_id=_library_work_id(pending_item), title="正常处理中",
            source="local", metadata_state="waiting_metadata",
        ),
    ]))

    with TestClient(app) as client:
        payload = client.get("/api/library?compact=true").json()

    assert [work["work_id"] for work in payload["works"]] == [
        _library_work_id(failed_item),
    ]


def test_cached_title_is_not_refreshed_from_nfo_during_read(tmp_path):
    """读取 API 只消费索引，NFO 标题应在扫描或刮削阶段写回。"""
    from fastapi.testclient import TestClient
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    from app.main import app

    work_dir = tmp_path / "Heya Camp"
    work_dir.mkdir()
    (work_dir / "tvshow.nfo").write_text(
        "<tvshow><title>房间露营△</title><originaltitle>へやキャン△</originaltitle></tvshow>",
        encoding="utf-8",
    )
    save_library_index(LibraryIndex(version=2, works=[
        WorkIndex(work_id="heya", title="Heya Camp", dir_path=str(work_dir), source="local"),
    ]))

    with TestClient(app) as client:
        compact = client.get("/api/library?compact=true").json()["works"][0]
        detail = client.get("/api/library/works/heya").json()

    assert compact["title"] == "Heya Camp"
    assert detail["title"] == "Heya Camp"


def test_episode_title_falls_back_to_standard_filename_when_nfo_missing():
    """没有分集 NFO 时，标准文件名里的集标题不能被 S01E01 覆盖。"""
    from app.api.library import _enrich_one_episode_title

    ep = {
        "season_number": 1,
        "episode_number": 1,
        "group_type": "season",
        "title": "和自己生活在截然不同世界的人",
        "strm_path": r"D:\mirror\更衣人偶坠入爱河\Season 1\更衣人偶坠入爱河 - S01E01 - 和自己生活在截然不同世界的人.strm",
    }

    _enrich_one_episode_title(ep, "更衣人偶坠入爱河")

    assert ep["episode_code"] == "S01E01"
    assert ep["episode_title"] == "和自己生活在截然不同世界的人"
    assert ep["display_title"] == "和自己生活在截然不同世界的人"
    assert ep["title"] == "和自己生活在截然不同世界的人"
    assert ep["full_title"] == "更衣人偶坠入爱河 - S01E01 - 和自己生活在截然不同世界的人"


def test_episode_title_extracts_from_standard_filename_when_cached_title_generic():
    """缓存标题只有 S01E01 时，也要从文件名右侧提取实际集标题。"""
    from app.api.library import _enrich_one_episode_title

    ep = {
        "season_number": 1,
        "episode_number": 2,
        "group_type": "season",
        "title": "S01E02",
        "strm_path": r"D:\mirror\更衣人偶坠入爱河\Season 1\更衣人偶坠入爱河 - S01E02 - 马上就来做吧？.strm",
    }

    _enrich_one_episode_title(ep, "更衣人偶坠入爱河")

    assert ep["episode_title"] == "马上就来做吧？"
    assert ep["display_title"] == "马上就来做吧？"


def test_episode_thumbnail_falls_back_only_when_reference_is_empty(tmp_path):
    """读取 API 不探测本地文件；空引用回退，已有引用交由资源接口处理。"""
    from app.api.library import _enrich_episode_artwork

    fanart = tmp_path / "fanart.jpg"
    fanart.write_bytes(b"fanart")
    missing_thumb = tmp_path / "missing-thumb.jpg"
    work = {
        "work_id": "work-1",
        "fanart_path": str(fanart),
        "poster_path": str(tmp_path / "poster.jpg"),
        "episodes": [
            {"episode_id": "e1", "thumb_path": ""},
            {"episode_id": "e2", "thumb_path": str(missing_thumb)},
            {"episode_id": "e3", "thumb_path": "https://image.tmdb.org/t/p/w500/still.jpg"},
        ],
    }

    _enrich_episode_artwork(work, str(fanart))

    assert work["episodes"][0]["thumb_path"] == str(fanart)
    assert work["episodes"][1]["thumb_path"] == str(missing_thumb)
    assert work["episodes"][2]["thumb_path"].endswith("still.jpg")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [test_library_empty, test_rescan_returns_task, test_task_not_found, test_work_not_found,
             test_library_compact_omits_episode_details_but_keeps_counts,
             test_episode_title_falls_back_to_standard_filename_when_nfo_missing,
             test_episode_title_extracts_from_standard_filename_when_cached_title_generic]
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
