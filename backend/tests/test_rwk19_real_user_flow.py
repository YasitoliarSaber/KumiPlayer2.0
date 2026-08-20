"""RWK-19 验收：现有 TXT 导入路径接通 Provider Source Catalog 的真实产品流。

必须证明（真实用户流程，不经隐藏 bootstrap API）：
1. 通过现有 115/百度 TXT 导入入口（media-presets）建库；
2. preset.catalog_root_id 非空，对应 Provider SourceRoot 存在（pan115-{hash}）；
3. restart（重读）后关联保持；
4. 配置 OpenList + route → bind（免手填 root_id 的 UI 数据面：preset 已关联）；
5. bound rescan → root/source/media_units 不分裂。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.media_presets.store import list_presets

TREE_TXT = """|——根目录
| |-动画
| | |-冰菓.2012
| | | |-冰菓.S01E01.mkv
"""


class FakeOpenListClient:
    instances = []
    tree = {
        "/115网盘/动画": [("动画", True, None, 100.0)],
    }

    def __init__(self, server_url, username, password, **kwargs):
        FakeOpenListClient.instances.append(self)
        self.server_url = server_url
        self.username = username
        self.password = password

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        from app.integrations.openlist.client import join_remote_path, normalize_remote_path
        from app.integrations.openlist.models import OpenListEntry

        normalized = normalize_remote_path(path)
        items = FakeOpenListClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        return type("Page", (), {"entries": entries, "total": len(entries)})()


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "rwk19.db"
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


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.client.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


class TestRealUserFlow:
    def test_media_presets_txt_import_creates_provider_root(self, client, tmp_path):
        """现有 TXT 导入入口 → Provider SourceRoot + preset.catalog_root_id 关联。"""
        from app.catalog import store as catalog_store

        tree = tmp_path / "115目录树.txt"
        tree.write_text(TREE_TXT, encoding="utf-8")
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/media-presets",
                data={
                    "source": "pan115",
                    "source_root": str(tmp_path / "mount"),
                    "import_family": "anime",
                    "import_scope": "",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text

        presets = [p for p in list_presets() if p.source == "pan115"]
        assert len(presets) >= 1
        preset = presets[0]
        assert preset.catalog_root_id, "TXT 导入后 preset.catalog_root_id 必须非空"
        root = catalog_store.get_source_root(preset.catalog_root_id)
        assert root is not None, "对应 Provider SourceRoot 必须存在"
        assert root.source_id.startswith("pan115-")

    def test_restart_keeps_association_then_bind_and_rescan(self, client, tmp_path):
        """restart 后关联保持 → bind → rescan → root/source/media_units 不分裂。"""
        from app.catalog import store as catalog_store
        from app.db.database import close_connection, init_db
        import app.db.database as db_mod

        # 1. 现有 TXT 导入
        tree = tmp_path / "115目录树.txt"
        tree.write_text(TREE_TXT, encoding="utf-8")
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/media-presets",
                data={
                    "source": "pan115",
                    "source_root": str(tmp_path / "mount"),
                    "import_family": "anime",
                    "import_scope": "",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        source_id = catalog_store.get_source_root(root_id).source_id
        assert source_id.startswith("pan115-")

        # 2. 模拟重启：重读 preset 与 root
        close_connection()
        if hasattr(db_mod._local, "connection"):
            db_mod._local.connection = None
        init_db()
        presets = [p for p in list_presets() if p.source == "pan115"]
        assert presets and presets[0].catalog_root_id == root_id, "restart 后关联保持"
        root = catalog_store.get_source_root(root_id)
        assert root is not None and root.source_id == source_id

        # 2.5 从 bindable-providers 确认 baseline 状态（TXT 导入即同步完成 baseline → ready）
        bp_resp = client.get("/api/openlist/bindable-providers")
        providers0 = bp_resp.json()["providers"]
        entry0 = next(p for p in providers0 if p["root_id"] == root_id)
        assert entry0["baseline_ready"] is True, "TXT 导入同步完成后必须可绑定"

        # 3. 配置 OpenList + route → bind（preset 已关联 root_id，UI 免手填）
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
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        # 执行 TXT baseline discovery（从 job store 找到该 root 的 snapshot job）
        # RWK-34：TXT 导入即同步完成 baseline（无 queued job、无需手动执行）

        # baseline 就绪后 bindable-providers 显示 ready
        bp_resp2 = client.get("/api/openlist/bindable-providers")
        entry1 = next(p for p in bp_resp2.json()["providers"] if p["root_id"] == root_id)
        assert entry1["baseline_ready"] is True, "TXT baseline 完成后必须可绑定"
        assert entry1["baseline_node_count"] > 0

        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画"},
        )
        assert bind.status_code == 200, bind.text

        # 4. bound rescan：root/source 不分裂
        rescan = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan.status_code == 200, rescan.text
        assert rescan.json()["source_id"] == source_id
        assert rescan.json()["root_id"] == root_id
        # 没有创建第二套 source/root
        from app.db.database import get_connection

        roots = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE source_id = ?",
            (source_id,),
        ).fetchone()["c"]
        assert int(roots) == 1


class TestProviderIdempotency:
    def test_trailing_slash_same_provider_root(self, client, tmp_path):
        """尾斜杠/大小写差异不产生第二套 Provider source（RWK-17 修复）。"""
        from app.catalog import store as catalog_store
        from app.media_presets.service import ensure_provider_source_root

        root_a = ensure_provider_source_root(
            provider="pan115",
            local_mount_root=str(tmp_path / "mount"),
            import_family="anime",
        )
        root_b = ensure_provider_source_root(
            provider="pan115",
            local_mount_root=str(tmp_path / "mount") + "\\",
            import_family="anime",
        )
        assert root_a == root_b, "尾斜杠差异必须复用同一 root"
        source_a = catalog_store.get_source_root(root_a).source_id
        sources = catalog_store.get_connection().execute(
            "SELECT COUNT(*) AS c FROM sources WHERE source_id = ?", (source_a,)
        ).fetchone()["c"]
        assert int(sources) == 1

    def test_bindable_providers_lists_association(self, client, tmp_path):
        """bindable-providers 返回 TXT 导入建立的 Provider 来源（免手填 root_id）。"""
        from app.catalog import store as catalog_store

        tree = tmp_path / "115目录树.txt"
        tree.write_text(TREE_TXT, encoding="utf-8")
        with open(tree, "rb") as fh:
            resp = client.post(
                "/api/media-presets",
                data={
                    "source": "pan115",
                    "source_root": str(tmp_path / "mount"),
                    "import_family": "anime",
                    "import_scope": "",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")

        resp = client.get("/api/openlist/bindable-providers")
        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert any(
            p["root_id"] == preset.catalog_root_id
            and p["provider"] == "pan115"
            and p["bound"] is False
            for p in providers
        ), "bindable-providers 必须包含 TXT 导入建立的 Provider 来源"
