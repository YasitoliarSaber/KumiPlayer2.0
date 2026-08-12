# -*- coding: utf-8 -*-
"""播放 API 测试"""

import shutil
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from app.main import app

_DATA_DIR = Path(__file__).parent.parent.parent / "data"

client = TestClient(app)


def _cleanup():
    from app.playback.service import reset_playback_manager
    reset_playback_manager()
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR)


def _setup_library_index():
    """创建测试用 LibraryIndex"""
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.store import save_library_index

    strm_dir = _DATA_DIR / "mirror" / "115" / "CLANNAD" / "Season 1"
    strm_dir.mkdir(parents=True, exist_ok=True)

    work = WorkIndex(
        work_id="w1",
        title="CLANNAD",
        source="pan115",
        media_type="tv",
        poster_path="",
        episodes=[
            EpisodeIndex(
                episode_id="ep1", work_id="w1",
                season_number=1, episode_number=1,
                title="在樱花飞散的坡道", group_type="season",
                strm_path=str(strm_dir / "test_s01e01.strm"),
            ),
            EpisodeIndex(
                episode_id="ep2", work_id="w1",
                season_number=1, episode_number=2,
                title="Episode 2", group_type="season",
                strm_path=str(strm_dir / "test_s01e02.strm"),
            ),
        ],
        seasons=[
            SeasonIndex(season_id="s1", work_id="w1", season_number=1, group_type="season", label="第1季", episode_count=2),
        ],
    )
    index = LibraryIndex(works=[work])
    save_library_index(index)

    # 创建 .strm 文件
    for ep in work.episodes:
        p = Path(ep.strm_path)
        p.write_text("fake_video_path.mkv\n", encoding="utf-8")


def test_play_returns_session():
    """POST /api/playback/play 返回 session"""
    _cleanup()
    try:
        _setup_library_index()
        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 9999
            mock_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
            mock_popen.return_value = mock_process

            resp = client.post("/api/playback/play", json={"work_id": "w1", "episode_id": "ep1"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "playing"
            assert data["pid"] == 9999
            assert data["episode_id"] == "ep1"

            wait_event.set()
            import time; time.sleep(0.3)
            client.post("/api/playback/stop")
    finally:
        _cleanup()


def test_stop_returns_idle():
    """无播放时 stop 返回 idle"""
    _cleanup()
    try:
        resp = client.post("/api/playback/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"
    finally:
        _cleanup()


def test_status_returns_idle():
    """无播放时 status 返回 idle"""
    _cleanup()
    try:
        resp = client.get("/api/playback/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "idle"
    finally:
        _cleanup()


def test_history_empty():
    """无历史时返回空列表"""
    _cleanup()
    try:
        resp = client.get("/api/playback/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
    finally:
        _cleanup()


def test_continue_no_history():
    """无历史时 continue 返回 404"""
    _cleanup()
    try:
        resp = client.get("/api/playback/continue/w1")
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_play_nonexistent_work_404():
    """不存在的 work_id 返回 404"""
    _cleanup()
    try:
        _setup_library_index()
        resp = client.post("/api/playback/play", json={"work_id": "nonexistent", "episode_id": "ep1"})
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_play_strm_path_outside_library_400():
    """strm_path 不属于 LibraryIndex 返回 400"""
    _cleanup()
    try:
        _setup_library_index()
        resp = client.post(
            "/api/playback/play",
            json={"work_id": "w1", "strm_path": "C:\\Windows\\notepad.exe"},
        )
        assert resp.status_code == 400
    finally:
        _cleanup()


def test_play_no_library_index_409():
    """无 LibraryIndex 返回 409"""
    _cleanup()
    try:
        resp = client.post("/api/playback/play", json={"work_id": "w1", "episode_id": "ep1"})
        assert resp.status_code == 409
    finally:
        _cleanup()


if __name__ == "__main__":
    tests = [
        test_play_returns_session,
        test_stop_returns_idle,
        test_status_returns_idle,
        test_history_empty,
        test_continue_no_history,
        test_play_nonexistent_work_404,
        test_play_strm_path_outside_library_400,
        test_play_no_library_index_409,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
