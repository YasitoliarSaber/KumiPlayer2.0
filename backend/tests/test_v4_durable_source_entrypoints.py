"""所有导入入口都必须先创建 durable SourceScan，不能占用 HTTP 请求扫描大库。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "durable-entrypoints.db")
    database.initialize()
    return database


def test_local_durable_entrypoint_returns_before_the_directory_scan_finishes(tmp_path, monkeypatch):
    """本地大目录扫描在后台进行，创建任务接口不得等待文件枚举。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    started = threading.Event()
    release = threading.Event()
    expected_root_id, _locator = media_v4._local_root_identity("D:/Media")

    def slow_local_scan(_root_path, **_kwargs):
        started.set()
        assert release.wait(3)
        evidence = to_source_evidence(SourceEntry(
            root_id=expected_root_id, scan_id="scan-inner", provider="local", ingest_method="local_scan",
            relative_path="Show/Show.S01E01.mkv", source_key="Show/Show.S01E01.mkv",
            source_locator="D:/Media/Show/Show.S01E01.mkv", playback_locator="D:/Media/Show/Show.S01E01.mkv",
        ))
        return expected_root_id, "scan-inner", [evidence]

    monkeypatch.setattr(media_v4, "scan_local_directory", slow_local_scan)
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        openlist_server_url="https://openlist.example.test", openlist_remote_root="/",
        pan115_root="", baidu_root="", openlist_mount_root="X:/OpenList", openlist_routes=[],
    ))
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    started_at = time.monotonic()
    response = client.post("/api/v4/sources/scans", json={"source": "local", "root_path": "D:/Media"})
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200, response.text
    scan_id = response.json()["scan_id"]
    assert response.json()["status"] == "running"
    assert elapsed < 0.5
    assert started.wait(1)
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if get_durable_scan(database, scan_id)["status"] != "running":
            break
        time.sleep(0.02)
    completed = get_durable_scan(database, scan_id)
    assert completed["status"] == "completed"
    assert [entry["relative_path"] for entry in completed["entries"]] == ["Show/Show.S01E01.mkv"]


def test_tree_durable_entrypoint_defers_txt_reading_until_after_task_creation(tmp_path, monkeypatch):
    """目录树读取也不能卡在 HTTP 请求内，完成后证据必须归属该 durable scan。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    mounted_root = tmp_path / "mounted"
    video = mounted_root / "Anime" / "Show" / "Show.S01E01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    tree_file = tmp_path / "Anime.txt"
    tree_file.write_text("Anime\n└── Show\n    └── Show.S01E01.mkv\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    real_read = media_v4.read_directory_tree_text

    def slow_tree_read(path):
        started.set()
        assert release.wait(3)
        return real_read(path)

    monkeypatch.setattr(media_v4, "read_directory_tree_text", slow_tree_read)
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="", baidu_root=str(mounted_root), openlist_mount_root="", openlist_routes=[],
    ))
    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    response = client.post("/api/v4/sources/scans", json={
        "source": "tree", "tree_file": str(tree_file), "provider": "baidu",
    })

    assert response.status_code == 200, response.text
    scan_id = response.json()["scan_id"]
    assert response.json()["status"] == "running"
    assert started.wait(1)
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and get_durable_scan(database, scan_id)["status"] == "running":
        time.sleep(0.02)
    result = get_durable_scan(database, scan_id)
    assert result["status"] == "completed"
    assert [entry["relative_path"] for entry in result["entries"]] == ["Show/Show.S01E01.mkv"]
