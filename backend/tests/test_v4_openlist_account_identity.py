"""账户换源隔离及旧来源身份保全：临时库、纯词法定位，不访问网盘。"""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import media_v4, tracking_v4
from app.core.config import AppConfig
from app.core.openlist_connections import OpenListConnectionConfig
from app.integrations.openlist.providers import OpenListRouteConfig
from app.media_v4.persistence.database import V4Database
from app.media_v4.sources.scanner import openlist_root_id


def _old_root():
    return "root_" + hashlib.sha256(b"one\x1f/Anime").hexdigest()[:24]


@pytest.fixture
def database(tmp_path, monkeypatch):
    db = V4Database(tmp_path / "identity.db")
    db.initialize()
    monkeypatch.setattr(media_v4, "get_database", lambda: db)
    return db


def _seed(database, request, root_id=None):
    media_v4._register_durable_source_scan(
        database,
        root={"root_id": root_id or _old_root(), "provider": "other",
              "ingest_method": "openlist_scan", "source_locator": "/Anime",
              "source_mode": "openlist_full"},
        scan={"root_id": root_id or _old_root(), "scan_id": "scan-old",
              "scan_kind": "openlist_full", "source_mode": "openlist_full",
              "request": request},
    )


@pytest.mark.parametrize("server,user", [
    ("https://other.example.test", "alice"),
    ("https://example.test", "bob"),
    ("https://example.test", "Alice"),
    ("https://example.test/API", "alice"),
])
def test_changed_account_gets_a_different_root(server, user):
    original = openlist_root_id("https://example.test", "alice", "/Anime", connection_id="one")
    assert openlist_root_id(server, user, "/Anime", connection_id="one") != original


def test_endpoint_aliases_preserve_account_identity():
    assert openlist_root_id("HTTPS://EXAMPLE.TEST:443/dav/", "alice", "/Anime/", connection_id="one") == (
        openlist_root_id("https://example.test", "alice", "/Anime", connection_id="one")
    )


def test_legacy_root_contract_is_preserved():
    raw = "https://example.test\x1falice\x1f/Anime"
    expected = "root_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    assert openlist_root_id("https://example.test", "alice", "/Anime", connection_id="legacy") == expected


@pytest.mark.parametrize("saved,current,reuse", [
    ("old-fingerprint", "old-fingerprint", True),
    ("old-fingerprint", "new-fingerprint", False),
    ("", "", False),
])
def test_old_connection_root_requires_identity_proof(database, saved, current, reuse):
    from app.media_v4.sources.connection_identity import resolve_openlist_root_id

    _seed(database, {"connection_id": "one", "connection_fingerprint": saved})
    result = resolve_openlist_root_id(
        database, "https://example.test", "alice", "/Anime",
        connection_id="one", current_fingerprint=current,
    )
    assert (result == _old_root()) is reuse
    with database.connect() as conn:
        request = conn.execute("SELECT request_json FROM source_scan_requests").fetchone()[0]
    assert json.loads(request) == {"connection_id": "one", "connection_fingerprint": saved}


def test_account_proof_preserves_root_when_mount_mapping_changes(database):
    from app.media_v4.sources.connection_identity import (
        openlist_account_namespace,
        resolve_openlist_root_id,
    )

    namespace = openlist_account_namespace("https://example.test", "alice")
    _seed(database, {"connection_id": "one", "account_namespace": namespace,
                     "connection_fingerprint": "old-mount"})
    assert resolve_openlist_root_id(
        database, "https://example.test", "alice", "/Anime",
        connection_id="one", current_fingerprint="new-mount",
    ) == _old_root()
    assert resolve_openlist_root_id(
        database, "https://other.example.test", "alice", "/Anime",
        connection_id="one", current_fingerprint="old-mount",
    ) != _old_root()


def _selected_config(monkeypatch, server="https://example.test"):
    routes = [OpenListRouteConfig(route_id="route-one", remote_prefix="/Anime", provider_id="other")]
    config = AppConfig(openlist_connections=[OpenListConnectionConfig(
        "one", openlist_server_url=server, openlist_remote_root="/",
        openlist_mount_root="X:/", openlist_routes=routes,
    )])
    from app.core.openlist_connections import select_connection

    monkeypatch.setattr("app.core.config.resolve_openlist_credentials", lambda *_: ("alice", "unused", "found"))
    monkeypatch.setattr(media_v4, "_select_openlist_config", lambda *_: select_connection(config, "one"))
    monkeypatch.setattr(media_v4, "_openlist_credentials", lambda *_: ("alice", "unused", "found"))
    monkeypatch.setattr(tracking_v4, "resolve_openlist_credentials", lambda *_: ("alice", "unused", "found"))
    return config


