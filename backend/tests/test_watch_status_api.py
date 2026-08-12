# -*- coding: utf-8 -*-
"""Local media-library watch status API tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _redirect_watch_status_store(tmp_path, monkeypatch):
    from app.library import watch_status

    monkeypatch.setattr(watch_status, "_status_path", lambda: tmp_path / "cache" / "watch_status.json")


def test_watch_status_default_and_patch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    _redirect_watch_status_store(tmp_path, monkeypatch)
    client = TestClient(app)

    response = client.get("/api/library/watch-status/work-1")
    assert response.status_code == 200
    assert response.json()["status"] == ""


def test_watch_status_persists_favorite_without_changing_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    _redirect_watch_status_store(tmp_path, monkeypatch)
    client = TestClient(app)

    response = client.patch(
        "/api/library/watch-status/work-favorite",
        json={"status": "", "favorite": True},
    )

    assert response.status_code == 200
    assert response.json()["favorite"] is True
    assert response.json()["status"] == ""
    assert client.get("/api/library/watch-status/work-favorite").json()["favorite"] is True

    response = client.patch("/api/library/watch-status/work-1", json={"status": "watching"})
    assert response.status_code == 200
    data = response.json()
    assert data["work_id"] == "work-1"
    assert data["status"] == "watching"
    assert data["updated_at"]

    response = client.get("/api/library/watch-status")
    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "watching"

    response = client.patch("/api/library/watch-status/work-1", json={"status": ""})
    assert response.status_code == 200
    assert response.json()["status"] == ""


def test_watch_status_rejects_unknown_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    _redirect_watch_status_store(tmp_path, monkeypatch)
    client = TestClient(app)

    response = client.patch("/api/library/watch-status/work-1", json={"status": "unknown"})
    assert response.status_code == 400


def test_library_payload_includes_and_filters_local_watch_status(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.core import paths as core_paths
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import invalidate_library_index_cache, save_library_index
    from app.main import app

    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    _redirect_watch_status_store(tmp_path, monkeypatch)
    invalidate_library_index_cache()
    save_library_index(LibraryIndex(works=[
        WorkIndex(work_id="work-1", title="夏日回声", episodes=[]),
        WorkIndex(work_id="work-2", title="月光下的异旅", episodes=[]),
    ]))
    client = TestClient(app)

    response = client.patch("/api/library/watch-status/work-1", json={"status": "watching"})
    assert response.status_code == 200

    response = client.get("/api/library")
    assert response.status_code == 200
    works = {item["work_id"]: item for item in response.json()["works"]}
    assert works["work-1"]["watch_status"]["status"] == "watching"
    assert works["work-2"]["watch_status"]["status"] == ""

    response = client.get("/api/library?watch_status=watching")
    assert response.status_code == 200
    assert [item["work_id"] for item in response.json()["works"]] == ["work-1"]

    response = client.get("/api/library/works/work-1")
    assert response.status_code == 200
    assert response.json()["watch_status"]["status"] == "watching"


def test_local_watch_status_does_not_call_bangumi_collection_sync(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    _redirect_watch_status_store(tmp_path, monkeypatch)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("local watch status must not call Bangumi")

    monkeypatch.setattr(bangumi_api, "BangumiClient", fail_if_called)
    client = TestClient(app)

    response = client.patch("/api/library/watch-status/work-1", json={"status": "watched"})
    assert response.status_code == 200
    assert response.json()["status"] == "watched"
