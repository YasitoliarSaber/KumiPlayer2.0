"""V4 媒体库读模型 API 合同。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    from app.api import library_v4, media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "library-api.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(media_v4.router)
    application.include_router(library_v4.router)
    return TestClient(application), database


def _entry(revision_id: str = "rev-library") -> dict:
    return {
        "revision_id": revision_id,
        "root_id": "root-library",
        "scan_id": f"scan-{revision_id}",
        "entries": [
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": "Show/Show.S01E01.mkv",
                "source_locator": "local://show/Show.S01E01.mkv",
                "playback_locator": "local://show/Show.S01E01.mkv",
            },
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": "Show/Show.S01E01.2160p.mkv",
                "source_locator": "local://show/Show.S01E01.2160p.mkv",
                "playback_locator": "local://show/Show.S01E01.2160p.mkv",
            },
        ],
    }


def test_detail_deduplicates_episode_rows_but_exposes_all_assets(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    assert client.post("/api/v4/imports/preview", json=_entry()).status_code == 200
    assert client.post("/api/v4/imports/rev-library/confirm").status_code == 200
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]

    response = client.get(f"/api/library/works/{work_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["media_type"] == "tv"
    assert body["show_type"] == "anime_series"
    assert len(body["episodes"]) == 1
    assert len(body["episodes"][0]["assets"]) == 2
    assert body["seasons"][0]["episode_count"] == 1


def test_watch_status_patch_preserves_omitted_fields_and_rescan_is_v4_projection(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    assert client.post("/api/v4/imports/preview", json=_entry("rev-watch")).status_code == 200
    assert client.post("/api/v4/imports/rev-watch/confirm").status_code == 200
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]

    first = client.patch(
        f"/api/library/watch-status/{work_id}",
        json={"status": "watching", "note": "keep", "favorite": True},
    )
    assert first.status_code == 200
    second = client.patch(f"/api/library/watch-status/{work_id}", json={"note": "updated"})
    assert second.status_code == 200
    assert second.json()["status"] == "watching"
    assert second.json()["favorite"] is True
    assert second.json()["note"] == "updated"

    rescan = client.post("/api/library/rescan")
    assert rescan.status_code == 200
    assert rescan.json()["status"] == "succeeded"
    assert rescan.json()["task_id"].startswith("projection:")


def test_detail_only_exposes_latest_confirmed_revision_for_same_root(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    first = _entry("rev-old")
    assert client.post("/api/v4/imports/preview", json=first).status_code == 200
    assert client.post("/api/v4/imports/rev-old/confirm").status_code == 200

    second = _entry("rev-new")
    second["entries"] = [{
        "provider": "local",
        "ingest_method": "local_scan",
        "relative_path": "Show/Show.S01E02.mkv",
        "source_locator": "local://show/Show.S01E02.mkv",
        "playback_locator": "local://show/Show.S01E02.mkv",
    }]
    assert client.post("/api/v4/imports/preview", json=second).status_code == 200
    assert client.post("/api/v4/imports/rev-new/confirm").status_code == 200

    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]
    detail = client.get(f"/api/library/works/{work_id}")
    assert detail.status_code == 200
    assert [episode["episode_number"] for episode in detail.json()["episodes"]] == [2]
    assert detail.json()["asset_count"] == 1


def test_library_sources_come_from_each_evidence_not_the_first_root_entry(tmp_path, monkeypatch):
    client, _database = _client(tmp_path, monkeypatch)
    payload = _entry("rev-mixed-provider")
    payload["entries"][1]["provider"] = "pan115"

    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-mixed-provider/confirm").status_code == 200
    response = client.get("/api/library")

    assert response.status_code == 200
    assert response.json()["works"][0]["sources"] == ["local", "pan115"]


def test_empty_scan_can_confirm_source_removal_and_publish_empty_library(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    assert client.post("/api/v4/imports/preview", json=_entry("rev-populated")).status_code == 200
    assert client.post("/api/v4/imports/rev-populated/confirm").status_code == 200

    empty = {
        "revision_id": "rev-empty",
        "root_id": "root-library",
        "scan_id": "scan-empty",
        "entries": [],
    }
    assert client.post("/api/v4/imports/preview", json=empty).status_code == 409
    empty["allow_empty"] = True
    preview = client.post("/api/v4/imports/preview", json=empty)
    assert preview.status_code == 200
    assert preview.json()["works"] == []
    assert client.post("/api/v4/imports/rev-empty/confirm").status_code == 200

    with database.connect() as conn:
        projection_job = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE revision_id = 'rev-empty' AND job_type = 'refresh_projection'"
        ).fetchone()[0]
        work_jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE revision_id = 'rev-empty' AND work_id != ''"
        ).fetchone()[0]
    assert projection_job == 1
    assert work_jobs == 0
    assert client.post("/api/library/rescan").status_code == 200
    assert client.get("/api/library").json()["works"] == []
