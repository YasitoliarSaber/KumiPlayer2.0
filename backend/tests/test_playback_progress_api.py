# -*- coding: utf-8 -*-
"""Playback progress and completed-episode API tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_playback_progress_marks_episode_completed_at_threshold(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core import paths as core_paths
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import invalidate_library_index_cache, save_library_index
    from app.main import app

    monkeypatch.setattr(core_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    invalidate_library_index_cache()
    save_library_index(LibraryIndex(works=[
        WorkIndex(
            work_id="work-1",
            title="夏日回声",
            episodes=[EpisodeIndex(
                episode_id="ep-1",
                work_id="work-1",
                season_number=1,
                episode_number=1,
                title="第一集",
                strm_path=str(tmp_path / "S01E01.strm"),
            )],
        )
    ]))

    client = TestClient(app)
    response = client.post(
        "/api/playback/progress",
        json={"work_id": "work-1", "episode_id": "ep-1", "position": 950, "duration": 1000},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["completed"] is True
    assert data["ratio"] == 0.95

    response = client.get("/api/playback/progress?work_id=work-1")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["episode_id"] == "ep-1"
    assert item["completed"] is True


def test_playback_progress_syncs_bangumi_once_when_completion_threshold_is_crossed(tmp_path, monkeypatch):
    from app.core import paths as core_paths
    from app.playback import progress as progress_store

    monkeypatch.setattr(core_paths, "get_data_dir", lambda: tmp_path / "data")
    calls = []

    def sync_episode(work_id, episode_id):
        calls.append((work_id, episode_id))
        return True, ""

    monkeypatch.setattr(progress_store, "_try_sync_bangumi_episode", sync_episode)

    item = progress_store.save_progress("work-1", "ep-8", 950, 1000)
    item = progress_store.save_progress("work-1", "ep-8", 980, 1000)

    assert item.completed is True
    assert item.bangumi_synced is True
    assert item.bangumi_error == ""
    assert calls == [("work-1", "ep-8")]


def test_playback_progress_can_defer_bangumi_sync_for_mpv_event_thread(tmp_path, monkeypatch):
    from app.core import paths as core_paths
    from app.playback import progress as progress_store

    monkeypatch.setattr(core_paths, "get_data_dir", lambda: tmp_path / "data")
    sync_calls = []
    monkeypatch.setattr(
        progress_store,
        "_try_sync_bangumi_episode",
        lambda work_id, episode_id: sync_calls.append((work_id, episode_id)) or (True, ""),
    )

    item = progress_store.save_progress(
        "work-1",
        "ep-8",
        960,
        1000,
        sync_bangumi=False,
    )

    assert item.completed is True
    assert item.bangumi_synced is False
    assert sync_calls == []


def test_completed_episode_keeps_local_state_when_bangumi_sync_fails(tmp_path, monkeypatch):
    from app.core import paths as core_paths
    from app.playback import progress as progress_store

    monkeypatch.setattr(core_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(
        progress_store,
        "_try_sync_bangumi_episode",
        lambda work_id, episode_id: (False, "Bangumi 请求超时"),
    )

    item = progress_store.save_progress("work-1", "ep-8", 950, 1000)

    assert item.completed is True
    assert item.bangumi_synced is False
    assert item.bangumi_error == "Bangumi 请求超时"


def test_playback_progress_below_threshold_is_not_completed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(paths := core_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(paths, "get_cache_dir", lambda: tmp_path / "cache")

    client = TestClient(app)
    response = client.post(
        "/api/playback/progress",
        json={"work_id": "work-1", "episode_id": "ep-1", "position": 940, "duration": 1000},
    )
    assert response.status_code == 200
    assert response.json()["completed"] is False


def test_playback_progress_manual_mark_completed_and_unwatched(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(core_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")

    client = TestClient(app)
    response = client.post(
        "/api/playback/progress/mark",
        json={"work_id": "work-1", "episode_id": "ep-1", "completed": True},
    )
    assert response.status_code == 200
    assert response.json()["completed"] is True

    response = client.post(
        "/api/playback/progress/mark",
        json={"work_id": "work-1", "episode_id": "ep-1", "completed": False},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["completed"] is False
    assert data["position"] == 0
    assert data["duration"] == 0
    assert data["ratio"] == 0
    assert data["manually_unwatched"] is True


def test_manual_unwatched_override_survives_partial_playback(tmp_path, monkeypatch):
    from app.core import paths as core_paths
    from app.playback import progress as progress_store

    monkeypatch.setattr(core_paths, "get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(progress_store, "_try_sync_bangumi_episode", lambda *_args: (True, ""))

    progress_store.mark_episode_completed("work-1", "ep-9", False)
    partial = progress_store.save_progress("work-1", "ep-9", 450, 1000)
    completed = progress_store.save_progress("work-1", "ep-9", 950, 1000)

    assert partial.completed is False
    assert partial.manually_unwatched is True
    assert completed.completed is True
    assert completed.manually_unwatched is False
