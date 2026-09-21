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


def test_source_cleanup_releases_orphan_identity_but_keeps_shared_identity(tmp_path):
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-b", provider="baidu", work_ids=["old", "shared"], scan_id="scan-b", revision_id="rev-b")
    _seed_confirmed(database, root_id="root-p", provider="pan115", work_ids=["shared"], scan_id="scan-p", revision_id="rev-p")
    with database.connect() as conn:
        for work_id, provider_id in [("old", "100"), ("shared", "200")]:
            conn.execute("INSERT INTO provider_bindings(work_id,provider,media_type,provider_id) VALUES (?,'tmdb','tv',?)", (work_id, provider_id))
            conn.execute("INSERT INTO work_aliases(work_id,normalized_title,language,alias_type) VALUES (?,?,'','source')", (work_id, work_id))
            conn.execute("INSERT INTO work_source_bindings(work_id,root_id,structural_key,confidence,binding_source) VALUES (?,'root-b',?,'high','resolver')", (work_id, work_id))
    preview = compute_delete_preview(database, provider="baidu")
    confirm_delete_preview(database, preview_id=preview["preview_id"], scope="baidu", digest=preview["digest"])
    with database.connect() as conn:
        assert conn.execute("SELECT status FROM works WHERE work_id='old'").fetchone()[0] != "active"
        assert conn.execute("SELECT 1 FROM works WHERE identity_key='ik-old'").fetchone() is None
        for table in ("provider_bindings", "work_aliases", "work_source_bindings"):
            assert conn.execute(f"SELECT 1 FROM {table} WHERE work_id='old'").fetchone() is None
        assert conn.execute("SELECT provider_id FROM provider_bindings WHERE work_id='shared'").fetchone()[0] == "200"
        assert conn.execute("SELECT status FROM works WHERE work_id='shared'").fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM revision_bindings WHERE revision_id='rev-b'").fetchone()[0] == 2


def test_legacy_retired_source_is_not_reused_by_preview(tmp_path):
    from app.media_v4.domain.models import ResolvedMediaGraph, ResolvedWork
    from app.media_v4.revisions.service import V4RevisionService, _existing_work_matches

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-old", provider="baidu", work_ids=["old"])
    with database.connect() as conn:
        conn.execute("UPDATE works SET identity_key='title:wrong::tv', preferred_title='Wrong' WHERE work_id='old'")
        conn.execute("UPDATE source_roots SET retired_at='2026-09-10'")
        conn.execute("INSERT INTO provider_bindings(work_id,provider,media_type,provider_id) VALUES ('old','tmdb','tv','100')")
        work = ResolvedWork(work_key="title:wrong::tv", preferred_title="Wrong", media_type="tv", year=None)
        assert _existing_work_matches(conn, work) == []
        graph = ResolvedMediaGraph(works=(work,))
        assert V4RevisionService(database)._existing_bindings_by_key(graph, conn=conn)[work.work_key] == []
        assert conn.execute("SELECT status FROM works WHERE work_id='old'").fetchone()[0] == "active", "预览不能改写历史数据"


def test_deleted_work_does_not_keep_identity_authority(tmp_path):
    from app.media_v4.domain.models import ResolvedWork
    from app.media_v4.maintenance.service import compute_work_delete_preview, confirm_work_delete
    from app.media_v4.revisions.service import _existing_work_matches

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-delete", provider="local", work_ids=["old"])
    with database.connect() as conn:
        conn.execute("INSERT INTO provider_bindings(work_id,provider,media_type,provider_id) VALUES ('old','tmdb','tv','100')")
    preview = compute_work_delete_preview(database, work_id="old", mirror_root=tmp_path / "mirror")
    confirm_work_delete(database, work_id="old", preview_id=preview["preview_id"], digest=preview["digest"], mirror_root=tmp_path / "mirror")
    with database.connect() as conn:
        work = ResolvedWork(work_key="ik-old", preferred_title="作品old", media_type="tv", year=None)
        assert _existing_work_matches(conn, work) == []
        assert conn.execute("SELECT 1 FROM provider_bindings WHERE work_id='old'").fetchone() is None