def test_status_does_not_claim_old_account_baseline(database, monkeypatch):
    _seed(database, {"connection_id": "one", "connection_fingerprint": "old-account"})
    _selected_config(monkeypatch, "https://other.example.test")
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda root: [object()] if root == _old_root() else [])
    result = media_v4.openlist_status("/Anime", "one")
    assert result["root_id"] != _old_root()
    assert result["has_confirmed_baseline"] is False


def test_tracking_cannot_scan_old_root_with_new_account(database, monkeypatch):
    _seed(database, {"connection_id": "one", "connection_fingerprint": "old-account"})
    config = _selected_config(monkeypatch, "https://other.example.test")
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda _: [object()])
    monkeypatch.setattr("app.media_v4.sources.source_scan_runner.get_source_scan_runner",
                        lambda _: SimpleNamespace(wake=lambda: pytest.fail("must not enqueue")))
    with pytest.raises(HTTPException) as error:
        tracking_v4._enqueue_root_incremental(database, config, root_id=_old_root(), remote_root="/Anime")
    assert error.value.status_code == 409
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_scans").fetchone()[0] == 1


def test_registration_rejects_account_change_between_root_and_request(monkeypatch):
    config = _selected_config(monkeypatch)
    from app.core.openlist_connections import select_connection

    with pytest.raises(HTTPException) as error:
        media_v4._connection_request_guard(select_connection(config, "one"), username="previous-account")
    assert error.value.status_code == 409


def test_worker_checks_account_proof_even_when_mapping_fingerprint_matches(monkeypatch):
    from app.core.openlist_connections import select_connection
    from app.media_v4.sources.connection_identity import openlist_account_namespace
    from app.media_v4.sources.scan_handlers import _assert_connection_unchanged

    config = _selected_config(monkeypatch)
    monkeypatch.setattr("app.core.openlist_connections.connection_fingerprint", lambda _: "same")
    request = {"connection_fingerprint": "same", "account_namespace": openlist_account_namespace(
        "https://example.test", "previous-account",
    )}
    with pytest.raises(ValueError):
        _assert_connection_unchanged(select_connection(config, "one"), request)


def test_budget_resume_rejects_a_new_account_with_the_same_public_mask(database, monkeypatch):
    from app.api import openlist_v4
    from app.core.openlist_connections import select_connection
    from app.media_v4.sources import scan_handlers
    from app.media_v4.sources.connection_identity import openlist_account_namespace

    config = _selected_config(monkeypatch)
    selected = select_connection(config, "one")
    current = {"username": "alice"}
    accounts = []
    monkeypatch.setattr("app.core.config.resolve_openlist_credentials",
                        lambda *_: (current["username"], "unused", "found"))
    monkeypatch.setattr(openlist_v4, "resolve_openlist_credentials",
                        lambda *_: (current["username"], "unused", "found"))
    monkeypatch.setattr(openlist_v4, "_connection_config", lambda *_: selected)
    monkeypatch.setattr(openlist_v4, "get_openlist_client", lambda _server, user, _password, **_: (
        accounts.append(user) or SimpleNamespace(username=user)
    ))
    request = {"connection_id": "one", "remote_root": "/Anime", "account_namespace":
               openlist_account_namespace(selected.openlist_server_url, "alice")}
    _seed(database, request, root_id="offline-root")
    task = SimpleNamespace(request=request, root_id="offline-root", scan_id="scan-old")
    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.source_scan_runner import SourceScanDeferred

    scan_frontier.ensure_directories(database, scan_id=task.scan_id, remote_paths=["/Anime/Pending"])
    runtime = SimpleNamespace(cancellation_requested=lambda: False, persist_evidence_batch=lambda _: None,
                              report_progress=lambda **_: None)

    def scan(_client, **kwargs):
        kwargs["scan_stats"]["budget_exhausted"] = len(accounts) == 1
        return kwargs["scan_id"], []

    monkeypatch.setattr(media_v4, "scan_openlist_directory", scan)
    # 第一轮执行后换账号，第二轮从持久队列恢复时仍须拒绝新账户。
    with pytest.raises(SourceScanDeferred):
        scan_handlers.scan_openlist_full_source(database, task, runtime)
    current.update(username="axxxe")
    with pytest.raises(ValueError, match="账号或映射已改变"):
        scan_handlers.scan_openlist_full_source(database, task, runtime)
    assert accounts == ["alice"]
