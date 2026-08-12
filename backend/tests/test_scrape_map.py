# -*- coding: utf-8 -*-
"""M08 ScrapeMap 测试"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_DATA_DIR = Path(__file__).parent.parent.parent / "data"


def _cleanup():
    if _DATA_DIR.exists():
        try:
            shutil.rmtree(_DATA_DIR)
        except OSError:
            shutil.rmtree(_DATA_DIR, ignore_errors=True)


def test_upsert_new_item():
    """新增 ScrapeMapItem"""
    from app.scrape.store import upsert_scrape_map_item, load_scrape_map
    from app.scrape.models import ScrapeMapItem

    _cleanup()
    try:
        item = ScrapeMapItem(
            scrape_target_id="t1", work_id="w1", source="pan115",
            local_title="CLANNAD", local_season_number=1,
            tmdb_id=12189, tmdb_type="tv", tmdb_season_number=1,
            selected_by="manual", scraped_at="2026-06-13T00:00:00",
        )
        upsert_scrape_map_item(item)
        sm = load_scrape_map()
        assert len(sm.items) == 1
        assert sm.items[0].scrape_target_id == "t1"
        assert sm.items[0].tmdb_id == 12189
        assert sm.items[0].local_season_number == 1
        assert sm.items[0].tmdb_season_number == 1
    finally:
        _cleanup()


def test_upsert_update_item():
    """同 scrape_target_id 更新 item"""
    from app.scrape.store import upsert_scrape_map_item, load_scrape_map
    from app.scrape.models import ScrapeMapItem

    _cleanup()
    try:
        item1 = ScrapeMapItem(scrape_target_id="t1", tmdb_id=100)
        upsert_scrape_map_item(item1)

        item2 = ScrapeMapItem(scrape_target_id="t1", tmdb_id=200)
        upsert_scrape_map_item(item2)

        sm = load_scrape_map()
        assert len(sm.items) == 1
        assert sm.items[0].tmdb_id == 200
    finally:
        _cleanup()


def test_preserve_season_numbers():
    """保留 local_season_number 和 tmdb_season_number"""
    from app.scrape.store import upsert_scrape_map_item, load_scrape_map
    from app.scrape.models import ScrapeMapItem

    _cleanup()
    try:
        item = ScrapeMapItem(
            scrape_target_id="t2",
            local_season_number=2,
            tmdb_season_number=1,
            tmdb_id=5000,
        )
        upsert_scrape_map_item(item)
        sm = load_scrape_map()
        assert sm.items[0].local_season_number == 2
        assert sm.items[0].tmdb_season_number == 1
    finally:
        _cleanup()


def test_upsert_replaces_same_source_nfo_path_when_target_id_drifted(tmp_path, monkeypatch):
    from app.scrape import store
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    state = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id="old-target",
        source="baidu",
        nfo_path=str(tmp_path / "Season 1" / "tvshow.nfo"),
        tmdb_id=1,
    )])
    monkeypatch.setattr(store, "load_scrape_map", lambda: state)
    monkeypatch.setattr(store, "save_scrape_map", lambda value: setattr(state, "items", value.items))

    store.upsert_scrape_map_item(ScrapeMapItem(
        scrape_target_id="new-target",
        source="baidu",
        nfo_path=str(tmp_path / "Season 1" / "tvshow.nfo"),
        tmdb_id=2,
    ))

    assert len(state.items) == 1
    assert state.items[0].scrape_target_id == "new-target"
    assert state.items[0].tmdb_id == 2


def test_failed_case_keeps_debug_context():
    """失败记录必须保留本地目标、候选和异常栈，方便排查刮削错误。"""
    from app.scrape.models import ScrapeCandidate, ScrapeTarget
    from app.scrape.store import build_failed_case, load_failed_cases, save_failed_case

    _cleanup()
    try:
        target = ScrapeTarget(
            scrape_target_id="t_debug",
            source="baidu",
            import_plan_id="p1",
            work_id="w1",
            media_type="tv",
            group_type="season",
            series_group="冰海战记",
            local_title="冰海战记",
            scrape_title="Vinland Saga",
            scrape_year=2019,
            scrape_type="tv",
            local_season_number=1,
            target_dir="D:/mirror/冰海战记/Season 1",
            item_ids=["ep1", "ep2"],
            warnings=["样本 warning"],
        )
        candidate = ScrapeCandidate(
            candidate_id="c1",
            provider="tmdb",
            tmdb_id=88803,
            tmdb_type="tv",
            title="Vinland Saga",
            original_title="ヴィンランド・サガ",
            year=2019,
            score=91,
            reasons=["标题完全匹配"],
        )
        try:
            raise RuntimeError("模拟刮削失败")
        except RuntimeError as exc:
            save_failed_case(build_failed_case(
                target=target,
                candidate=candidate,
                candidates=[candidate],
                error=exc,
                stage="auto_execute",
            ))

        cases = load_failed_cases()
        assert len(cases) == 1
        case = cases[0]
        assert case["scrape_target_id"] == "t_debug"
        assert case["source"] == "baidu"
        assert case["exception_type"] == "RuntimeError"
        assert "模拟刮削失败" in case["traceback"]
        assert case["target"]["target_dir"].endswith("Season 1")
        assert case["target"]["item_ids"] == ["ep1", "ep2"]
        assert case["selected_candidate"]["tmdb_id"] == 88803
        assert case["candidates"][0]["title"] == "Vinland Saga"
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [test_upsert_new_item, test_upsert_update_item, test_preserve_season_numbers]
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
