"""V4 导入 API 契约：revision 是唯一计划身份。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
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
    from app.api import media_v4

    activated_scan_ids = []
    monkeypatch.setattr(media_v4, "activate_scan_state", activated_scan_ids.append)

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
    assert activated_scan_ids == ["scan-api"]

    confirmed_evidence = media_v4._confirmed_source_evidence("root-api")
    assert [item.relative_path for item in confirmed_evidence] == ["Show/Show.S01E01.mkv"]

    library = client.get("/api/v4/library")
    assert library.status_code == 200, library.text
    assert library.json()["cards"][0]["title"] == "Show"


def test_confirmed_source_has_a_reopenable_card_with_live_job_summary(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload("rev-source-card")
    payload.update({
        "source_display_name": "本地动画库",
        "source_locator": "D:\\Media\\Anime",
        "playback_locator": "D:\\Media\\Anime",
    })

    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-source-card/confirm").status_code == 200

    response = client.get("/api/v4/sources/libraries")
    assert response.status_code == 200, response.text
    cards = response.json()["cards"]
    assert len(cards) == 1
    card = cards[0]
    assert card["root_id"] == "root-api"
    assert card["revision_id"] == "rev-source-card"
    assert card["display_name"] == "本地动画库"
    assert card["source_locator"] == "D:\\Media\\Anime"
    assert card["evidence_count"] == 1
    assert card["work_count"] == 1
    assert card["job_summary"]["total"] > 0
    assert card["job_summary"]["queued"] > 0
    assert card["can_resume"] is True


def test_source_card_list_omits_unconfirmed_drafts(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/v4/imports/preview", json=_payload("rev-draft-only")).status_code == 200

    response = client.get("/api/v4/sources/libraries")
    assert response.status_code == 200
    assert response.json()["cards"] == []


def test_preview_with_unknown_title_returns_review_issue_and_confirm_conflict(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload("rev-review")
    payload["entries"][0]["relative_path"] = "unknown/0001.mkv"

    preview = client.post("/api/v4/imports/preview", json=payload)
    assert preview.status_code == 200
    assert preview.json()["issues"]

    confirmed = client.post("/api/v4/imports/rev-review/confirm")
    assert confirmed.status_code == 409


def test_hybrid_tree_scan_reuses_the_openlist_root_identity(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.sources.scanner import openlist_root_id

    tree = tmp_path / "anime-tree.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="",
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(
        media_v4,
        "resolve_openlist_credentials",
        lambda: ("kumi", "secret", "available"),
    )
    monkeypatch.setattr(media_v4, "stage_scan_state", lambda _scan_id, _state: None)

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="hybrid",
        root_path="/Anime",
        tree_file=str(tree),
        provider="pan115",
    ))

    assert result["root_id"] == openlist_root_id(
        config.openlist_server_url,
        "kumi",
        "/Anime",
    )
    assert result["entries"][0]["ingest_method"] == "directory_tree"
    assert result["entries"][0]["relative_path"] == "Show/Show.S01E01.mkv"


def test_tree_scan_preserves_quark_as_the_content_provider(tmp_path):
    from app.api import media_v4

    tree = tmp_path / "quark-tree.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="quark",
    ))

    assert result["entries"][0]["provider"] == "quark"


def test_explicit_openlist_incremental_requires_a_confirmed_txt_baseline(monkeypatch):
    from fastapi import HTTPException

    from app.api import media_v4

    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda _root_id: [])

    with pytest.raises(HTTPException) as exc_info:
        media_v4.scan_source(media_v4.SourceScanRequest(
            source="openlist",
            root_path="/Anime",
            provider="pan115",
            scan_mode="incremental",
        ))

    assert exc_info.value.status_code == 409
    assert "TXT 基线" in exc_info.value.detail


def test_openlist_auto_scan_rebuilds_missing_checkpoint_from_confirmed_revision(monkeypatch):
    from app.api import media_v4, openlist_v4
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.scanner import openlist_root_id

    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[],
    )
    root_id = openlist_root_id(config.openlist_server_url, "kumi", "/Anime")
    baseline = [to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id="scan-confirmed",
        provider="pan115",
        ingest_method="directory_tree",
        relative_path="Show/Show.S01E01.mkv",
    ))]
    captured = {}

    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda _root_id: baseline)
    monkeypatch.setattr(media_v4, "load_active_state", lambda _root_id: None)
    monkeypatch.setattr(openlist_v4, "_client", lambda _config: object())
    monkeypatch.setattr(media_v4, "scan_openlist_directory", lambda *_args, **_kwargs: pytest.fail("不应退回全量扫描"))

    def fake_incremental(_client, **kwargs):
        captured["state"] = kwargs["state"]
        return "scan-incremental", baseline, kwargs["state"], {"requested_directories": 1}

    monkeypatch.setattr(media_v4, "scan_openlist_incremental", fake_incremental)
    monkeypatch.setattr(media_v4, "stage_scan_state", lambda scan_id, _state: captured.setdefault("scan_id", scan_id))

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="openlist",
        root_path="/Anime",
        provider="pan115",
    ))

    assert result["scan_mode"] == "incremental"
    assert captured["scan_id"] == "scan-incremental"
    assert captured["state"]["remote_verified"] is False
    assert captured["state"]["root_id"] == root_id


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
