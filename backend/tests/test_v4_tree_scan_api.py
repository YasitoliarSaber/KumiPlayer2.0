"""durable TXT 导入兼容虚拟卷，输入错误不能伪装成后端失联。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.api import media_v4
from app.main import app
from app.media_v4.sources import input_archive, source_scan_runner
from app.media_v4.sources.input_archive import InputArchiveError, archive_is_intact
from fastapi.testclient import TestClient

from tests.source_disk_guard import guard_source_disk_io


@pytest.fixture
def tree_api(tmp_path, monkeypatch):
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Season 1/Show.S01E01.mkv\nShow/SP01.mkv\n", encoding="utf-16")
    config = SimpleNamespace(
        pan115_root="", baidu_root=r"Q:\offline-library", openlist_mount_root=r"Q:\offline-library",
        openlist_remote_root="/", openlist_routes=[], openlist_server_url="https://offline.invalid",
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "_select_openlist_config", lambda _: config)
    monkeypatch.setattr(media_v4, "_openlist_credentials", lambda _: ("fixture-account", "", "available"))
    monkeypatch.setattr(media_v4, "_connection_request_guard", lambda _: {})
    from app.api import openlist_v4

    monkeypatch.setattr(openlist_v4, "_configured_routes", lambda _: [])
    monkeypatch.setattr(media_v4, "provider_for_remote", lambda *_: ("route-fixture", "baidu"))
    database = media_v4.get_database()
    runner = source_scan_runner.SourceScanRunner(database)
    # API 登记和执行分开，测试同步运行同一生产 handler，不留后台线程。
    monkeypatch.setattr(runner, "wake", lambda: None)
    monkeypatch.setattr(source_scan_runner, "get_source_scan_runner", lambda _: runner)
    guard_source_disk_io(monkeypatch, [config.baidu_root])
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "offline-fixture-token")
    client = TestClient(app, raise_server_exceptions=False, headers={
        "Origin": "http://tauri.localhost", "X-KumiPlayer-Token": "offline-fixture-token",
    })
    return tree, database, runner, client


def _request(tree, source):
    return {"source": source, "tree_file": str(tree), "provider": "baidu", "root_path": "/"}


def _assert_no_scan(database):
    with database.connect() as conn:
        for table in ("source_roots", "source_scans", "source_scan_requests"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("source", ["tree", "hybrid"])
@pytest.mark.parametrize("unsupported", ["resolve", "stat"])
def test_durable_tree_scan_reads_virtual_volume_without_path_probes(tree_api, monkeypatch, source, unsupported):
    tree, database, runner, client = tree_api
    original = getattr(Path, unsupported)

    def volume_error(self, *args, **kwargs):
        if self == tree:
            raise OSError("WinError 1005: virtual volume does not support path probes")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, unsupported, volume_error)
    response = client.post("/api/v4/sources/scans", json=_request(tree, source))

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    scan_id = response.json()["scan_id"]
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM source_scan_requests WHERE scan_id=?", (scan_id,)).fetchone()
    assert json.loads(row["request_json"])["tree_file_path"] == os.path.abspath(tree)
    assert archive_is_intact(row["input_archive_path"], row["input_sha256"])
    assert tree.read_text(encoding="utf-16").count(".mkv") == 2

    task = runner.claim_next_scan()
    assert task is not None and task.scan_id == scan_id
    runner.run_scan(task)
    with database.connect() as conn:
        scan = conn.execute("SELECT status, error FROM source_scans WHERE scan_id=?", (scan_id,)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM source_evidence WHERE scan_id=?", (scan_id,)).fetchone()[0]
    assert scan["status"] == "completed", dict(scan)
    assert count == 2
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("source", ["tree", "hybrid"])
@pytest.mark.parametrize("content, message", [(None, "无法打开"), (b"", "为空"), (b"plain text", "不是可识别")])
def test_durable_tree_input_errors_are_actionable_json(tree_api, source, content, message):
    tree, database, _runner, client = tree_api
    if content is None:
        tree.unlink()
    else:
        tree.write_bytes(content)

    response = client.post("/api/v4/sources/scans", json=_request(tree, source))

    assert response.status_code == 400, response.text
    assert message in response.json()["detail"]
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert client.get("/api/health").status_code == 200
    _assert_no_scan(database)


@pytest.mark.parametrize("error, message", [
    (InputArchiveError("目录树文件过大，请拆分后重新导出"), "过大"),
    (OSError("fixture input disappeared or disk full"), "归档"),
])
def test_durable_tree_archive_failure_creates_no_scan(tree_api, monkeypatch, error, message):
    tree, database, _runner, client = tree_api

    def archive_failure(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(input_archive, "archive_tree_input", archive_failure)
    response = client.post("/api/v4/sources/scans", json=_request(tree, "tree"))

    assert response.status_code == 400, response.text
    assert message in response.json()["detail"]
    assert "fixture input" not in response.json()["detail"]
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert client.get("/api/health").status_code == 200
    _assert_no_scan(database)


def test_application_lifespan_starts_and_stops_on_an_isolated_empty_database():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
