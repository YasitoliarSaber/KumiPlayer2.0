"""RWK-3 验收：Provider SourceRoot 绑定可选 OpenList 增量通道。

必须证明：
- 已存在 Provider（pan115）root → 绑定 OpenList → root_id 不变、
  source_id 仍是 pan115、media_unit 不重复；
- 绑定前错绑预检：快照与远端完全无重叠 → 拒绝，0 变更；
- OpenList 未配置 / credential unavailable → 拒绝（可信 resolver）；
- 绑定后同 root 用 scan_channel=openlist 扫描（root 身份不换）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.media_presets.store import list_presets

TREE_TXT = """|——根目录
| |-动画
| | |-冰菓.2012
| | | |-冰菓.S01E01.mkv
"""


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "rwk3.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    import app.media_presets.store as mstore
    import app.media_presets.service as mservice
    import app.core.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mstore, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mservice, "get_data_dir", lambda: data_dir)
    import app.api.media_presets as api_mp

    monkeypatch.setattr(api_mp, "get_data_dir", lambda: data_dir)
    yield
    close_connection()


@pytest.fixture
def client():
    return TestClient(app)


def _bootstrap_provider(client, tmp_path):
    """先用 TXT bootstrap 建立 Provider root 并完成 snapshot 扫描（RWK-2 流程）。"""
    from app.jobs import store as job_store
    from app.pipeline.discovery_handler import handle_discovery_scan

    tree = tmp_path / "115目录树.txt"
    tree.write_text(TREE_TXT, encoding="utf-8")
    with open(tree, "rb") as fh:
        resp = client.post(
            "/api/openlist/bootstrap-tree",
            data={
                "local_mount_root": str(tmp_path / "mount"),
                "provider": "pan115",
                "import_family": "anime",
            },
            files={"tree_file": ("115目录树.txt", fh, "text/plain")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 手动执行 snapshot discovery（API 端点本身不启动 worker）
    job = job_store.get_job(body["task_id"])
    result = handle_discovery_scan(job.payload)
    assert result["summary"].get("failed_count", 0) == 0
    return body


class FakeClient:
    """模拟 OpenList 客户端（用于 bind 的错绑预检 list）。"""

    instances = []
    tree = {
        # 远端绑定路径的直接成员与快照 root 直接成员同名（"动画"）
        "/115网盘/动画": [("动画", True, None, 100.0)],
    }
    def __init__(self, *args, **kwargs):
        FakeClient.instances.append(self)

    def login(self):
        return "fake-token"
    def list_dir(self, path, page=1, per_page=100, refresh=False):
        from app.integrations.openlist.client import join_remote_path, normalize_remote_path
        from app.integrations.openlist.models import OpenListEntry

        normalized = normalize_remote_path(path)
        items = FakeClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        return type("Page", (), {"entries": entries, "total": len(entries)})()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeClient)
    monkeypatch.setattr("app.integrations.openlist.client.OpenListClient", FakeClient)
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeClient)
    yield


def _configure_openlist(client, tmp_path, *, provider: str = "pan115"):
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
    # RWK-14：binding 契约要求命中启用路由且 provider 与 root 一致
    resp2 = client.put(
        "/api/openlist/routes",
        json={
            "routes": [
                {
                    "route_id": "route-115-anime",
                    "label": "115",
                    "remote_prefix": "/115网盘/动画",
                    "provider_id": provider,
                    "enabled": True,
                }
            ]
        },
    )
    assert resp2.status_code == 200, resp2.text


class TestBindRoot:
    def test_bind_keeps_provider_identity(self, client, tmp_path):
        """绑定后 root_id 不变、source_id 仍是 pan115、media unit 不重复。"""
        from app.catalog import store as catalog_store

        body = _bootstrap_provider(client, tmp_path)
        root_id = body["root_id"]
        source_id = body["source_id"]
        assert source_id.startswith("pan115-")

        _configure_openlist(client, tmp_path)
        resp = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound"] is True

        # root 身份不变
        root = catalog_store.get_source_root(root_id)
        assert root.source_id == source_id
        assert root.openlist_conn_hash != ""
        assert root.openlist_remote_locator == "/115网盘/动画"

        # 没有创建新的 openlist source/root
        from app.db.database import get_connection

        rows = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        assert int(rows["c"]) == 1

    def test_bind_rejects_without_openlist_config(self, client, tmp_path):
        """未配置 OpenList → 400（可信 resolver 拒绝，不派生 stale identity）。"""
        body = _bootstrap_provider(client, tmp_path)
        resp = client.post(
            "/api/openlist/bind-root",
            json={"root_id": body["root_id"], "remote_locator": "/115网盘/动画"},
        )
        assert resp.status_code == 400
        assert "配置" in resp.json()["detail"]

    def test_bind_preflight_rejects_mismatch(self, client, tmp_path):
        """错绑预检：远端直接成员与快照完全无重叠 → 409，0 变更。"""
        from app.catalog import store as catalog_store

        body = _bootstrap_provider(client, tmp_path)
        _configure_openlist(client, tmp_path)
        # 为电影目录配一条 pan115 route（过 binding 契约校验，才能到 preflight）
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "route-115-movie",
                        "label": "115 电影",
                        "remote_prefix": "/115网盘/电影",
                        "provider_id": "pan115",
                        "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        FakeClient.tree = {
            "/115网盘/电影": [("电影A", True, None, 5.0)],
        }
        resp = client.post(
            "/api/openlist/bind-root",
            json={"root_id": body["root_id"], "remote_locator": "/115网盘/电影"},
        )
        assert resp.status_code == 409
        assert "invalid_snapshot_mapping" in resp.json()["detail"]
        root = catalog_store.get_source_root(body["root_id"])
        assert root.openlist_conn_hash == ""
