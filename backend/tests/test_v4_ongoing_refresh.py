"""Ongoing refresh reads explicit source purpose and protects persistent work."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import media_v4, tracking_v4
from app.media_v4.persistence.database import V4Database


@pytest.fixture
def context(tmp_path, monkeypatch):
    database = V4Database(tmp_path / "refresh.db")
    database.initialize()
    monkeypatch.setattr(tracking_v4, "get_database", lambda: database)
    monkeypatch.setattr(media_v4, "_database", database)
    app = FastAPI()
    app.include_router(tracking_v4.router)
    return database, TestClient(app)


def seed(database, root_id, mode="local", scope="ongoing", *, enabled=1, retired="", baseline=True):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, "
            "source_mode, content_scope, enabled, retired_at, created_at, updated_at) "
            "VALUES (?, 'local', 'fixture', ?, ?, ?, ?, ?, ?, 'now', 'now')",
            (root_id, f"/{root_id}", f"/{root_id}", mode, scope, enabled, retired),
        )
        conn.execute("INSERT INTO source_scans(scan_id, root_id, generation, status) VALUES (?, ?, 1, 'completed')",
                     (f"scan-{root_id}", root_id))
        if baseline:
            conn.execute(
                "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, "
                "graph_digest, created_at, confirmed_at) VALUES (?, ?, ?, 'v4', 'confirmed', '', 'now', 'now')",
                (f"rev-{root_id}", root_id, f"scan-{root_id}"),
            )


def test_sources_are_explicit_and_get_never_reads_credentials(context, monkeypatch):
    database, client = context
    for root_id, mode in [("local", "local"), ("txt", "tree_snapshot"), ("cloud", "tree_openlist")]:
        seed(database, root_id, mode)
    seed(database, "completed", scope="completed")
    seed(database, "disabled", enabled=0)
    seed(database, "retired", retired="now")
    seed(database, "no-baseline", baseline=False)
    def forbidden(*args, **kwargs):
        pytest.fail("read-only source listing accessed config or credentials")
    monkeypatch.setattr(tracking_v4, "load_config", forbidden)
    monkeypatch.setattr(tracking_v4, "resolve_openlist_credentials", forbidden)
    response = client.get("/api/v4/tracking/sources")
    assert response.status_code == 200
    sources = {item["root_id"]: item for item in response.json()["sources"]}
    assert set(sources) == {"local", "txt", "cloud"}
    assert sources["txt"]["status"] == "needs_new_txt"
    assert sources["local"]["status"] == "completed"
    assert all(item["task_id"] == "" for item in sources.values())


def test_scan_all_routes_three_sources_and_preserves_preview_choice(context, monkeypatch):
    database, client = context
    for root_id, mode in [("local", "local"), ("txt", "tree_snapshot"), ("cloud", "openlist_full")]:
        seed(database, root_id, mode)
    seed(database, "completed", scope="completed")
    calls = []
    monkeypatch.setattr(tracking_v4, "load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(media_v4, "_start_durable_local_scan", lambda request: calls.append(request) or {"scan_id": "local-new", "status": "queued"})
    def cloud(database, config, **kwargs):
        calls.append(kwargs)
        return {"task_id": "cloud-new", "status": "queued"}
    monkeypatch.setattr(tracking_v4, "_enqueue_root_incremental", cloud)
    response = client.post("/api/v4/tracking/scan-all", json={"include_scrape": False})
    assert response.status_code == 200, response.text
    sources = {item["root_id"]: item for item in response.json()["tasks"]}
    assert set(sources) == {"local", "txt", "cloud"}
    assert sources["txt"]["status"] == "needs_new_txt"
    assert len(calls) == 2
    local = next(call for call in calls if not isinstance(call, dict))
    remote = next(call for call in calls if isinstance(call, dict))
    assert local.target_root_id == "local" and local.content_scope == "ongoing"
    assert local.auto_update is False and local.revision_id
    assert remote["include_scrape"] is False and remote["revision_id"]
    assert local.revision_id != remote["revision_id"]


@pytest.mark.parametrize("state, expected, task", [("queued", "queued", "active"), ("running", "running", "active"), ("paused", "blocked", ""), ("cancelling", "running", ""), ("draft", "needs_confirmation", "")])
def test_existing_scan_or_draft_blocks_new_work(context, monkeypatch, state, expected, task):
    database, client = context
    seed(database, "local")
    with database.connect() as conn:
        if state == "draft":
            conn.execute("INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at) VALUES ('draft', 'local', 'scan-local', 'v4', 'draft', '', 'later')")
        else:
            conn.execute("INSERT INTO source_scans(scan_id, root_id, generation, status, cancel_requested) VALUES ('active', 'local', 2, ?, ?)",
                         ("running" if state == "cancelling" else state, int(state == "cancelling")))
    monkeypatch.setattr(media_v4, "_start_durable_local_scan", lambda _: pytest.fail("created duplicate scan"))
    result = client.post("/api/v4/tracking/scan-all", json={}).json()["tasks"][0]
    assert result["status"] == expected and result["task_id"] == task
    assert result["can_cancel"] is bool(task)


def test_txt_refresh_has_no_source_io(context, monkeypatch):
    database, client = context
    seed(database, "txt", "tree_snapshot")
    monkeypatch.setattr(tracking_v4, "load_config", lambda: pytest.fail("TXT accessed config"))
    monkeypatch.setattr(media_v4, "_start_durable_tree_scan", lambda _: pytest.fail("TXT reused archive"))
    monkeypatch.setattr(media_v4, "_start_durable_local_scan", lambda _: pytest.fail("TXT scanned mount"))
    result = client.post("/api/v4/tracking/scan-all", json={}).json()["tasks"][0]
    assert result["status"] == "needs_new_txt" and result["reason"]
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_scans").fetchone()[0] == 1


@pytest.mark.parametrize("status, expected", [("queued", "running"), ("running", "running"), ("failed", "failed"), ("succeeded", "completed"), ("cancelled", "cancelled")])
def test_confirmed_execution_is_distinct_from_cancellable_scan(context, monkeypatch, status, expected):
    database, client = context
    seed(database, "local")
    with database.connect() as conn:
        conn.execute("INSERT INTO jobs(job_id, job_type, revision_id, idempotency_key, status, last_error, created_at, updated_at) "
                     "VALUES ('job', 'scrape_work', 'rev-local', 'unique', ?, 'secret-token', 'now', 'now')", (status,))
    result = client.get("/api/v4/tracking/sources").json()["sources"][0]
    assert result["status"] == expected and result["task_id"] == "" and result["can_cancel"] is False
    assert "secret-token" not in json.dumps(result)
    if status in {"queued", "running"}:
        assert result["activity_kind"] == "execution"
        monkeypatch.setattr(media_v4, "_start_durable_local_scan", lambda _: pytest.fail("execution duplicated scan"))
        assert client.post("/api/v4/tracking/scan-all", json={}).json()["tasks"][0]["status"] == "running"


def test_local_real_registration_reuses_active_task_and_serializes_update(context, tmp_path, monkeypatch):
    database, client = context
    directory = tmp_path / "source"
    directory.mkdir()
    root_id, locator = media_v4._local_root_identity(str(directory))
    seed(database, root_id)
    with database.connect() as conn:
        conn.execute("UPDATE source_roots SET source_locator = ?, playback_locator = ? WHERE root_id = ?", (locator, locator, root_id))
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(openlist_server_url="", openlist_mount_root="", openlist_connections=[]))
    monkeypatch.setattr("app.media_v4.sources.source_scan_runner.get_source_scan_runner", lambda _: SimpleNamespace(wake=lambda: None))
    first = client.post("/api/v4/tracking/scan-all", json={}).json()["tasks"][0]
    second = client.post("/api/v4/tracking/scan-all", json={}).json()["tasks"][0]
    assert first["task_id"] == second["task_id"] and first["status"] == "queued"
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_scans").fetchone()[0] == 2
        request = json.loads(conn.execute("SELECT request_json FROM source_scan_requests WHERE scan_id = ?", (first["task_id"],)).fetchone()[0])
    assert request["content_scope"] == "ongoing" and request["auto_update"] is True
    assert request["revision_id"] != f"rev-{root_id}"


@pytest.mark.parametrize("account_changed", [False, True])
def test_remote_request_uses_its_own_connection_and_actual_registered_id(context, monkeypatch, account_changed):
    from app.api import openlist_v4
    from app.core.openlist_connections import OpenListConnectionConfig
    from app.integrations.openlist.providers import OpenListRouteConfig

    database, _client = context
    seed(database, "cloud", "tree_openlist")
    with database.connect() as conn:
        conn.execute("INSERT INTO source_scan_requests(scan_id, scan_kind, source_mode, request_json, created_at, updated_at) "
                     "VALUES ('scan-cloud', 'openlist_full', 'tree_openlist', ?, 'now', 'now')", (json.dumps({"connection_id": "right"}),))
    route = OpenListRouteConfig(route_id="anime", remote_prefix="/cloud", provider_id="pan115")
    config = SimpleNamespace(openlist_connections=[OpenListConnectionConfig("wrong", openlist_server_url="https://wrong.example.test"),
        OpenListConnectionConfig("right", openlist_server_url="https://right.example.test", openlist_routes=[route])])
    identities = []
    monkeypatch.setattr(tracking_v4, "resolve_openlist_credentials", lambda identity: identities.append(identity) or ("right-user", "fixture-password", "found"))
    monkeypatch.setattr(media_v4, "_source_openlist_root_id", lambda config, username, remote, **kwargs: "changed" if account_changed else "cloud")
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda _: [object()])
    monkeypatch.setattr(media_v4, "_connection_request_guard", lambda config, **kwargs: {"account_namespace": "right-namespace"})
    monkeypatch.setattr(openlist_v4, "_configured_routes", lambda config: config.openlist_routes)
    registrations = []
    monkeypatch.setattr(media_v4, "_register_durable_source_scan", lambda database, **kwargs: registrations.append(kwargs) or "existing-task")
    monkeypatch.setattr("app.media_v4.sources.source_scan_runner.get_source_scan_runner", lambda _: SimpleNamespace(wake=lambda: None))
    if account_changed:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as error:
            tracking_v4._enqueue_root_incremental(database, config, root_id="cloud", remote_root="/cloud")
        assert error.value.status_code == 409 and registrations == [] and identities == ["right"]
        return
    result = tracking_v4._enqueue_root_incremental(database, config, root_id="cloud", remote_root="/cloud", revision_id="new-revision")
    request = registrations[0]["scan"]["request"]
    assert identities == ["right"] and request["connection_id"] == "right"
    assert request["revision_id"] == "new-revision" and request["auto_update"] is True
    assert request["content_scope"] == "ongoing" and request["account_namespace"] == "right-namespace"
    assert result["task_id"] == "existing-task"
    assert "fixture-password" not in json.dumps(registrations)


def test_single_work_only_refreshes_confirmed_ongoing_membership(context, monkeypatch):
    from .test_v4_p006 import _seed_work

    database, client = context
    _seed_work(database, work_id="work", title="Fixture")
    seed(database, "unrelated", "tree_snapshot")
    monkeypatch.setattr(media_v4, "_start_durable_local_scan", lambda request: {"scan_id": "work-task"})
    assert client.post("/api/v4/tracking/work/scan", json={}).status_code == 409
    with database.connect() as conn:
        conn.execute("UPDATE source_roots SET content_scope = 'ongoing', source_mode = 'local' WHERE root_id = 'root-p'")
    result = client.post("/api/v4/tracking/work/scan", json={}).json()
    assert result["root_id"] == "root-p" and result["task_id"] == "work-task"
    assert len(result["tasks"]) == 1


def test_manual_and_scheduler_concurrent_triggers_share_one_registration(context, tmp_path, monkeypatch):
    from app.media_v4.tracking.refresh import refresh_all_sources

    database, _client = context
    directory = tmp_path / "local"
    directory.mkdir()
    root_id, locator = media_v4._local_root_identity(str(directory))
    seed(database, root_id)
    with database.connect() as conn:
        conn.execute("UPDATE source_roots SET source_locator = ? WHERE root_id = ?", (locator, root_id))
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(openlist_server_url="", openlist_mount_root="", openlist_connections=[]))
    monkeypatch.setattr("app.media_v4.sources.source_scan_runner.get_source_scan_runner", lambda _: SimpleNamespace(wake=lambda: None))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: refresh_all_sources(database), range(2)))
    assert results[0][0]["task_id"] == results[1][0]["task_id"]
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_scans").fetchone()[0] == 2


def test_unchanged_update_is_completed_without_replacing_confirmed_revision(context):
    database, client = context
    seed(database, "local")
    with database.connect() as conn:
        conn.execute("INSERT INTO source_scans(scan_id, root_id, generation, status) VALUES ('unchanged', 'local', 2, 'completed')")
        conn.execute("INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at) "
                     "VALUES ('no-change', 'local', 'unchanged', 'v4', 'superseded', '', 'later')")
    result = client.get("/api/v4/tracking/sources").json()["sources"][0]
    assert result["status"] == "completed" and result["latest_revision_id"] == "rev-local"
    assert result["draft_revision_id"] == "" and result["task_id"] == ""


def test_partly_cancelled_execution_does_not_report_completed(context):
    database, client = context
    seed(database, "local")
    with database.connect() as conn:
        for status in ("succeeded", "cancelled"):
            conn.execute("INSERT INTO jobs(job_id, job_type, revision_id, idempotency_key, status, created_at, updated_at) "
                         "VALUES (?, 'scrape_work', 'rev-local', ?, ?, 'now', 'now')", (status, status, status))
    result = client.get("/api/v4/tracking/sources").json()["sources"][0]
    assert result["status"] == "cancelled" and result["activity_kind"] == "execution"
    assert result["task_id"] == "" and result["can_cancel"] is False
