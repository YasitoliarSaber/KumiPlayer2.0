"""HYB-2 验收：OpenList SourceRoot 的 TXT Zero-API Bootstrap。

必须证明：
- 10k-node fake TXT bootstrap：0 OpenList 网络请求；
- bootstrap → 后续 OpenList scan：root_id 不变；
- bootstrap → revisions：media unit 不重复；
- TXT 归档到数据目录（MediaTreeVersion），不指向用户临时文件；
- restart 后 snapshot job 仍可继续（payload 持久化）；
- 未配置 OpenList 时引导走纯 TXT 链路（400）；
- 全程无真实账号。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.media_presets.store import list_presets

# 115 目录树 TXT 内容（真实 115 导出格式，本地解析，0 网络）
TREE_TXT = """|——根目录
| |-动画
| | |-冰菓.2012
| | | |-冰菓.S01E01.深具传统的古籍研究社之重生.mkv
| | | |-冰菓.S01E02.神秘的古典文学部之.mkv
| | |-CLANNAD
| | | |-CLANNAD.S01E01.mkv
| |-新番
| | |-孤独摇滚
| | | |-孤独摇滚.S01E01.mkv
"""


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "hyb2.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    # 隔离数据目录（preset 归档/索引）
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    import app.media_presets.store as mstore
    import app.media_presets.service as mservice
    import app.core.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mstore, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mservice, "get_data_dir", lambda: data_dir)
    yield
    close_connection()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    """替换 OpenListClient，保证 config/test-connection/扫描全部离线。"""
    from tests.test_openlist_api import FakeOpenListClient

    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr(
        "app.integrations.openlist.connection.OpenListClient", FakeOpenListClient
    )
    yield


@pytest.fixture(autouse=True)
def openlist_config(client, tmp_path):
    """预置 OpenList 连接身份（server_url + username + mount root，0 网络）。

    走真实 /api/openlist/config 端点（与 test_openlist_api 一致），
    避免直接构造 AppConfig dataclass。
    """
    resp = client.post(
        "/api/openlist/config",
        json={
            "server_url": "http://127.0.0.1:5244",
            "remote_root": "/115网盘",
            "mount_root": str(tmp_path / "mount"),
            "username": "test-user",
            "password": "p@ssw0rd",
        },
    )
    assert resp.status_code == 200, resp.text
    resp2 = client.put(
        "/api/openlist/routes",
        json={
            "routes": [
                {
                    "route_id": "route-115-anime",
                    "label": "115",
                    "remote_prefix": "/115网盘/动画",
                    "provider_id": "pan115",
                    "enabled": True,
                }
            ]
        },
    )
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _tree_files(tmp_path: Path) -> Path:
    tree = tmp_path / "115目录树.txt"
    tree.write_text(TREE_TXT, encoding="utf-8")
    return tree


class TestBootstrapZeroNetwork:
    def test_bootstrap_never_constructs_openlist_client(self, client, tmp_path, monkeypatch):
        """TXT bootstrap 全程 0 OpenList 请求：客户端构造函数不得被调用。"""
        calls = []

        class ExplodingClient:
            def __init__(self, *args, **kwargs):
                calls.append("constructed")
                raise AssertionError("bootstrap 不应构造 OpenList 客户端")

        monkeypatch.setattr("app.api.openlist.OpenListClient", ExplodingClient)
        tree = _tree_files(tmp_path)
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/openlist/bootstrap-tree",
                data={
                    "remote_locator": "/115网盘/动画",
                    "local_mount_root": str(tmp_path / "mount"),
                    "import_family": "anime",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        assert calls == []

    def test_bootstrap_requires_openlist_identity(self, client, tmp_path, monkeypatch):
        """未配置 OpenList 身份 → 400，提示走纯 TXT 链路。"""
        import app.api.openlist as openlist_api
        from app.core.config import AppConfig

        monkeypatch.setattr(openlist_api, "load_config", lambda: AppConfig())
        tree = _tree_files(tmp_path)
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/openlist/bootstrap-tree",
                data={
                    "remote_locator": "/115网盘/动画",
                    "local_mount_root": str(tmp_path / "mount"),
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 400
        assert "115/百度目录树导入" in resp.json()["detail"]


class TestBootstrapIdentity:
    def test_bootstrap_creates_openlist_root_and_preset(self, client, tmp_path):
        """bootstrap 建立 canonical OpenList source + root + preset。"""
        from app.catalog import store as catalog_store

        tree = _tree_files(tmp_path)
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/openlist/bootstrap-tree",
                data={
                    "remote_locator": "/115网盘/动画",
                    "local_mount_root": str(tmp_path / "mount"),
                    "import_family": "anime",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["scan_channel"] == "snapshot_pan115"
        assert body["scan_mode"] == "full"
        assert body["root_id"]
        assert body["task_id"]

        root = catalog_store.get_source_root(body["root_id"])
        assert root is not None
        assert root.source_id.startswith("openlist-")
        assert root.remote_locator == "/115网盘/动画"

        presets = list_presets()
        assert any(p.source == "openlist" and p.remote_locator == "/115网盘/动画" for p in presets)

    def test_bootstrap_archive_stored_in_data_dir(self, client, tmp_path):
        """TXT 归档进 KumiPlayer 数据目录（MediaTreeVersion），不指向用户临时文件。"""
        tree = _tree_files(tmp_path)
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/openlist/bootstrap-tree",
                data={
                    "remote_locator": "/115网盘/动画",
                    "local_mount_root": str(tmp_path / "mount"),
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        presets = [p for p in list_presets() if p.source == "openlist"]
        assert len(presets) == 1
        assert presets[0].versions, "bootstrap 应产生 MediaTreeVersion"
        version = presets[0].versions[-1]
        assert version.input_type == "directory_tree"
        archive = tmp_path / "data" / version.archive_path
        assert archive.exists()
        assert version.sha256

    def test_rebootstrap_reuses_same_root_and_preset(self, client, tmp_path):
        """再次 bootstrap 同一远端定位 → 复用同一 root/preset，不制造第二套。"""
        tree = _tree_files(tmp_path)
        root_ids = set()
        preset_ids = set()
        for _ in range(2):
            with open(tree, "rb") as fh:
                resp = client.post(
                    "/api/openlist/bootstrap-tree",
                    data={
                        "remote_locator": "/115网盘/动画",
                        "local_mount_root": str(tmp_path / "mount"),
                    },
                    files={"tree_file": ("115目录树.txt", fh, "text/plain")},
                )
            assert resp.status_code == 200, resp.text
            root_ids.add(resp.json()["root_id"])
            preset_ids.add(resp.json()["preset_id"])
        assert len(root_ids) == 1
        assert len(preset_ids) == 1

    def test_bootstrap_then_openlist_rescan_same_root(self, client, tmp_path):
        """bootstrap 后切 OpenList 增量扫描：root_id 不变。"""
        from app.catalog import store as catalog_store
        from tests.test_openlist_api import FakeOpenListClient
        from tests.test_openlist_api import db_ready as _unused  # noqa: F401

        tree = _tree_files(tmp_path)
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/openlist/bootstrap-tree",
                data={
                    "remote_locator": "/115网盘/动画",
                    "local_mount_root": str(tmp_path / "mount"),
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        bootstrap_root_id = resp.json()["root_id"]

        # 用 OpenList rescan 端点（fake client 已在 test_openlist_api 的 fixture
        # 之外，这里显式替换为 Fake）——模拟后续 OpenList 增量通道
        import app.api.openlist as openlist_api

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(openlist_api, "OpenListClient", FakeOpenListClient)
        try:
            # 先建 preset（bootstrap 已建），再走 rescan
            from app.media_presets.store import get_preset

            preset = next(p for p in list_presets() if p.source == "openlist")
            resp2 = client.post(f"/api/openlist/presets/{preset.preset_id}/rescan")
        finally:
            monkeypatch.undo()
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["root_id"] == bootstrap_root_id


class TestBootstrapDurable:
    def test_bootstrap_job_payload_persists_input_path(self, client, tmp_path):
        """discovery job payload 持久化 TXT 归档路径与 scan_channel（restart 恢复）。"""
        from app.jobs import store as job_store

        tree = _tree_files(tmp_path)
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/openlist/bootstrap-tree",
                data={
                    "remote_locator": "/115网盘/动画",
                    "local_mount_root": str(tmp_path / "mount"),
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        job = job_store.get_job(resp.json()["task_id"])
        assert job is not None
        assert job.payload.get("scan_channel") == "snapshot_pan115"
        assert job.payload.get("scan_mode") == "full"
        input_path = job.payload.get("input_path") or ""
        assert input_path
        assert Path(input_path).exists(), "归档 TXT 必须存在（不指向用户临时文件）"

    def test_bootstrap_snapshot_scan_completes_offline(self, client, tmp_path):
        """bootstrap 的 discovery job 可离线完成（不触网）。"""
        from app.catalog import store as catalog_store
        from app.pipeline.discovery_handler import handle_discovery_scan
        from app.jobs import store as job_store

        tree = _tree_files(tmp_path)
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/openlist/bootstrap-tree",
                data={
                    "remote_locator": "/115网盘/动画",
                    "local_mount_root": str(tmp_path / "mount"),
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        # durable handler 同步执行（不依赖 worker 线程）：离线完成、
        # 不触网（scan_channel=snapshot_pan115 → TXT 本地解析）
        job = job_store.get_job(resp.json()["task_id"])
        assert job is not None
        result = handle_discovery_scan(job.payload)
        assert result.get("summary") is not None
        assert result["summary"].get("failed_count", 0) == 0
        assert result["summary"].get("plan_ready", 0) + result["summary"].get("needs_review", 0) > 0
        # 扫描后目录已提交（source_directories 非空）
        rows = catalog_store.get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ?",
            (resp.json()["root_id"],),
        ).fetchone()
        assert int(rows["c"]) > 0