@pytest.mark.parametrize("job_status", ["queued", "running"])
@pytest.mark.parametrize("job_created_before_preview", [False, True])
def test_work_delete_cannot_release_identity_while_job_can_still_write(
    tmp_path, job_status, job_created_before_preview
):
    from app.media_v4.maintenance.service import compute_work_delete_preview, confirm_work_delete

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-delete", provider="local", work_ids=["old"])
    if job_created_before_preview:
        with database.connect() as conn:
            conn.execute("INSERT INTO jobs(job_id,job_type,revision_id,work_id,idempotency_key,status,created_at,updated_at) VALUES ('job','scrape_work','rev-s','old','job',?,'now','now')", (job_status,))
    preview = compute_work_delete_preview(database, work_id="old")
    assert preview["blocked"] is job_created_before_preview
    assert preview["blocked_job_count"] == int(job_created_before_preview)
    if not job_created_before_preview:
        with database.connect() as conn:
            conn.execute("INSERT INTO jobs(job_id,job_type,revision_id,work_id,idempotency_key,status,created_at,updated_at) VALUES ('job','scrape_work','rev-s','old','job',?,'now','now')", (job_status,))
    with pytest.raises(ValueError, match="后台任务"):
        confirm_work_delete(database, work_id="old", preview_id=preview["preview_id"], digest=preview["digest"])
    with database.connect() as conn:
        assert conn.execute("SELECT status FROM works WHERE work_id='old'").fetchone()[0] == "active"


def test_deleted_work_scrape_job_cannot_be_requeued(tmp_path):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.maintenance.service import compute_work_delete_preview, confirm_work_delete

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-delete", provider="local", work_ids=["old"])
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id,job_type,revision_id,work_id,idempotency_key,status,created_at,updated_at) "
            "VALUES ('job','scrape_work','rev-s','old','job','failed','now','now')"
        )
    preview = compute_work_delete_preview(database, work_id="old")
    confirm_work_delete(
        database,
        work_id="old",
        preview_id=preview["preview_id"],
        digest=preview["digest"],
    )

    with pytest.raises(RuntimeError, match="已退出媒体库"):
        V4ScrapeService(database).requeue_work("rev-s", "old")
    with database.connect() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE job_id='job'").fetchone()[0] == "failed"


