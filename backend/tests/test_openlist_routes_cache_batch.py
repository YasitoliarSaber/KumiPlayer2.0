"""OLIST-02 新功能聚焦测试。

覆盖：提供商路由保存/校验/最长前缀匹配、本地路径安全映射、名称只做建议、
懒加载浏览不递归、本地持久缓存（命中/过期/SWR/强制刷新/失败保留/容量淘汰）、
refresh=false 与显式 refresh=true 请求合同、有界预取不递归、
多选父子去重、批量导入串行/部分失败隔离/取消、新字段元数据、
旧 source=openlist 预设兼容回填与 rescan。
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import (
    OpenListEntry,
    OpenListPermissionError,
)
from app.main import app

# 与 test_openlist_api.py 相同的树：remote_root=/夸克网盘，mount=tmp/quark
REMOTE_ROOT = "/夸克网盘"
TREE = {
    "/夸克网盘": [("动画", True, None, None), ("说明.txt", False, 5, 1)],
    "/夸克网盘/动画": [("冰菓", True, None, None), ("真人", True, None, None)],
    "/夸克网盘/动画/冰菓": [
        ("冰菓 - 01.mkv", False, 100, 1700000000),
        ("冰菓 - 02.mkv", False, 200, 1700000001),
    ],
    "/夸克网盘/动画/真人": [("真人剧 - 01.mkv", False, 300, 1700000002)],
}


class FakeOpenListClient:
    instances = []
    login_user = ""
    permission_path = ""
    tree = TREE

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.calls: list[tuple[str, bool]] = []
        FakeOpenListClient.instances.append(self)

    def login(self):
        if self.username == FakeOpenListClient.login_user:
            raise PermissionError("auth")
        return "fake-token"

    def _list_dir_default(self, path, page=1, per_page=100, refresh=False):
        normalized = normalize_remote_path(path)
        self.calls.append((normalized, bool(refresh)))
        if normalized == FakeOpenListClient.permission_path:
            raise OpenListPermissionError()
        items = FakeOpenListClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name,
                is_dir=is_dir,
                size=size,
                modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        start = (page - 1) * per_page
        return type("Page", (), {"entries": entries[start:start + per_page], "total": len(entries)})()

    list_dir = _list_dir_default


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.login_user = ""
    FakeOpenListClient.permission_path = ""
    FakeOpenListClient.tree = TREE
    FakeOpenListClient.list_dir = FakeOpenListClient._list_dir_default
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _save_config(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/openlist/config",
        json={
            "server_url": "https://ol.example.com:5244",
            "remote_root": REMOTE_ROOT,
            "mount_root": str(tmp_path / "quark"),
            "username": "quark-user",
            "password": "p@ssw0rd",
            "cache_ttl_minutes": 1440,
            "prefetch_limit": 12,
        },
    )
    assert resp.status_code == 200, resp.text


def _make_local_mount(tmp_path: Path) -> Path:
    root = tmp_path / "quark"
    (root / "动画" / "冰菓").mkdir(parents=True)
    (root / "动画" / "冰菓" / "冰菓 - 01.mkv").write_bytes(b"1")
    (root / "动画" / "冰菓" / "冰菓 - 02.mkv").write_bytes(b"2")
    (root / "动画" / "真人").mkdir(parents=True)
    (root / "动画" / "真人" / "真人剧 - 01.mkv").write_bytes(b"3")
    return root


def _save_routes(
    client: TestClient,
    routes: list[dict] | None = None,
) -> None:
    if routes is None:
        routes = [
            {"route_id": "r1", "label": "动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True}
        ]
    resp = client.put("/api/openlist/routes", json={"routes": routes})
    assert resp.status_code == 200, resp.text


def _wait_task(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    record = {}
    while time.time() < deadline:
        record = client.get(f"/api/tasks/{task_id}").json()
        if record["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"任务 {task_id} 超时未完成")
    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    deadline = time.time() + 5
    while time.time() < deadline:
        with manager._lock:
            released = task_id not in manager._running.values()
        if released:
            break
        time.sleep(0.05)
    return record


def _wait_cache_fresh(tmp_path: Path, conn_key: str, remote_path: str, timeout: float = 5.0) -> dict | None:
    from app.integrations.openlist.cache import read_cache

    deadline = time.time() + timeout
    while time.time() < deadline:
        cached = read_cache(conn_key, remote_path)
        if cached is not None and cached["fresh"]:
            return cached
        time.sleep(0.05)
    return None


# ============================================================
# 提供商路由
# ============================================================

class TestRoutes:
    def test_route_mapping_115_baidu_quark_to_mount(self, client, tmp_path):
        """/115、/百度、/夸克 通过连接级挂载根映射到 K:\\115 等本地路径。"""
        from app.integrations.openlist.providers import derive_local_path

        assert derive_local_path("K:\\", "/", "/115/动画/冰菓") == "K:\\115\\动画\\冰菓"
        assert derive_local_path("K:\\", "/", "/百度/电影/某片") == "K:\\百度\\电影\\某片"
        assert derive_local_path("K:\\", "/", "/夸克/动画") == "K:\\夸克\\动画"
        assert derive_local_path("K:\\", "/", "/") == "K:\\"

    def test_route_mapping_respects_remote_root(self, client, tmp_path):
        from app.integrations.openlist.providers import derive_local_path

        assert derive_local_path("K:\\", "/115", "/115/动画/冰菓") == "K:\\动画\\冰菓"
        with pytest.raises(ValueError):
            derive_local_path("K:\\", "/115", "/百度/动画")

    def test_save_and_read_multi_provider_routes(self, client, tmp_path):
        """单次 OpenList 连接下保存多提供商路由，读取时零凭据泄露。"""
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": "/",
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200
        _save_routes(
            client,
            [
                {"route_id": "r1", "label": "115 网盘", "remote_prefix": "/115", "provider_id": "pan115", "enabled": True},
                {"route_id": "r2", "label": "百度网盘", "remote_prefix": "/百度", "provider_id": "baidu", "enabled": True},
                {"route_id": "r3", "label": "夸克网盘", "remote_prefix": "/夸克", "provider_id": "quark", "enabled": True},
            ],
        )
        body = client.get("/api/openlist/routes").json()
        assert len(body["routes"]) == 3
        by_provider = {item["provider_id"]: item for item in body["routes"]}
        assert by_provider["pan115"]["remote_prefix"] == "/115"
        assert by_provider["baidu"]["remote_prefix"] == "/百度"
        assert by_provider["quark"]["remote_prefix"] == "/夸克"
        # 本地路径由连接级挂载根统一推导，无需重复填写
        assert by_provider["pan115"]["local_path"] == str(tmp_path / "quark" / "115")
        serialized = json.dumps(body, ensure_ascii=False)
        assert "quark-user" not in serialized
        assert "p@ssw0rd" not in serialized
        assert "token" not in serialized.lower()

    def test_routes_must_be_inside_remote_root(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.put(
            "/api/openlist/routes",
            json={"routes": [{"route_id": "r1", "label": "外部", "remote_prefix": "/外部", "provider_id": "quark", "enabled": True}]},
        )
        assert resp.status_code == 400
        assert "不在远端总根之下" in resp.json()["detail"]

    def test_duplicate_route_prefix_rejected(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {"route_id": "r1", "label": "动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True},
                    {"route_id": "r2", "label": "动画副本", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "pan115", "enabled": True},
                ]
            },
        )
        assert resp.status_code == 400
        assert "重复配置" in resp.json()["detail"]

    def test_root_prefix_rejected_as_route(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.put(
            "/api/openlist/routes",
            json={"routes": [{"route_id": "r1", "label": "整个根", "remote_prefix": "/", "provider_id": "quark", "enabled": True}]},
        )
        assert resp.status_code == 400

    def test_unknown_provider_rejected(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.put(
            "/api/openlist/routes",
            json={"routes": [{"route_id": "r1", "label": "x", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "aliyun", "enabled": True}]},
        )
        assert resp.status_code == 400
        assert "未知内容提供商" in resp.json()["detail"]

    def test_longest_prefix_match_wins(self, client, tmp_path):
        _save_config(client, tmp_path)
        _save_routes(
            client,
            [
                {"route_id": "parent", "label": "动画总", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True},
                {"route_id": "child", "label": "冰菓专用", "remote_prefix": REMOTE_ROOT + "/动画/冰菓", "provider_id": "pan115", "enabled": True},
            ],
        )
        from app.integrations.openlist.providers import provider_for_remote
        from app.core.config import load_config

        config = load_config()
        routes = config.openlist_routes
        assert provider_for_remote(routes, REMOTE_ROOT + "/动画/冰菓") == ("child", "pan115")
        assert provider_for_remote(routes, REMOTE_ROOT + "/动画/真人")[1] == "quark"

    def test_route_response_has_local_path_preview_and_no_credentials(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(
            client,
            [{"route_id": "r1", "label": "动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True}],
        )
        body = client.get("/api/openlist/routes").json()
        route = body["routes"][0]
        assert route["local_path"] == str(tmp_path / "quark" / "动画")
        assert route["local_available"] is True
        serialized = json.dumps(body, ensure_ascii=False)
        assert "quark-user" not in serialized
        assert "p@ssw0rd" not in serialized
        assert "token" not in serialized.lower()

    def test_discover_suggests_but_does_not_persist_provider(self, client, tmp_path):
        """目录名只产生建议（115→pan115），未确认不得成为 provider 事实。"""
        FakeOpenListClient.tree = {
            "/": [
                ("115", True, None, None),
                ("百度", True, None, None),
                ("夸克", True, None, None),
                ("杂项", True, None, None),
                ("readme.txt", False, 5, 1),
            ]
        }
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": "/",
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200
        body = client.post("/api/openlist/routes/discover").json()
        hints = {item["name"]: item["hint_provider"] for item in body["items"]}
        assert hints["115"] == "pan115"
        assert hints["百度"] == "baidu"
        assert hints["夸克"] == "quark"
        assert hints["杂项"] == "other"
        assert "readme.txt" not in hints  # 只列目录
        # 未保存前不成为事实
        assert client.get("/api/openlist/routes").json()["routes"] == []


# ============================================================
# 懒加载浏览与缓存
# ============================================================

class TestBrowseCache:
    def test_browse_single_layer_no_recursion(self, client, tmp_path):
        """普通浏览只请求当前目录一层，绝不递归扫描后代。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"})
        client_instance = FakeOpenListClient.instances[-1]
        paths = [call[0] for call in client_instance.calls]
        assert paths == [REMOTE_ROOT + "/动画"]
        # 未浏览冰菓/真人子目录
        assert REMOTE_ROOT + "/动画/冰菓" not in paths

    def test_browse_uses_refresh_false_by_default(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"})
        client_instance = FakeOpenListClient.instances[-1]
        assert client_instance.calls[0][1] is False

    def test_explicit_refresh_uses_refresh_true_and_updates_cache(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        first = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"}).json()
        assert first["cache"]["status"] == "none"
        # 显式强制刷新当前层
        second = client.get(
            "/api/openlist/browse",
            params={"path": REMOTE_ROOT + "/动画", "refresh": "true"},
        ).json()
        assert second["refresh_requested"] is True
        assert second["cache"]["status"] == "none"
        client_instance = FakeOpenListClient.instances[-1]
        assert client_instance.calls[-1][1] is True

    def test_cache_hit_serves_without_upstream_request(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"})
        first_client = FakeOpenListClient.instances[-1]
        calls_after_first = list(first_client.calls)

        second = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"}).json()
        assert second["cache"]["cached"] is True
        assert second["cache"]["status"] == "fresh"
        assert len(FakeOpenListClient.instances) == 1  # 未新建客户端
        assert first_client.calls == calls_after_first  # 未再请求上游

    def test_stale_cache_returns_and_refreshes_in_background(self, client, tmp_path):
        """缓存过期：先返回 stale 数据，后台 refresh=false 更新当前层。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"})

        from app.core.config import load_config
        from app.integrations.openlist.cache import connection_key, read_cache

        config = load_config()
        conn_key = connection_key(
            config.openlist_server_url, config.openlist_username,
            normalize_remote_path(config.openlist_remote_root or "/"),
        )
        cached = read_cache(conn_key, REMOTE_ROOT + "/动画")
        assert cached is not None
        # 把缓存 TTL 改短并直接改配置：这里通过把 expires_at 改到过去来模拟过期
        cache_file = tmp_path / "data" / "openlist_cache" / f"conn_{conn_key}" / (
            __import__("app.integrations.openlist.cache", fromlist=["_path_key"])._path_key(REMOTE_ROOT + "/动画") + ".json"
        )
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        payload["fetched_at"] = time.time() - 9999
        payload["expires_at"] = time.time() - 1
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        stale = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"}).json()
        assert stale["cache"]["status"] == "stale"
        assert stale["cache"]["refreshing"] is True
        # 后台线程用 refresh=false 刷新
        refreshed = _wait_cache_fresh(tmp_path, conn_key, REMOTE_ROOT + "/动画")
        assert refreshed is not None
        assert refreshed["fresh"] is True
        assert FakeOpenListClient.instances[-1].calls[-1][1] is False

    def test_forced_refresh_failure_keeps_old_cache(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"})
        FakeOpenListClient.permission_path = REMOTE_ROOT + "/动画"
        resp = client.get(
            "/api/openlist/browse",
            params={"path": REMOTE_ROOT + "/动画", "refresh": "true"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cache"]["cached"] is True
        assert body["cache"]["status"] == "stale"
        assert body["cache"]["refresh_failed"] is True
        assert [e["name"] for e in body["entries"]] == ["冰菓", "真人"]

    def test_forced_refresh_failure_without_cache_returns_error(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        FakeOpenListClient.permission_path = REMOTE_ROOT + "/动画"
        resp = client.get(
            "/api/openlist/browse",
            params={"path": REMOTE_ROOT + "/动画", "refresh": "true"},
        )
        assert resp.status_code == 400


class TestCacheModule:
    def test_whitelist_fields_only(self, tmp_path, monkeypatch):
        """缓存只保存白名单字段，不缓存 Token/直链/内部 path。"""
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        from app.integrations.openlist.cache import (
            connection_key,
            write_cache,
            read_cache,
        )
        conn = connection_key("https://x", "u", "/")
        entries = [
            {"name": "动画", "is_dir": True, "size": None, "modified": 1,
             "remote_path": "/动画", "token": "secret", "url": "http://evil/1"},
            {"name": "a.mkv", "is_dir": False, "size": 100, "modified": 2,
             "remote_path": "/a.mkv", "token": "secret", "raw_path": "/internal/1"},
        ]
        write_cache(conn, "/", entries, 1440)
        cached = read_cache(conn, "/")
        assert cached is not None
        serialized = json.dumps(cached, ensure_ascii=False)
        assert "secret" not in serialized
        assert "http://evil" not in serialized
        assert "/internal" not in serialized
        assert cached["entries"][0]["name"] == "动画"

    def test_ttl_expiry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        from app.integrations.openlist.cache import connection_key, write_cache, read_cache
        conn = connection_key("https://x", "u", "/")
        now = 1000.0
        write_cache(conn, "/动画", [{"name": "x", "is_dir": True, "size": None, "modified": 1, "remote_path": "/动画/x"}], 10, now=now)
        fresh = read_cache(conn, "/动画", now=now + 5)
        assert fresh is not None and fresh["fresh"] is True
        expired = read_cache(conn, "/动画", now=now + 601)
        assert expired is not None and expired["fresh"] is False

    def test_capacity_eviction_lru(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        import app.integrations.openlist.cache as cache_module
        cache_module.MAX_CACHE_PATHS = 3
        conn = cache_module.connection_key("https://x", "u", "/")
        now = 1000.0
        for index in range(5):
            cache_module.write_cache(
                conn, f"/dir{index}",
                [{"name": "x", "is_dir": True, "size": None, "modified": 1, "remote_path": f"/dir{index}/x"}],
                1440, now=now + index,
            )
        # 最近写入的 3 个保留，最早 2 个被淘汰
        assert cache_module.read_cache(conn, "/dir4") is not None
        assert cache_module.read_cache(conn, "/dir3") is not None
        assert cache_module.read_cache(conn, "/dir2") is not None
        assert cache_module.read_cache(conn, "/dir1") is None
        assert cache_module.read_cache(conn, "/dir0") is None


class TestPrefetch:
    def test_prefetch_bounded_no_recursion(self, client, tmp_path):
        """预取只拉取指定路径一层，不递归后代。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/prefetch",
            json={"paths": [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["prefetched"] == 2
        calls = FakeOpenListClient.instances[-1].calls
        assert {call[0] for call in calls} == {REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"}
        # 未递归到任何后代
        assert all(call[0] not in (REMOTE_ROOT + "/动画/冰菓/x",) for call in calls)

    def test_prefetch_skips_fresh_cache(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"})
        calls_before = list(FakeOpenListClient.instances[-1].calls)
        resp = client.post(
            "/api/openlist/prefetch",
            json={"paths": [REMOTE_ROOT + "/动画"]},
        )
        assert resp.status_code == 200
        assert resp.json()["prefetched"] == 0
        assert resp.json()["skipped"] == 1
        assert FakeOpenListClient.instances[-1].calls == calls_before


# ============================================================
# 批量导入
# ============================================================

class TestBatchImport:
    def test_rejects_parent_child_overlap(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(
            client,
            [{"route_id": "r1", "label": "动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True}],
        )
        resp = client.post(
            "/api/openlist/batch-import",
            json={
                "remote_paths": [REMOTE_ROOT + "/动画", REMOTE_ROOT + "/动画/冰菓"],
                "import_family": "anime",
            },
        )
        assert resp.status_code == 400
        assert "父子重叠" in resp.json()["detail"]

    def test_rejects_unclassified_path(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画"], "import_family": "anime"},
        )
        assert resp.status_code == 400
        assert "尚未归类" in resp.json()["detail"]

    def test_rejects_over_limit(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        paths = [f"{REMOTE_ROOT}/动画/目录{i}" for i in range(21)]
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": paths, "import_family": "anime"},
        )
        assert resp.status_code == 400
        assert "一次最多导入" in resp.json()["detail"]

    def test_batch_success_creates_independent_presets(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(
            client,
            [{"route_id": "r1", "label": "动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True}],
        )
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        result = record["result"]
        assert result["succeeded"] == 2
        assert result["failed"] == 0
        for item in result["batch"]:
            assert item["status"] == "success"
            assert item["provider_id"] == "quark"
            assert item["preset_id"]
            assert item["plan_id"]

        from app.media_presets.store import list_presets
        presets = list_presets()
        assert len(presets) == 2
        assert {preset.remote_locator for preset in presets} == {
            REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人",
        }
        for preset in presets:
            assert preset.provider_id == "quark"
            assert preset.ingest_method == "openlist_api"
            assert preset.source_route_id == "r1"

    def test_batch_strictly_serial(self, client, tmp_path):
        """严格串行：一个目录完整扫完才开始下一个。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"], "import_family": "anime"},
        )
        _wait_task(client, resp.json()["task_id"])
        calls = FakeOpenListClient.instances[-1].calls
        paths = [call[0] for call in calls]
        assert paths[0] == REMOTE_ROOT + "/动画/冰菓"
        first_real = next(
            (index for index, path in enumerate(paths) if path == REMOTE_ROOT + "/动画/真人"),
            None,
        )
        assert first_real is not None
        # 一旦开始扫描第二个目录，后面不再出现第一个目录
        assert all(path == REMOTE_ROOT + "/动画/真人" for path in paths[first_real:])

    def test_batch_partial_failure_isolation(self, client, tmp_path):
        """一个目录失败不回滚其他成功目录；错误消息安全。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(
            client,
            [
                {"route_id": "r1", "label": "冰菓", "remote_prefix": REMOTE_ROOT + "/动画/冰菓", "provider_id": "quark", "enabled": True},
                {"route_id": "r2", "label": "真人", "remote_prefix": REMOTE_ROOT + "/动画/真人", "provider_id": "quark", "enabled": True},
            ],
        )
        # 本地挂载中移除冰菓目录 → 该目录扫描失败
        import shutil
        shutil.rmtree(tmp_path / "quark" / "动画" / "冰菓")
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"], "import_family": "anime"},
        )
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        result = record["result"]
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        by_path = {item["remote_path"]: item for item in result["batch"]}
        assert by_path[REMOTE_ROOT + "/动画/冰菓"]["status"] == "error"
        assert "token" not in json.dumps(result, ensure_ascii=False).lower()
        assert "password" not in json.dumps(result, ensure_ascii=False).lower()
        # 成功目录已形成独立预设
        from app.media_presets.store import list_presets
        assert len(list_presets()) == 1
        assert list_presets()[0].remote_locator == REMOTE_ROOT + "/动画/真人"

    def test_batch_cancel_stops_unstarted_directories(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        original_list = FakeOpenListClient._list_dir_default

        def slow_list_dir(self, path, page=1, per_page=100, refresh=False):
            time.sleep(0.05)
            return original_list(self, path, page=page, per_page=per_page, refresh=refresh)

        FakeOpenListClient.list_dir = slow_list_dir
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"], "import_family": "anime"},
        ).json()
        client.post(f"/api/tasks/{resp['task_id']}/cancel")
        record = _wait_task(client, resp["task_id"])
        assert record["status"] == "cancelled"
        # 未开始的目录不被扫描
        calls = FakeOpenListClient.instances[-1].calls
        assert all(call[0] == REMOTE_ROOT + "/动画/冰菓" for call in calls)


# ============================================================
# 旧预设兼容
# ============================================================

class TestLegacyPresetCompatibility:
    def _write_legacy_preset_index(self, tmp_path: Path, *, source_root: str = "K:\\夸克\\动画") -> None:
        index = tmp_path / "data" / "media_presets" / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            json.dumps(
                {
                    "version": 1,
                    "presets": [
                        {
                            "preset_id": "legacy-1",
                            "name": "旧夸克试点",
                            "source": "openlist",
                            "source_root": source_root,
                            "import_family": "anime",
                            "import_scope": "",
                            "update_mode": "openlist_scan",
                            "remote_locator": "/夸克网盘/动画",
                            "version_count": 1,
                            "versions": [
                                {
                                    "version_id": "v1",
                                    "preset_id": "legacy-1",
                                    "original_name": "动画（OpenList）",
                                    "archive_path": "media_presets/legacy-1/versions/v1.json",
                                    "input_type": "openlist",
                                    "remote_locator": "/夸克网盘/动画",
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_legacy_preset_backfills_provider_without_rewriting_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        self._write_legacy_preset_index(tmp_path)
        from app.media_presets.store import list_presets
        presets = list_presets()
        assert len(presets) == 1
        preset = presets[0]
        assert preset.preset_id == "legacy-1"
        assert preset.provider_id == "quark"
        assert preset.ingest_method == "openlist_api"
        # 磁盘索引未被回填过程重写（旧字段保留）
        raw = json.loads((tmp_path / "data" / "media_presets" / "index.json").read_text(encoding="utf-8"))
        assert "provider_id" not in raw["presets"][0]

    def test_legacy_115_baidu_local_backfill(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        index = tmp_path / "data" / "media_presets" / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            json.dumps(
                {
                    "version": 1,
                    "presets": [
                        {"preset_id": "p1", "source": "pan115", "source_root": "K:\\115", "update_mode": "directory_tree"},
                        {"preset_id": "p2", "source": "baidu", "source_root": "K:\\百度", "update_mode": "directory_tree"},
                        {"preset_id": "p3", "source": "local", "source_root": "D:\\本地", "update_mode": "local_scan"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        from app.media_presets.store import list_presets
        presets = {preset.preset_id: preset for preset in list_presets()}
        assert presets["p1"].provider_id == "pan115" and presets["p1"].ingest_method == "directory_tree"
        assert presets["p2"].provider_id == "baidu" and presets["p2"].ingest_method == "directory_tree"
        assert presets["p3"].provider_id == "local" and presets["p3"].ingest_method == "local_scan"

    def test_legacy_openlist_preset_rescan(self, client, tmp_path):
        """旧 source=openlist 预设（无新字段、有完整基线）仍可读取与 rescan。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        # 先正常导入创建带完整基线的预设
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画/冰菓", "import_family": "anime"},
        )
        assert resp.status_code == 200
        first = _wait_task(client, resp.json()["task_id"])
        assert first["status"] == "succeeded", first

        # 模拟旧格式：从磁盘索引移除新字段（provider/ingest/route）
        index_path = tmp_path / "data" / "media_presets" / "index.json"
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        for preset in raw["presets"]:
            preset.pop("provider_id", None)
            preset.pop("ingest_method", None)
            preset.pop("source_route_id", None)
        index_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        # 读取时兼容回填，不重写媒体库 ID
        from app.media_presets.store import list_presets
        preset = list_presets()[0]
        assert preset.preset_id == first["result"]["preset_id"]
        assert preset.provider_id == "quark"
        assert preset.ingest_method == "openlist_api"

        # 旧预设 rescan 仍成功
        resp = client.post(f"/api/openlist/presets/{preset.preset_id}/rescan")
        assert resp.status_code == 200, resp.text
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        assert list_presets()[0].provider_id == "quark"


# ============================================================
# .strm 契约源：OpenList 快照 real_path 恒为本地绝对路径
# ============================================================

class TestStrmSourceContract:
    def test_openlist_snapshot_real_paths_are_local_absolute(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画/冰菓", "import_family": "anime"},
        )
        assert resp.status_code == 200
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        from app.raw.store import load_raw_snapshot
        snapshot = load_raw_snapshot(record["result"]["snapshot_id"])
        assert snapshot is not None
        assert snapshot.video_count == 2
        for file_item in snapshot.files:
            real = str(file_item.real_path or "")
            assert real.startswith(str(tmp_path / "quark"))  # 本地绝对路径
            assert not real.startswith(("http://", "https://"))
            assert "token" not in real.lower()
            assert "authorization" not in real.lower()
