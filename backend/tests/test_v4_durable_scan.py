"""P-004 durable SourceScan 合同：可离开、可查询、可取消的大扫描。"""

from __future__ import annotations

import time


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("durable scan 未在超时内到达目标状态")


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "durable.db")
    database.initialize()
    return database


def _seed_root(database, root_id: str = "root-durable") -> None:
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, created_at, updated_at) "
            "VALUES (?, 'pan115', 'openlist_scan', '/Anime', 'K:\\\\Anime', 'now', 'now')",
            (root_id,),
        )


def _evidence(root_id: str, scan_id: str, relative: str):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id=scan_id,
        provider="pan115",
        ingest_method="openlist_api",
        relative_path=relative,
        source_key=relative,
        source_locator=relative,
        playback_locator=f"K:\\\\Anime\\\\{relative}",
    ))


def test_durable_full_scan_reaches_completed_and_persists_evidence(tmp_path):
    from app.media_v4.sources.durable_scan import create_durable_scan, get_durable_scan

    database = _fresh_database(tmp_path)
    _seed_root(database)
    scan_id = "scan-durable-1"

    def fake_full_scan():
        return scan_id, [_evidence("root-durable", scan_id, "Show/Show.S01E01.mkv")]

    create_durable_scan(database, scan_id=scan_id, root_id="root-durable", kind="full", scan_fn=fake_full_scan)
    _wait_until(lambda: get_durable_scan(database, scan_id)["status"] == "completed")

    result = get_durable_scan(database, scan_id)
    assert result["root_id"] == "root-durable"
    assert result["status"] == "completed"
    assert len(result["entries"]) == 1
    assert result["entries"][0]["relative_path"] == "Show/Show.S01E01.mkv"
    # 证据挂在该 scan 下，preview 可以按 scan_id 读取。
    from app.media_v4.persistence.repositories import V4Repository

    stored = V4Repository(database).list_scan_evidence(scan_id)
    assert [item.relative_path for item in stored] == ["Show/Show.S01E01.mkv"]


def test_durable_full_scan_failure_writes_explicit_terminal_state(tmp_path):
    from app.media_v4.sources.durable_scan import create_durable_scan, get_durable_scan

    database = _fresh_database(tmp_path)
    _seed_root(database)
    scan_id = "scan-durable-fail"

    def failing_scan():
        raise RuntimeError("远端连接超时")

    create_durable_scan(database, scan_id=scan_id, root_id="root-durable", kind="full", scan_fn=failing_scan)
    _wait_until(lambda: get_durable_scan(database, scan_id)["status"] == "failed")

    result = get_durable_scan(database, scan_id)
    assert result["status"] == "failed"
    assert "远端连接超时" in result["error"]
    assert result["entries"] == []


def test_durable_full_scan_cancel_discards_result(tmp_path):
    from app.media_v4.sources.durable_scan import cancel_durable_scan, create_durable_scan, get_durable_scan

    database = _fresh_database(tmp_path)
    _seed_root(database)
    scan_id = "scan-durable-cancel"
    started = {}

    def slow_scan():
        started["go"] = True
        time.sleep(3)
        return scan_id, [_evidence("root-durable", scan_id, "Show/Show.S01E01.mkv")]

    create_durable_scan(database, scan_id=scan_id, root_id="root-durable", kind="full", scan_fn=slow_scan)
    _wait_until(lambda: started.get("go"))
    assert cancel_durable_scan(scan_id) is True
    _wait_until(lambda: get_durable_scan(database, scan_id)["status"] == "cancelled")

    result = get_durable_scan(database, scan_id)
    assert result["status"] == "cancelled"
    assert result["entries"] == []
    from app.media_v4.persistence.repositories import V4Repository

    assert V4Repository(database).list_scan_evidence(scan_id) == []


def test_durable_scan_api_contract(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.api.media_v4 import openlist_root_id
    from app.integrations.openlist.providers import OpenListRouteConfig

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    config = SimpleNamespaceType(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))

    def fake_full_scan(_client, **kwargs):
        from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

        root_id = kwargs["root_id"]
        evidence = [to_source_evidence(SourceEntry(
            root_id=root_id,
            scan_id="scan-inner",
            provider="pan115",
            ingest_method="openlist_api",
            relative_path="Show/Show.S01E01.mkv",
            source_key="Show/Show.S01E01.mkv",
            source_locator="Show/Show.S01E01.mkv",
            playback_locator="X:\\OpenList\\Anime\\Show\\Show.S01E01.mkv",
        ))]
        return "scan-inner", evidence

    monkeypatch.setattr(media_v4, "scan_openlist_directory", fake_full_scan)
    from app.api import openlist_v4

    monkeypatch.setattr(openlist_v4, "_client", lambda _config: object())

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    response = client.post("/api/v4/sources/scans", json={
        "source": "openlist",
        "root_path": "/Anime",
        "provider": "pan115",
        "scan_mode": "full",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "running"
    scan_id = body["scan_id"]

    _wait_until(lambda: client.get(f"/api/v4/sources/scans/{scan_id}").json()["status"] == "completed")
    result = client.get(f"/api/v4/sources/scans/{scan_id}").json()
    assert result["root_id"] == openlist_root_id("https://openlist.example.test", "kumi", "/Anime")
    assert len(result["entries"]) == 1
    assert result["entries"][0]["relative_path"] == "Show/Show.S01E01.mkv"

    # 取消已完成扫描是幂等终态。
    cancelled = client.post(f"/api/v4/sources/scans/{scan_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "completed"

    assert client.get("/api/v4/sources/scans/scan-missing").status_code == 404


def test_durable_incremental_without_baseline_is_rejected(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.integrations.openlist.providers import OpenListRouteConfig

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    config = SimpleNamespaceType(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda _root_id: [])

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)
    response = client.post("/api/v4/sources/scans", json={
        "source": "openlist",
        "root_path": "/Anime",
        "provider": "pan115",
        "scan_mode": "incremental",
    })
    assert response.status_code == 409
    assert "已确认基线" in response.json()["detail"]


class SimpleNamespaceType:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
