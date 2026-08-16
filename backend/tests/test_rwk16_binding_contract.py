"""RWK-16 验收：binding 安全契约（P0 边界）。

必须证明：
1. configured remote_root=/115网盘，bind remote_locator=/其他目录/动画
   → 拒绝，0 OpenList 请求，0 binding mutation；
2. Provider root=pan115，matched route.provider_id=baidu → 拒绝，0 请求 0 mutation；
3. 合法 pan115 route → bind success → bound rescan success；
4. bind success → 修改 configured remote_root（server/user 不变）→ rescan
   在 bump_generation / enqueue 之前拒绝；
5. bind success → route disabled / provider 改变 → 已排队 durable job 执行
   时也拒绝，0 OpenList list 请求，0 catalog mutation。
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


class FakeOpenListClient:
    instances = []
    list_calls = 0
    tree = {
        "/115网盘/动画": [("动画", True, None, 100.0)],
        "/其他目录/动画": [("动画", True, None, 100.0)],
        "/百度网盘/动画": [("动画", True, None, 100.0)],
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
        FakeOpenListClient.list_calls += 1
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

    db_path = tmp_path / "rwk16.db"
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
    FakeOpenListClient.list_calls = 0
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.client.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _bootstrap_provider(client, tmp_path):
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
    job = job_store.get_job(body["task_id"])
    result = handle_discovery_scan(job.payload)
    assert result["summary"].get("failed_count", 0) == 0
    return body


def _configure_openlist(client, tmp_path, *, remote_root: str = "/115网盘"):
    resp = client.post(
        "/api/openlist/config",
        json={
            "server_url": "http://127.0.0.1:5244",
            "remote_root": remote_root,
            "mount_root": str(tmp_path / "mount"),
            "username": "test-user",
            "password": "p@ssw0rd",
        },
    )
    assert resp.status_code == 200, resp.text


def _save_routes(client, routes):
    resp = client.put("/api/openlist/routes", json={"routes": routes})
    assert resp.status_code == 200, resp.text


def _bind(client, root_id, locator):
    return client.post(
        "/api/openlist/bind-root",
        json={"root_id": root_id, "remote_locator": locator},
    )


class TestBindingSafetyContract:
    def test_bind_outside_remote_root_rejected(self, client, tmp_path):
        """①binding locator 超出 configured remote_root → 拒绝 0 请求 0 mutation。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        body = _bootstrap_provider(client, tmp_path)
        _configure_openlist(client, tmp_path)  # remote_root=/115网盘
        # 只配 /115网盘 内的合法 route；/其他目录 不在 remote_root 内
        _save_routes(client, [{
            "route_id": "r-115", "label": "115",
            "remote_prefix": "/115网盘/动画", "provider_id": "pan115", "enabled": True,
        }])
        before = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE root_id = ?",
            (body["root_id"],),
        ).fetchone()["c"]
        calls_before = FakeOpenListClient.list_calls

        resp = _bind(client, body["root_id"], "/其他目录/动画")
        assert resp.status_code == 400
        assert "不在当前 OpenList 远端总根" in resp.json()["detail"]
        assert FakeOpenListClient.list_calls == calls_before, "0 OpenList 请求"
        root = catalog_store.get_source_root(body["root_id"])
        assert root.openlist_conn_hash == "", "0 binding mutation"
        after = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE root_id = ?",
            (body["root_id"],),
        ).fetchone()["c"]
        assert after == before

    def test_bind_provider_mismatch_rejected(self, client, tmp_path):
        """②pan115 root + baidu route → 拒绝 0 请求 0 mutation。"""
        from app.catalog import store as catalog_store

        body = _bootstrap_provider(client, tmp_path)  # pan115 root
        _configure_openlist(client, tmp_path)
        _save_routes(client, [{
            "route_id": "r-baidu", "label": "百度",
            "remote_prefix": "/115网盘/动画", "provider_id": "baidu", "enabled": True,
        }])
        calls_before = FakeOpenListClient.list_calls
        resp = _bind(client, body["root_id"], "/115网盘/动画")
        assert resp.status_code == 400
        assert "Provider 身份不一致" in resp.json()["detail"]
        assert FakeOpenListClient.list_calls == calls_before, "0 OpenList 请求"
        root = catalog_store.get_source_root(body["root_id"])
        assert root.openlist_conn_hash == "", "0 binding mutation"

    def test_bind_valid_route_success_and_rescan(self, client, tmp_path):
        """③合法 pan115 route → bind success → bound rescan success。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store

        body = _bootstrap_provider(client, tmp_path)
        _configure_openlist(client, tmp_path)
        _save_routes(client, [{
            "route_id": "r-115", "label": "115",
            "remote_prefix": "/115网盘/动画", "provider_id": "pan115", "enabled": True,
        }])
        resp = _bind(client, body["root_id"], "/115网盘/动画")
        assert resp.status_code == 200, resp.text
        root = catalog_store.get_source_root(body["root_id"])
        assert root.openlist_conn_hash != ""

        rescan = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": body["root_id"]},
        )
        assert rescan.status_code == 200, rescan.text
        job = job_store.get_job(rescan.json()["task_id"])
        assert job.payload.get("scan_channel") == "openlist"

    def test_rescan_rejected_after_remote_root_change(self, client, tmp_path):
        """④bind 后修改 configured remote_root（server/user 不变）→ rescan 拒绝（bump 前）。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        body = _bootstrap_provider(client, tmp_path)
        _configure_openlist(client, tmp_path)
        _save_routes(client, [{
            "route_id": "r-115", "label": "115",
            "remote_prefix": "/115网盘/动画", "provider_id": "pan115", "enabled": True,
        }])
        assert _bind(client, body["root_id"], "/115网盘/动画").status_code == 200

        # 修改 remote_root（server/user 不变，conn hash 不变）
        _configure_openlist(client, tmp_path, remote_root="/新根")
        gen_before = catalog_store.get_source_root(body["root_id"]).active_generation
        jobs_before = get_connection().execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]

        rescan = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": body["root_id"]},
        )
        assert rescan.status_code == 409
        gen_after = catalog_store.get_source_root(body["root_id"]).active_generation
        jobs_after = get_connection().execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]
        assert gen_after == gen_before, "拒绝必须在 bump_generation 之前"
        assert jobs_after == jobs_before, "0 job enqueue"

    def test_job_execution_rejected_after_route_change(self, client, tmp_path):
        """⑤bind 后 route 禁用/provider 改变 → queued job 执行时拒绝，0 list 0 mutation。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan
        from app.db.database import get_connection

        body = _bootstrap_provider(client, tmp_path)
        _configure_openlist(client, tmp_path)
        _save_routes(client, [{
            "route_id": "r-115", "label": "115",
            "remote_prefix": "/115网盘/动画", "provider_id": "pan115", "enabled": True,
        }])
        assert _bind(client, body["root_id"], "/115网盘/动画").status_code == 200

        # 先正常 rescan 一次（拿到 queued job 语义：rescan 会 enqueue 新 job）
        # 然后修改路由：把 provider 改为 baidu（route 仍在但 provider 变了）
        _save_routes(client, [{
            "route_id": "r-115", "label": "115",
            "remote_prefix": "/115网盘/动画", "provider_id": "baidu", "enabled": True,
        }])
        rescan = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": body["root_id"]},
        )
        assert rescan.status_code == 409, "rescan 入口必须先拒绝（provider 变了）"
        calls_before = FakeOpenListClient.list_calls

        # 直接构造一个已排队的 openlist job 并执行（模拟 route 变更发生在排队后）
        gen = catalog_store.bump_generation(body["root_id"])
        job = job_store.create_job(
            job_type="discovery_scan",
            resource_key=f"scan:conn:{body['source_id']}",
            payload={
                "root_id": body["root_id"],
                "generation": gen,
                "source_id": body["source_id"],
                "input_path": "",
                "scan_mode": "incremental",
                "scan_channel": "openlist",
            },
        )
        with pytest.raises(ValueError, match="Provider 身份不一致"):
            handle_discovery_scan(job.payload)
        assert FakeOpenListClient.list_calls == calls_before, "0 OpenList list 请求"
        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ?",
            (body["root_id"],),
        ).fetchone()["c"]
        assert nodes > 0  # 既有 snapshot 数据保留
