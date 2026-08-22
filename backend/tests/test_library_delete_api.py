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


def _seed_v3_scrape_rows(work_id: str = "w1", source: str = "pan115") -> None:
    """预置 V3 SQLite 刮削事实：binding + 孤儿候选 reviews/failures + 队列/缓存。"""
    from app.db.database import get_connection, init_db

    init_db()
    conn = get_connection()
    now = "2026-08-22T12:00:00+08:00"
    conn.execute(
        "INSERT OR REPLACE INTO scrape_bindings "
        "(binding_id, revision_id, work_id, source, provider_id, status, created_at, updated_at) "
        "VALUES ('bind-1', 'rev-1', ?, ?, 'pan115', 'ready', ?, ?)",
        (work_id, source, now, now),
    )
    conn.execute(
        "INSERT OR REPLACE INTO scrape_reviews "
        "(review_id, binding_id, local_title, reason, candidates_json, status, added_at, updated_at) "
        "VALUES ('review-1', 'bind-1', 'T', 'r', '[]', 'pending', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT OR REPLACE INTO scrape_failures "
        "(binding_id, error, stage, retryable, timestamp) VALUES ('bind-1', 'e', 's', 0, ?)",
        (now,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO scrape_review_queue "
        "(scrape_target_id, source, series_group, local_title, added_at, status) "
        "VALUES ('target-1', ?, 'g', 'T', ?, 'pending')",
        (source, now),
    )
    conn.execute(
        "INSERT OR REPLACE INTO scrape_candidate_cache "
        "(cache_id, scrape_target_id, provider, tmdb_id, tmdb_type, title, cached_at) "
        "VALUES ('cache-1', 'target-1', 'tmdb', 1, 'tv', 'T', ?)",
        (now,),
    )
    conn.commit()


def test_work_delete_clears_v3_scrape_database_state(monkeypatch):
    """作品删除必须同步清除 scrape_bindings 与其孤儿 reviews/failures。"""
    from contextlib import nullcontext

    from app.db.database import get_connection

    class Manager:
        def maintenance(self, _label):
            return nullcontext()

    _cleanup()
    try:
        _setup_library()
        _seed_v3_scrape_rows()
        monkeypatch.setattr("app.api.library.get_task_manager", lambda: Manager())
        client = TestClient(app)

        preview = client.post("/api/library/works/w1/delete/preview")
        confirmed = client.post(
            "/api/library/works/w1/delete/confirm",
            json={"preview_id": preview.json()["preview_id"]},
        )
        assert confirmed.status_code == 200, confirmed.text

        conn = get_connection()
        assert conn.execute(
            "SELECT COUNT(*) FROM scrape_bindings WHERE work_id = 'w1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM scrape_reviews WHERE binding_id = 'bind-1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM scrape_failures WHERE binding_id = 'bind-1'"
        ).fetchone()[0] == 0
    finally:
        _cleanup()


def test_source_library_delete_clears_v3_scrape_database_state(monkeypatch):
    """按来源清库必须同步清除 SQLite 队列/缓存/绑定，不留双写脏状态。"""
    from contextlib import nullcontext

    from app.db.database import get_connection

    class Manager:
        def maintenance(self, _label):
            return nullcontext()

    _cleanup()
    try:
        _setup_library()
        _seed_v3_scrape_rows()
        monkeypatch.setattr("app.api.library.get_task_manager", lambda: Manager())
        client = TestClient(app)

        preview = client.post("/api/library/delete/library/preview", json={"source": "pan115"})
        assert preview.status_code == 200, preview.text
        confirmed = client.post(
            "/api/library/delete/library/confirm",
            json={"preview_id": preview.json()["preview_id"]},
        )
        assert confirmed.status_code == 200, confirmed.text

        conn = get_connection()
        assert conn.execute(
            "SELECT COUNT(*) FROM scrape_review_queue WHERE source = 'pan115'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM scrape_candidate_cache WHERE scrape_target_id = 'target-1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM scrape_bindings WHERE source = 'pan115'"
        ).fetchone()[0] == 0
    finally:
        _cleanup()
