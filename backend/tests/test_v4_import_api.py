"""V4 导入 API 契约：revision 是唯一计划身份。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "api.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(media_v4.router)
    return TestClient(application)


def _payload(revision_id: str = "rev-api"):
    return {
        "revision_id": revision_id,
        "root_id": "root-api",
        "scan_id": "scan-api",
        "entries": [
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": "Show/Show.S01E01.mkv",
                "source_locator": "local://show/Show.S01E01.mkv",
                "playback_locator": "local://show/Show.S01E01.mkv",
            }
        ],
    }


def test_preview_confirm_and_library_use_revision_work_and_job_identities(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    preview = client.post("/api/v4/imports/preview", json=_payload())
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["revision_id"] == "rev-api"
    assert body["status"] == "draft"
    assert body["works"][0]["work_key"]
    assert "plan_id" not in body

    confirmed = client.post("/api/v4/imports/rev-api/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    assert all("job_id" in job for job in confirmed.json()["jobs"])

    library = client.get("/api/v4/library")
    assert library.status_code == 200, library.text
    assert library.json()["cards"][0]["title"] == "Show"


def test_preview_with_unknown_title_returns_review_issue_and_confirm_conflict(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload("rev-review")
    payload["entries"][0]["relative_path"] = "unknown/0001.mkv"

    preview = client.post("/api/v4/imports/preview", json=payload)
    assert preview.status_code == 200
    assert preview.json()["issues"]

    confirmed = client.post("/api/v4/imports/rev-review/confirm")
    assert confirmed.status_code == 409


def test_playback_and_tracking_are_user_state_endpoints(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    preview = client.post("/api/v4/imports/preview", json=_payload("rev-state"))
    assert preview.status_code == 200
    client.post("/api/v4/imports/rev-state/confirm")

    with media_v4._database.connect() as conn:
        row = conn.execute(
            """
            SELECT w.work_id, e.episode_id, a.asset_id
            FROM works w JOIN episodes e ON e.work_id = w.work_id
            JOIN episode_assets ea ON ea.episode_id = e.episode_id
            JOIN assets a ON a.asset_id = ea.asset_id
            LIMIT 1
            """
        ).fetchone()
    assert row is not None

    progress = client.post(
        "/api/v4/playback/progress",
        json={
            "work_id": row["work_id"],
            "episode_id": row["episode_id"],
            "asset_id": row["asset_id"],
            "position": 12.5,
            "duration": 100,
            "completed": False,
        },
    )
    assert progress.status_code == 200
    assert progress.json()["position"] == 12.5

    tracking = client.post(
        "/api/v4/tracking/state",
        json={
            "work_id": row["work_id"],
            "provider": "bangumi",
            "provider_id": "subject-1",
            "last_watched_episode": 1,
        },
    )
    assert tracking.status_code == 200
    assert tracking.json()["provider_id"] == "subject-1"
