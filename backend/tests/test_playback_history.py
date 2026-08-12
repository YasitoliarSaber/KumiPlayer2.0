# -*- coding: utf-8 -*-
"""播放历史测试"""

import shutil
import sys
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

_DATA_DIR = Path(__file__).parent.parent.parent / "data"


def _cleanup():
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR)


def test_save_and_load_history():
    """保存和加载播放历史"""
    from app.playback.history import save_history, load_history, build_history_item

    _cleanup()
    try:
        item = build_history_item(
            work_id="w1", work_title="CLANNAD", episode_id="ep1",
            episode_title="在樱花飞散的坡道", source="pan115",
            media_type="tv", group_type="season", season_number=1,
            episode_number=1, strm_path="/tmp/test.strm",
        )
        save_history(item)
        items = load_history()
        assert len(items) == 1
        assert items[0].work_id == "w1"
        assert items[0].episode_number == 1
    finally:
        _cleanup()


def test_history_order():
    """历史按 played_at 倒序"""
    from app.playback.history import save_history, get_history, build_history_item

    _cleanup()
    try:
        for i in range(3):
            item = build_history_item(
                work_id="w1", work_title="CLANNAD", episode_id=f"ep{i}",
                episode_title=f"Episode {i}", source="pan115",
                media_type="tv", group_type="season", season_number=1,
                episode_number=i, strm_path=f"/tmp/test{i}.strm",
            )
            save_history(item)

        items = get_history()
        assert len(items) == 3
        # 最新在前
        assert items[0].episode_number == 2
        assert items[1].episode_number == 1
        assert items[2].episode_number == 0
    finally:
        _cleanup()


def test_load_history_sorts_existing_file():
    """已有 history.json 顺序异常时，加载仍按 played_at 倒序"""
    from app.playback.history import load_history

    _cleanup()
    try:
        history_path = _DATA_DIR / "playback" / "history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps([
            {
                "history_id": "old",
                "work_id": "w1",
                "work_title": "CLANNAD",
                "episode_id": "ep1",
                "episode_title": "Episode 1",
                "source": "pan115",
                "media_type": "tv",
                "group_type": "season",
                "season_number": 1,
                "episode_number": 1,
                "strm_path": "/tmp/ep1.strm",
                "poster_path": "",
                "played_at": "2026-06-13T10:00:00+08:00",
            },
            {
                "history_id": "new",
                "work_id": "w1",
                "work_title": "CLANNAD",
                "episode_id": "ep2",
                "episode_title": "Episode 2",
                "source": "pan115",
                "media_type": "tv",
                "group_type": "season",
                "season_number": 1,
                "episode_number": 2,
                "strm_path": "/tmp/ep2.strm",
                "poster_path": "",
                "played_at": "2026-06-13T11:00:00+08:00",
            },
        ], ensure_ascii=False), encoding="utf-8")

        items = load_history()
        assert [item.episode_id for item in items] == ["ep2", "ep1"]
    finally:
        _cleanup()


def test_history_limit():
    """limit 生效"""
    from app.playback.history import save_history, get_history, build_history_item

    _cleanup()
    try:
        for i in range(5):
            item = build_history_item(
                work_id="w1", work_title="CLANNAD", episode_id=f"ep{i}",
                episode_title=f"Episode {i}", source="pan115",
                media_type="tv", group_type="season", season_number=1,
                episode_number=i, strm_path=f"/tmp/test{i}.strm",
            )
            save_history(item)

        items = get_history(limit=2)
        assert len(items) == 2
    finally:
        _cleanup()


def test_history_filter_by_work_id():
    """按 work_id 筛选"""
    from app.playback.history import save_history, get_history, build_history_item

    _cleanup()
    try:
        for work_id in ["w1", "w2", "w1"]:
            item = build_history_item(
                work_id=work_id, work_title="Test", episode_id="ep1",
                episode_title="Ep1", source="pan115",
                media_type="tv", group_type="season", season_number=1,
                episode_number=1, strm_path="/tmp/test.strm",
            )
            save_history(item)

        items = get_history(work_id="w1")
        assert len(items) == 2
        assert all(i.work_id == "w1" for i in items)
    finally:
        _cleanup()


def test_continue_returns_latest():
    """continue 返回指定作品最近播放条目"""
    from app.playback.history import save_history, get_continue_item, build_history_item

    _cleanup()
    try:
        for ep_num in [1, 2, 3]:
            item = build_history_item(
                work_id="w1", work_title="CLANNAD", episode_id=f"ep{ep_num}",
                episode_title=f"Episode {ep_num}", source="pan115",
                media_type="tv", group_type="season", season_number=1,
                episode_number=ep_num, strm_path=f"/tmp/test{ep_num}.strm",
            )
            save_history(item)

        cont = get_continue_item("w1")
        assert cont is not None
        assert cont.episode_number == 3
    finally:
        _cleanup()


def test_continue_no_history():
    """无历史返回 None"""
    from app.playback.history import get_continue_item

    _cleanup()
    try:
        cont = get_continue_item("nonexistent")
        assert cont is None
    finally:
        _cleanup()


if __name__ == "__main__":
    tests = [
        test_save_and_load_history,
        test_history_order,
        test_load_history_sorts_existing_file,
        test_history_limit,
        test_history_filter_by_work_id,
        test_continue_returns_latest,
        test_continue_no_history,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