def test_inactive_historical_binding_cannot_block_reimport(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.revisions.service import V4RevisionService

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-old", provider="local", work_ids=["old"])
    with database.connect() as conn:
        conn.execute(
            "UPDATE works SET identity_key='retired:old', preferred_title='错误历史作品', "
            "status='superseded' WHERE work_id='old'"
        )
        conn.execute(
            "INSERT INTO provider_bindings(work_id,provider,media_type,provider_id) "
            "VALUES ('old','tmdb','tv','100')"
        )
    evidence = SourceEvidence(
        evidence_id="fresh",
        scan_id="scan-fresh",
        root_id="root-fresh",
        source_key="Correct/01.mkv",
        relative_path="Correct/01.mkv",
        entry_kind="video",
        provider="local",
        source_locator="C:/fixture/Correct/01.mkv",
        playback_locator="C:/fixture/Correct/01.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-fresh",
        evidence_id="fresh",
        parser_version="fixture",
        work_title="Correct",
        title_candidates=("Correct",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        tmdb_hint_id=100,
        tmdb_hint_type="tv",
        confidence="high",
    )
    service = V4RevisionService(database)

    graph = service.create_draft("rev-fresh", [(evidence, facts)])
    assert graph.issues == ()
    service.confirm("rev-fresh")

    with database.connect() as conn:
        owners = {
            str(row["work_id"])
            for row in conn.execute(
                "SELECT work_id FROM provider_bindings "
                "WHERE provider='tmdb' AND media_type='tv' AND provider_id='100'"
            ).fetchall()
        }
        assert "old" in owners
        assert any(work_id != "old" for work_id in owners)
        assert conn.execute(
            "SELECT 1 FROM provider_bindings WHERE work_id='old'"
        ).fetchone() is not None


@pytest.mark.parametrize("legacy_cleanup", [False, True, "hidden"])
@pytest.mark.parametrize("new_provider_id", [100, 200])
@pytest.mark.parametrize("same_root", [True, False])
def test_reimport_after_cleanup_does_not_resurrect_wrong_work(tmp_path, legacy_cleanup, new_provider_id, same_root):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview
    from app.media_v4.revisions.service import V4RevisionService

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-old", provider="local", work_ids=["old"])
    with database.connect() as conn:
        conn.execute("UPDATE works SET identity_key='title:correct::tv', preferred_title='Wrong historical title' WHERE work_id='old'")
        conn.execute("INSERT INTO work_aliases(work_id,normalized_title,language,alias_type) VALUES ('old','correct','','source')")
        conn.execute("INSERT INTO provider_bindings(work_id,provider,media_type,provider_id) VALUES ('old','tmdb','tv','100')")
    if legacy_cleanup == "hidden":
        with database.connect() as conn:
            conn.execute("INSERT INTO work_overrides(work_id,override_json,created_at,updated_at) VALUES ('old',?, 'now','now')", (json.dumps({"hidden": True}),))
    elif legacy_cleanup:
        with database.connect() as conn:
            conn.execute("UPDATE source_roots SET retired_at='2026-09-10'")
    else:
        preview = compute_delete_preview(database, provider="local")
        confirm_delete_preview(database, preview_id=preview["preview_id"], scope="local", digest=preview["digest"])
    evidence = SourceEvidence(evidence_id="new-evidence", scan_id="scan-new", root_id="root-old" if same_root else "root-new", source_key="Correct/01.mkv", relative_path="Correct/01.mkv", entry_kind="video", provider="local", source_locator="C:/fixture/Correct/01.mkv", playback_locator="C:/fixture/Correct/01.mkv")
    facts = ParsedFacts(parsed_fact_id="new-facts", evidence_id=evidence.evidence_id, parser_version="fixture", work_title="Correct", title_candidates=("Correct",), media_type="tv", group_type="season", season_candidate=1, episode_candidate=1, tmdb_hint_id=new_provider_id, tmdb_hint_type="tv", confidence="high")
    service = V4RevisionService(database)
    graph = service.create_draft("rev-new", [(evidence, facts)])
    assert graph.issues == ()
    service.confirm("rev-new")
    with database.connect() as conn:
        row = conn.execute("SELECT w.work_id,w.preferred_title FROM revision_bindings rb JOIN works w ON w.work_id=rb.work_id WHERE rb.revision_id='rev-new'").fetchone()
        assert row["work_id"] != "old"
        assert row["preferred_title"] == "Correct"
        assert conn.execute("SELECT provider_id FROM provider_bindings WHERE work_id=? AND provider='tmdb'", (row["work_id"],)).fetchone()[0] == str(new_provider_id)
        assert conn.execute("SELECT status FROM works WHERE work_id='old'").fetchone()[0] != "active"
        assert conn.execute("SELECT work_id FROM revision_bindings WHERE revision_id='rev-s'").fetchone()[0] == "old"


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


def test_all_scope_skips_unconfirmed_roots_and_keeps_confirmed_cleanup_usable(tmp_path):
    """全选范围不能被尚未确认、也没有媒体库记录的来源根阻断。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_confirmed(database, root_id="root-baidu", provider="baidu", work_ids=["w-baidu"])
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, created_at, updated_at) "
            "VALUES ('root-draft', 'pan115', 'openlist', '/draft', '', 'now', 'now')"
        )

    preview = compute_delete_preview(database, provider="all")

    assert preview["root_count"] == 1
    assert preview["skipped_root_count"] == 1
    assert preview["skipped_provider_counts"] == [{"provider": "pan115", "count": 1}]
    assert all("root-draft" not in warning for warning in preview["warnings"])

    result = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="all", digest=preview["digest"])

    assert result["status"] == "completed"
    with database.connect() as conn:
        retired = {
            str(row["root_id"]): str(row["retired_at"] or "")
            for row in conn.execute("SELECT root_id, retired_at FROM source_roots").fetchall()
        }
    assert retired["root-baidu"]
    assert retired["root-draft"] == ""


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
