# -*- coding: utf-8 -*-
"""手动删除 API 测试"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from app.main import app

_DATA_DIR = Path(__file__).parent.parent.parent / "data"


def _cleanup():
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR)


def _setup_library():
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    strm_dir = _DATA_DIR / "mirror" / "115" / "CLANNAD" / "Season 1"
    strm_dir.mkdir(parents=True, exist_ok=True)
    strm_path = strm_dir / "CLANNAD.S01E01.strm"
    strm_path.write_text("fake_video.mkv\n", encoding="utf-8")

    work = WorkIndex(
        work_id="w1",
        title="CLANNAD",
        source="pan115",
        media_type="tv",
        episodes=[
            EpisodeIndex(
                episode_id="ep1",
                work_id="w1",
                season_number=1,
                episode_number=1,
                title="Ep1",
                group_type="season",
                strm_path=str(strm_path),
            ),
        ],
    )
    save_library_index(LibraryIndex(works=[work]))
    return strm_path


def test_delete_library_confirm_rejects_active_background_tasks(monkeypatch):
    """全局删除必须与镜像、刮削等后台任务互斥。"""
    class BusyTaskManager:
        def maintenance(self, label):
            raise ValueError(f"存在正在运行的后台任务，无法{label}")

    _cleanup()
    try:
        strm_path = _setup_library()
        client = TestClient(app)
        preview_resp = client.post(
            "/api/library/delete/library/preview",
            json={"source": "all"},
        )
        assert preview_resp.status_code == 200, preview_resp.text
        monkeypatch.setattr("app.api.library.get_task_manager", lambda: BusyTaskManager())

        response = client.post(
            "/api/library/delete/library/confirm",
            json={"preview_id": preview_resp.json()["preview_id"]},
        )

        assert response.status_code == 409
        assert strm_path.exists()
    finally:
        _cleanup()


def test_delete_single_work_preview_and_confirm_routes(monkeypatch):
    """详情页作品删除必须经过持久化预览与明确确认。"""
    from contextlib import nullcontext

    class Manager:
        def maintenance(self, _label):
            return nullcontext()

    _cleanup()
    try:
        strm_path = _setup_library()
        monkeypatch.setattr("app.api.library.get_task_manager", lambda: Manager())
        client = TestClient(app)

        preview = client.post("/api/library/works/w1/delete/preview")
        assert preview.status_code == 200
        assert preview.json()["scope"] == "work"
        assert preview.json()["work_id"] == "w1"

        confirmed = client.post(
            "/api/library/works/w1/delete/confirm",
            json={"preview_id": preview.json()["preview_id"]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["deleted_library_work_count"] == 1
        assert not strm_path.exists()
    finally:
        _cleanup()
