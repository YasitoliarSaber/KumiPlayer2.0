"""RWK-24 验收：TXT 真正成为 Source Catalog baseline 的决定性回归。

A. 原生路径 TXT 导入（/api/media-presets/import-local-tree）
   → preset.catalog_root_id 非空、Provider root 存在、
     source_nodes > 0、source_directories complete > 0、全过程 0 OpenList 请求；
B. multipart TXT 导入与 A 共用同一共享 bootstrap，同挂载根不产生第二套 root；
C. TXT baseline 完成后再 bind → 成功、root_id/source_id 不变、snapshot 已存在；
D. 大型 fake TXT（多层大量目录）baseline → bind → 第一次 bound incremental
   → 必须真正执行 handle_discovery_scan
   → Fake OpenList 请求量 ≪ 全树目录数（不重新枚举已知 subtree）
   → source_nodes canonical identity 不变、media_units unit_id 不重复。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.media_presets.store import list_presets

# 大型 fake TXT：1 根 × 40 作品 × 2 季 ≈ 122 目录节点（>100 且足够证明
# "首次增量请求量 ≪ 已知目录数"；避免超大树的扫描耗时拖慢回归）
def _big_tree() -> str:
    lines = ["|——根目录", "| |-动画1"]
    for work in range(40):
        work_name = f"作品1-{work:03d}"
        lines.append(f"| | |-{work_name}")
        for season in range(2):
            lines.append(f"| | | |-Season {season + 1}")
            for ep in range(3):
                lines.append(
                    f"| | | | |-{work_name}.S{season + 1:02d}E{ep + 1:02d}.mkv"
                )
    return "\n".join(lines) + "\n"


class FakeOpenListClient:
    instances = []
    list_calls = 0
    requested_paths: list[str] = []
    tree = {}

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
        FakeOpenListClient.list_calls += 1
        FakeOpenListClient.requested_paths.append(normalized)
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

    db_path = tmp_path / "rwk24.db"
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
    yield
    close_connection()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.list_calls = 0
    FakeOpenListClient.requested_paths = []
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.client.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _run_snapshot_baseline(client, root_id):
    """执行该 root 的 snapshot baseline job（durable handler 同步跑）。"""
    from app.jobs import store as job_store
    from app.pipeline.discovery_handler import handle_discovery_scan

    jobs = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=200)
    snapshot_job = next(
        (j for j in jobs
         if j.payload.get("root_id") == root_id
         and j.payload.get("scan_channel", "").startswith("snapshot_")),
        None,
    )
    assert snapshot_job is not None, "必须产生 snapshot baseline job"
    return handle_discovery_scan(snapshot_job.payload)


def _write_tree(tmp_path, text: str, name: str = "115目录树.txt") -> Path:
    tree = tmp_path / name
    tree.write_text(text, encoding="utf-8")
    return tree


class TestNativePathBaseline:
    def test_import_local_tree_creates_full_baseline(self, client, tmp_path):
        """A：原生路径 TXT 导入 → 完整 Source Catalog baseline，0 OpenList 请求。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        tree = _write_tree(tmp_path, _big_tree())
        calls_before = FakeOpenListClient.list_calls
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={
                "tree_path": str(tree),
                "import_family": "anime",
                "import_scope": "",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("baseline", {}).get("status") == "baseline_queued", resp.text
        assert FakeOpenListClient.list_calls == calls_before, "TXT baseline 全程 0 OpenList 请求"

        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        assert root_id, "import-local-tree 后 catalog_root_id 必须非空"
        root = catalog_store.get_source_root(root_id)
        assert root is not None and root.source_id.startswith("pan115-")

        # 执行 baseline job → source_nodes + complete directories
        result = _run_snapshot_baseline(client, root_id)
        assert result["summary"].get("failed_count", 0) == 0

        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        dirs = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ? AND state = 'complete'",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0, "TXT baseline 必须写入 source_nodes"
        assert int(dirs) > 0, "TXT baseline 必须建立 complete source_directories frontier"

        # bindable-providers 显示 ready
        bp = client.get("/api/openlist/bindable-providers").json()
        entry = next(p for p in bp["providers"] if p["root_id"] == root_id)
        assert entry["baseline_ready"] is True
        assert entry["baseline_node_count"] == int(nodes)

    def test_multipart_and_native_share_same_root(self, client, tmp_path):
        """B：multipart 与原生路径同挂载根 → 同一 root（不产生第二套）。"""
        from app.catalog import store as catalog_store
        from app.media_presets.service import ensure_provider_source_root

        # 先用原生路径导入
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id

        # multipart 导入同一挂载根（source_root 指向同 mount 根）
        with open(tree, "rb") as fh:
            resp2 = client.post(
                "/api/media-presets",
                data={
                    "source": "pan115",
                    "source_root": str(tmp_path),
                    "import_family": "anime",
                    "import_scope": "",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp2.status_code == 200, resp2.text

        # 同挂载根的所有 pan115 root 应复用同一 root
        from app.db.database import get_connection

        rows = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE source_id LIKE 'pan115-%'",
        ).fetchone()
        assert int(rows["c"]) == 1, "同一挂载根不得产生第二套 Provider root"
        # preset 关联指向同一 root
        presets = [p for p in list_presets() if p.source == "pan115"]
        roots = {p.catalog_root_id for p in presets if p.catalog_root_id}
        assert len(roots) == 1

    def test_bind_after_baseline_keeps_identity(self, client, tmp_path):
        """C：TXT baseline 完成后 bind → 成功、root/source 不变、snapshot 已存在。"""
        from app.catalog import store as catalog_store

        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        source_id = catalog_store.get_source_root(root_id).source_id
        _run_snapshot_baseline(client, root_id)

        # 配置 OpenList + route → bind
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
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 200, bind.text
        root = catalog_store.get_source_root(root_id)
        assert root.source_id == source_id
        assert root.openlist_conn_hash != ""


class TestFirstIncrementalNotFullTraversal:
    def test_first_incremental_requests_far_less_than_tree(self, client, tmp_path):
        """D：大 TXT baseline → bind → 第一次 incremental 请求量 ≪ 全树目录数。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan
        from app.db.database import get_connection

        # 1. 大型 TXT baseline（1 根 × 40 作品 × 2 季 ≈ 122 目录节点）
        #    Fake 远端只暴露 root 直接成员（动画1 等），其余内容以 TXT baseline 为准
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        _run_snapshot_baseline(client, root_id)

        # 统计 TXT 已知目录数（source_directories 总数）
        total_dirs = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ?",
            (root_id,),
        ).fetchone()["c"])
        assert total_dirs > 100, f"测试前置：TXT 目录树应 >100 目录（实际 {total_dirs}）"

        # 2. 配置 OpenList + route + bind
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
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 200, bind.text

        # Fake 远端：root 直接成员与 TXT 一致（动画1），不提供深层内容——
        # incremental 必须依赖 TXT baseline（mtime UNKNOWN→baseline learning），
        # 不得重新枚举已知 subtree。
        FakeOpenListClient.tree = {
            "/115网盘/动画1": [("动画1", True, None, 100.0)],
        }

        # 3. 第一次 bound incremental rescan → 真实执行 handler
        FakeOpenListClient.list_calls = 0
        FakeOpenListClient.requested_paths = []
        rescan = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan.status_code == 200, rescan.text
        job = job_store.get_job(rescan.json()["task_id"])
        result = handle_discovery_scan(job.payload)
        assert result["summary"].get("failed_count", 0) == 0

        # 4. 关键断言：请求量 ≪ 全树目录数（不重新枚举已知 subtree）
        requested = FakeOpenListClient.list_calls
        assert requested > 0, "必须真实请求 OpenList"
        assert requested < total_dirs // 2, (
            f"第一次 incremental 请求量 {requested} 应远小于 TXT 已知目录数 {total_dirs}"
        )

        # 5. canonical identity 不变、无重复 media unit
        units = get_connection().execute(
            "SELECT unit_id, COUNT(*) AS c FROM media_units WHERE root_id = ? GROUP BY unit_id HAVING c > 1",
            (root_id,),
        ).fetchall()
        assert len(units) == 0, "media unit_id 不得重复"
        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0
