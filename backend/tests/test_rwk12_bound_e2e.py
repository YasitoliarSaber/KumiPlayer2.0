"""RWK-12 验收：Bound Provider Root 真实增量扫描端到端闭环。

必须真正走完整链路（产品入口 → binding → 路径映射 → 持久化）：
1. 纯 TXT、无 OpenList → bootstrap + snapshot 完成（Provider root，无 binding）；
2. 模拟重启/重新读取（重新 init_db 同一文件 + 从 store 重读 root）；
3. 配置 OpenList → bind-root（binding 写入）；
4. bound-roots/rescan → durable job → handle_discovery_scan 真实执行；
5. Fake OpenList 收到的物理路径 = binding locator（/115网盘/动画/...）；
6. 数据库 node canonical path 仍是原 Provider namespace（/K:/... 或原值）；
7. root_id 不变、source_id 仍为 pan115、media_unit 不重复；
8. 绑定后切换 OpenList server/user → 扫描拒绝且 0 catalog mutation。
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
| | | |-冰菓.S01E02.mkv
| | |-CLANNAD
| | | |-CLANNAD.S01E01.mkv
"""


class FakeOpenListClient:
    """记录收到的物理请求路径；返回远端条目（remote_path 以绑定根为前缀）。"""

    instances = []
    requested_paths: list[str] = []
    tree = {
        "/115网盘/动画": [("动画", True, None, 100.0)],
        "/115网盘/动画/动画": [("冰菓.2012", True, None, 100.0), ("CLANNAD", True, None, 200.0)],
        "/115网盘/动画/动画/冰菓.2012": [
            ("冰菓.S01E01.mkv", False, 100, 1.0),
            ("冰菓.S01E02.mkv", False, 200, 2.0),
        ],
        "/115网盘/动画/动画/CLANNAD": [("CLANNAD.S01E01.mkv", False, 300, 3.0)],
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

    db_path = tmp_path / "rwk12.db"
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
    FakeOpenListClient.requested_paths = []
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.client.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _bootstrap(client, tmp_path):
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
    # 手动执行 snapshot discovery（API 不启动 worker）
    from app.jobs import store as job_store
    from app.pipeline.discovery_handler import handle_discovery_scan

    job = job_store.get_job(body["task_id"])
    result = handle_discovery_scan(job.payload)
    assert result["summary"].get("failed_count", 0) == 0
    return body


def _configure_openlist(client, tmp_path):
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


class TestBoundEndToEnd:
    def test_full_chain_keeps_identity_and_maps_paths(self, client, tmp_path):
        """完整链路：TXT→snapshot→重启→bind→rescan→物理路径=binding→canonical 不变。"""
        from app.catalog import store as catalog_store
        from app.import_plan import revision_store
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan

        # 1. 纯 TXT bootstrap（无 OpenList）
        body = _bootstrap(client, tmp_path)
        root_id = body["root_id"]
        source_id = body["source_id"]
        canonical_locator = catalog_store.get_source_root(root_id).remote_locator
        assert source_id.startswith("pan115-")

        # snapshot 后 node 的 canonical path（Provider namespace）
        nodes_before = {
            row["remote_path"]
            for row in catalog_store.get_connection().execute(
                "SELECT remote_path FROM source_nodes WHERE root_id = ? AND tombstone = ''",
                (root_id,),
            ).fetchall()
        }
        assert nodes_before, "snapshot 必须产生 source_nodes"
        assert all(p.startswith(canonical_locator) for p in nodes_before)
        assert all("115网盘" not in p for p in nodes_before), "TXT 阶段不应出现 OpenList 物理路径"

        # 2. 模拟重启：关闭连接重新 init（同一 db 文件），重读 root
        from app.db.database import close_connection, init_db
        import app.db.database as db_mod

        close_connection()
        if hasattr(db_mod._local, "connection"):
            db_mod._local.connection = None
        init_db()
        root_reloaded = catalog_store.get_source_root(root_id)
        assert root_reloaded is not None, "restart 后 root 必须可重读"
        assert root_reloaded.source_id == source_id

        # preset 与 root 持久关联（RWK-10）
        presets = [p for p in list_presets() if p.source == "pan115"]
        assert len(presets) == 1
        assert presets[0].catalog_root_id == root_id, "bootstrap 后 preset.catalog_root_id 必须持久关联"

        # 3. 配置 OpenList 并绑定
        _configure_openlist(client, tmp_path)
        bind_resp = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画"},
        )
        assert bind_resp.status_code == 200, bind_resp.text
        root_bound = catalog_store.get_source_root(root_id)
        assert root_bound.openlist_conn_hash != ""
        assert root_bound.openlist_remote_locator == "/115网盘/动画"
        assert root_bound.source_id == source_id

        # 4. bound durable rescan（真实产品入口）
        # 清空 config probe / bind preflight 产生的请求，只验证 rescan 后的物理请求
        FakeOpenListClient.requested_paths = []
        revisions_before = len(revision_store.list_revisions(root_id))
        rescan_resp = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan_resp.status_code == 200, rescan_resp.text
        assert rescan_resp.json()["scan_channel"] == "openlist"
        assert rescan_resp.json()["root_id"] == root_id
        job = job_store.get_job(rescan_resp.json()["task_id"])
        assert job.payload.get("scan_channel") == "openlist"
        result = handle_discovery_scan(job.payload)
        assert result["summary"].get("failed_count", 0) == 0

        # 5. Fake OpenList 收到的物理路径 = binding locator
        assert FakeOpenListClient.requested_paths, "必须真实请求 OpenList"
        assert all(
            p == "/115网盘/动画" or p.startswith("/115网盘/动画/")
            for p in FakeOpenListClient.requested_paths
        ), f"物理请求必须严格用 binding locator（/115网盘/动画），实际: {FakeOpenListClient.requested_paths}"
        # root 的 canonical locator 是本地 POSIX 路径；物理请求绝不含它
        assert all(
            str(tmp_path).replace("\\", "/") not in p
            for p in FakeOpenListClient.requested_paths
        )

        # 6. 库内 node canonical path 仍是 Provider namespace（不产生第二套）
        nodes_after = {
            row["remote_path"]
            for row in catalog_store.get_connection().execute(
                "SELECT remote_path FROM source_nodes WHERE root_id = ? AND tombstone = ''",
                (root_id,),
            ).fetchall()
        }
        assert nodes_after == nodes_before, (
            "OpenList 增量后 node identity 必须与 TXT snapshot 完全一致"
        )
        assert all(p.startswith(canonical_locator) for p in nodes_after)

        # 7. root_id 不变、source_id 仍 pan115、media unit 不重复
        assert catalog_store.get_source_root(root_id).source_id == source_id
        # 增量内容与 snapshot 相同 → revision 不重复（幂等）；
        # 物理请求已发生（第 5 步断言），node 集合不变证明无重复 unit
        assert len(revision_store.list_revisions(root_id)) == revisions_before

    def test_connection_change_rejects_scan_zero_mutation(self, client, tmp_path):
        """绑定后切换 OpenList server/user → rescan 拒绝（409），0 catalog mutation。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store
        from app.db.database import get_connection

        body = _bootstrap(client, tmp_path)
        root_id = body["root_id"]
        _configure_openlist(client, tmp_path)
        bind_resp = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画"},
        )
        assert bind_resp.status_code == 200, bind_resp.text

        # 切换 OpenList 连接（不同 server_url → 不同 conn hash）
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.2:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text

        before_nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ?", (root_id,)
        ).fetchone()["c"]
        before_jobs = get_connection().execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE payload LIKE ?",
            (f"%{root_id}%",),
        ).fetchone()["c"]

        rescan_resp = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan_resp.status_code == 409
        assert "连接已变更" in rescan_resp.json()["detail"]

        after_nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ?", (root_id,)
        ).fetchone()["c"]
        after_jobs = get_connection().execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE payload LIKE ?",
            (f"%{root_id}%",),
        ).fetchone()["c"]
        assert after_nodes == before_nodes, "0 catalog mutation"
        assert after_jobs == before_jobs, "0 job mutation"

    def test_rescan_without_binding_rejected(self, client, tmp_path):
        """未绑定 → bound rescan 拒绝（400）。"""
        body = _bootstrap(client, tmp_path)
        resp = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": body["root_id"]},
        )
        assert resp.status_code == 400
        assert "尚未绑定" in resp.json()["detail"]
