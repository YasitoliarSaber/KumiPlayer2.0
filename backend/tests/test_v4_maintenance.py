"""P-005 媒体库维护 service 合同：预览/确认/混合来源保留/路径白名单/幂等。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "maintenance.db")
    database.initialize()
    return database


def _seed_confirmed(database, *, root_id, provider, work_ids, scan_id="scan-s", revision_id="rev-s", generation=1, confirmed_at="2026-08-25T00:00:00+00:00"):
    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, created_at, updated_at) "
            "VALUES (?, ?, 'local_scan', ?, ?, 'now', 'now')",
            (root_id, provider, f"loc-{root_id}", f"play-{root_id}"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO source_scans(scan_id, root_id, generation, status) VALUES (?, ?, ?, 'completed')",
            (scan_id, root_id, generation),
        )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at, confirmed_at) "
            "VALUES (?, ?, ?, 'v4', 'confirmed', '', '2026-08-24T00:00:00+00:00', ?)",
            (revision_id, root_id, scan_id, confirmed_at),
        )
        for _index, work_id in enumerate(work_ids):
            conn.execute(
                "INSERT OR IGNORE INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
                "VALUES (?, ?, 'series', ?, 'now', 'now')",
                (work_id, f"ik-{work_id}", f"作品{work_id}"),
            )
            evidence_id = f"ev-{root_id}-{work_id}"
            conn.execute(
                "INSERT INTO source_evidence(evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind, source_locator, playback_locator, ingest_method, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'video', ?, ?, 'local_scan', 'now')",
                (evidence_id, scan_id, root_id, provider, f"rel-{work_id}", f"rel-{work_id}", f"loc-{work_id}", f"play-{work_id}"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO revision_bindings(binding_id, revision_id, evidence_id, work_id, confidence) "
                "VALUES (?, ?, ?, ?, 'high')",
                (f"b-{root_id}-{work_id}", revision_id, evidence_id, work_id),
            )


def _seed_artifact(database, revision_id: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("artifact", encoding="utf-8")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO artifacts(artifact_id, revision_id, work_id, artifact_type, target_path, digest, status, created_at, updated_at) "
            "VALUES (?, ?, 'w-x', 'poster', ?, 'd', 'staged', 'now', 'now')",
            (f"art-{path.name}", revision_id, str(path)),
        )


def test_preview_lists_affected_roots_works_and_artifacts(tmp_path):
    from app.media_v4.maintenance.service import compute_delete_preview

    database = _fresh_database(tmp_path)
    mirror = tmp_path / "mirror"
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-baidu-only"])
    _seed_artifact(database, "rev-s", mirror / "baidu" / "w-baidu-only" / "poster.jpg")

    preview = compute_delete_preview(database, provider="baidu", mirror_root=mirror)
    assert preview["scope"] == "baidu"
    assert preview["root_count"] == 1
    assert preview["work_count"] == 1
    assert preview["orphan_work_count"] == 1
    assert preview["mixed_work_count"] == 0
    assert preview["artifact_count"] == 1
    assert preview["blocked"] is False
    assert preview["digest"]


def test_preview_blocks_when_active_jobs_exist(tmp_path):
    from app.media_v4.maintenance.service import compute_delete_preview

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-115", provider="pan115", work_ids=["w-a"])
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, status, attempts, last_error, created_at, updated_at) "
            "VALUES ('job-run', 'materialize_mirror', 'rev-s', 'w-a', 'ik', 'running', 1, '', 'now', 'now')"
        )

    preview = compute_delete_preview(database, provider="pan115")
    assert preview["blocked"] is True
    assert preview["blocked_job_count"] == 1
    assert preview["blocked_job_types"] == ["materialize_mirror"]


def test_mixed_source_work_is_kept(tmp_path):
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-mixed", "w-orphan"], scan_id="scan-b", revision_id="rev-b")
    _seed_confirmed(database, root_id="root-115", provider="pan115", work_ids=["w-mixed"], scan_id="scan-c", revision_id="rev-c")

    preview = compute_delete_preview(database, provider="baidu")
    with database.connect() as conn:
        stored = json.loads(conn.execute(
            "SELECT preview_json FROM maintenance_operations WHERE operation_id = ?",
            (preview["preview_id"],),
        ).fetchone()["preview_json"])
    assert stored["orphan_works"] == ["w-orphan"]
    assert stored["mixed_works"] == ["w-mixed"]
    assert preview["orphan_work_count"] == 1
    assert preview["mixed_work_count"] == 1

    result = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="baidu", digest=preview["digest"])
    assert result["status"] == "completed"
    assert result["orphan_work_count"] == 1
    assert result["mixed_work_count"] == 1
    # 百度 root 退役，115 root 保留
    with database.connect() as conn:
        rows = {str(r["root_id"]): str(r["retired_at"] or "") for r in conn.execute("SELECT root_id, retired_at FROM source_roots").fetchall()}
    assert rows["root-baidu"] != ""
    assert rows["root-115"] == ""


def test_confirm_rejects_stale_digest(tmp_path):
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-a"])
    preview = compute_delete_preview(database, provider="baidu")
    # 旧 revision 被新 revision 取代（真实链路：确认新 revision 时旧标 superseded）
    with database.connect() as conn:
        conn.execute("UPDATE import_revisions SET status = 'superseded' WHERE revision_id = 'rev-s'")
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-b"], scan_id="scan-new", revision_id="rev-new", generation=2, confirmed_at="2026-08-25T10:00:00+00:00")

    with pytest.raises(ValueError, match="重新生成"):
        confirm_delete_preview(database, preview_id=preview["preview_id"], scope="baidu", digest=preview["digest"])


def test_confirm_is_idempotent_and_removes_artifact(tmp_path):
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    mirror = tmp_path / "mirror"
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-a"])
    artifact = mirror / "baidu" / "w-a" / "poster.jpg"
    _seed_artifact(database, "rev-s", artifact)

    preview = compute_delete_preview(database, provider="baidu", mirror_root=mirror)
    assert artifact.exists()
    first = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="baidu", digest=preview["digest"], mirror_root=mirror)
    assert first["artifact_results"][0]["status"] == "removed"
    assert not artifact.exists()
    # 幂等：同一 preview 再次确认直接返回既有结果
    second = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="baidu", digest=preview["digest"], mirror_root=mirror)
    assert second["status"] == "completed"


def test_orphan_work_playback_and_tracking_removed_but_mixed_kept(tmp_path):
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-orphan"], scan_id="scan-b", revision_id="rev-b")
    _seed_confirmed(database, root_id="root-115", provider="pan115", work_ids=["w-mixed"], scan_id="scan-c", revision_id="rev-c")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO playback_progress(episode_id, asset_id, work_id, position, duration, updated_at) VALUES ('ep-o', 'as-o', 'w-orphan', 1, 10, 'now')"
        )
        conn.execute(
            "INSERT INTO tracking_states(work_id, provider, provider_id, updated_at) VALUES ('w-orphan', 'bangumi', '1', 'now')"
        )
        conn.execute(
            "INSERT INTO playback_progress(episode_id, asset_id, work_id, position, duration, updated_at) VALUES ('ep-m', 'as-m', 'w-mixed', 1, 10, 'now')"
        )
        conn.execute(
            "INSERT INTO tracking_states(work_id, provider, provider_id, updated_at) VALUES ('w-mixed', 'bangumi', '2', 'now')"
        )

    preview = compute_delete_preview(database, provider="baidu")
    confirm_delete_preview(database, preview_id=preview["preview_id"], scope="baidu", digest=preview["digest"])
    with database.connect() as conn:
        orphan_playback = conn.execute("SELECT 1 FROM playback_progress WHERE work_id = 'w-orphan'").fetchone()
        orphan_tracking = conn.execute("SELECT 1 FROM tracking_states WHERE work_id = 'w-orphan'").fetchone()
        mixed_playback = conn.execute("SELECT 1 FROM playback_progress WHERE work_id = 'w-mixed'").fetchone()
        mixed_tracking = conn.execute("SELECT 1 FROM tracking_states WHERE work_id = 'w-mixed'").fetchone()
    assert orphan_playback is None
    assert orphan_tracking is None
    assert mixed_playback is not None
    assert mixed_tracking is not None


def test_maintenance_api_contract(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-a"])

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)
    preview_response = client.post("/api/v4/library-maintenance/delete-preview", json={"scope": "baidu"})
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["scope"] == "baidu"
    assert preview["work_count"] == 1

    confirm_response = client.post("/api/v4/library-maintenance/delete-confirm", json={
        "preview_id": preview["preview_id"],
        "scope": "baidu",
        "digest": preview["digest"],
    })
    assert confirm_response.status_code == 200, confirm_response.text
    assert confirm_response.json()["status"] == "completed"
    # 退役后来源卡不再返回
    cards = client.get("/api/v4/sources/libraries").json()["cards"]
    assert all(card["root_id"] != "root-baidu" for card in cards)

    # OpenList 不允许作为删除来源
    assert client.post("/api/v4/library-maintenance/delete-preview", json={"scope": "openlist"}).status_code == 422
