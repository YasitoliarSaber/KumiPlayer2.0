"""OpenList API 端点聚焦测试。

覆盖：未配置引导、配置保存与敏感值掩码、连接测试、目录浏览、
后台导入、同 SHA 更新、取消无残留、路径不可达、预设更新。
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import (
    OpenListAuthError,
    OpenListEntry,
    OpenListPermissionError,
)
from app.main import app
from app.media_presets.store import list_presets

REMOTE_ROOT = "/夸克网盘"
TREE = {
    "/夸克网盘": [("动画", True, None, None), ("说明.txt", False, 5, 1)],
    "/夸克网盘/动画": [("冰菓", True, None, None)],
    "/夸克网盘/动画/冰菓": [
        ("冰菓 - 01.mkv", False, 100, 1700000000),
        ("冰菓 - 02.mkv", False, 200, 1700000001),
    ],
}


class FakeOpenListClient:
    """替换 app.api.openlist.OpenListClient 的假客户端。"""

    instances = []
    login_user = ""  # 该用户登录失败
    permission_path = ""  # 该路径拒绝访问
    tree = TREE

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.calls = []
        FakeOpenListClient.instances.append(self)

    def login(self):
        if self.username == FakeOpenListClient.login_user:
            raise OpenListAuthError()
        return "fake-token"

    def _list_dir_default(self, path, page=1, per_page=100, refresh=False):
        normalized = normalize_remote_path(path)
        self.calls.append((normalized, bool(refresh), page))
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
        return type(
            "Page",
            (),
            {"entries": entries[start:start + per_page], "total": len(entries)},
        )()

    list_dir = _list_dir_default  # 实例方法入口，测试可整体替换


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    """模块 1 起 browse/test-connection 会查 source_health 表：临时 SQLite 初始化。

    TestClient(app) 不触发 lifespan（init_db 在 app 启动时才执行），
    这里显式为整个文件的 API 测试准备隔离数据库。
    """
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "api.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.login_user = ""
    FakeOpenListClient.permission_path = ""
    FakeOpenListClient.tree = TREE
    # 恢复 list_dir 为默认实例方法（取消测试可能整体替换过）
    FakeOpenListClient.list_dir = FakeOpenListClient._list_dir_default
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _wait_task(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(f"/api/tasks/{task_id}").json()
        if record["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"任务 {task_id} 超时未完成")
    # 等待任务线程完全退出（释放 (task_type, source) 并发槽），
    # 避免后续提交同一来源任务时被 409 拒绝。
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


def _make_local_mount(tmp_path: Path) -> Path:
    """构造与 TREE 对应的本地挂载；返回本地动画根。"""
    root = tmp_path / "quark" / "动画"
    (root / "冰菓").mkdir(parents=True)
    (root / "冰菓" / "冰菓 - 01.mkv").write_bytes(b"1")
    (root / "冰菓" / "冰菓 - 02.mkv").write_bytes(b"2")
    return root


def _save_config(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/openlist/config",
        json={
            "server_url": "https://ol.example.com:5244",
            "remote_root": REMOTE_ROOT,
            "mount_root": str(tmp_path / "quark"),
            "username": "quark-user",
            "password": "p@ssw0rd",
        },
    )
    assert resp.status_code == 200, resp.text


def _save_routes(
    client: TestClient,
    prefix: str = REMOTE_ROOT + "/动画",
    provider: str = "quark",
    *,
    label: str = "夸克",
) -> None:
    resp = client.put(
        "/api/openlist/routes",
        json={
            "routes": [
                {
                    "route_id": "route-test",
                    "label": label,
                    "remote_prefix": prefix,
                    "provider_id": provider,
                    "enabled": True,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text


class TestConfigEndpoint:
    def test_save_rejects_public_http(self, client):
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://ol.example.com",
                "remote_root": "/夸克网盘",
                "mount_root": "C:\\quark",
                "username": "u",
                "password": "p",
            },
        )
        assert resp.status_code == 400
        assert "公网 HTTP" in resp.json()["detail"]

    def test_save_requires_insecure_confirm_for_lan_http(self, client):
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://192.168.1.10:5244",
                "remote_root": "/夸克网盘",
                "mount_root": "C:\\quark",
                "username": "u",
                "password": "p",
            },
        )
        assert resp.status_code == 400
        assert "确认风险" in resp.json()["detail"]
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://192.168.1.10:5244",
                "remote_root": "/夸克网盘",
                "mount_root": "C:\\quark",
                "username": "u",
                "password": "p",
                "allow_insecure_http": True,
            },
        )
        assert resp.status_code == 200

    def test_save_rejects_missing_fields(self, client):
        resp = client.post(
            "/api/openlist/config",
            json={"server_url": "https://ol.example.com", "remote_root": "/", "mount_root": "", "username": "u"},
        )
        assert resp.status_code == 400

    def test_config_response_never_exposes_credentials(self, client, tmp_path):
        """公共配置响应零泄露：用户名与密码字段不存在，仅返回是否已配置。"""
        import json as _json
        _save_config(client, tmp_path)
        public = client.get("/api/config").json()
        assert public["openlist_server_url"] == "https://ol.example.com:5244"
        assert public["openlist_configured"] is True
        assert "openlist_username" not in public
        assert "openlist_password" not in public
        serialized = _json.dumps(public, ensure_ascii=False)
        assert "quark-user" not in serialized
        assert "p@ssw0rd" not in serialized

    def test_config_response_configured_false_when_empty(self, client):
        public = client.get("/api/config").json()
        assert public["openlist_configured"] is False
        assert "openlist_username" not in public
        assert "openlist_password" not in public

    def test_config_save_response_has_no_credentials(self, client, tmp_path):
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "config" not in body
        assert "username" not in body
        assert "password" not in body

    def test_config_save_keeps_password_when_blank(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_config_save_keeps_saved_credentials_when_draft_is_blank(self, client, tmp_path):
        """重启后的设置表单不回显凭据，保存非凭据字段也不能清空它们。"""
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": "/",
                "mount_root": str(tmp_path / "quark"),
                "username": "",
                "password": "",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = client.post("/api/openlist/test-connection", json={})
        assert resp.json()["ok"] is True

    def test_save_normalizes_webdav_address_to_openlist_api_root(self, client, tmp_path):
        """用户粘贴 WebDAV 地址时，保存后必须回归 OpenList API 根地址。"""
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://localhost:5244/dav/",
                "remote_root": "/",
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "p@ssw0rd",
                "allow_insecure_http": True,
            },
        )
        assert resp.status_code == 200
        assert client.get("/api/config").json()["openlist_server_url"] == "http://localhost:5244"

    def test_save_normalizes_drive_only_mount_root(self, client):
        """Windows 盘符不能保存成与当前目录相关的 ``I:``。"""
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": "/",
                "mount_root": "I:",
                "username": "quark-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200
        assert client.get("/api/config").json()["openlist_mount_root"] == "I:\\"

    def test_save_loopback_http_does_not_require_insecure_confirm(self, client, tmp_path):
        """回环地址（localhost）的 HTTP 明文不经过网络，保存无需风险确认。"""
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://localhost:5244/dav/",
                "remote_root": "/",
                "mount_root": str(tmp_path / "quark"),
                "username": "admin",
                "password": "123456",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        # /dav/ 归一化为 API 根
        assert client.get("/api/config").json()["openlist_server_url"] == "http://localhost:5244"

    def test_test_connection_loopback_http_without_confirm(self, client, tmp_path):
        """测试连接：localhost HTTP 不需要风险确认即可执行。"""
        _save_config(client, tmp_path)
        resp = client.post("/api/openlist/test-connection", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "insecure_http_required" not in body or body["insecure_http_required"] is not True


class TestTestConnection:
    def _assert_no_credentials(self, body: dict) -> None:
        import json as _json
        assert "username" not in body
        assert "password" not in body
        assert "token" not in body
        serialized = _json.dumps(body, ensure_ascii=False)
        assert "quark-user" not in serialized
        assert "p@ssw0rd" not in serialized

    def test_bad_credentials(self, client, tmp_path):
        _save_config(client, tmp_path)
        FakeOpenListClient.login_user = "quark-user"
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is False
        assert "认证失败" in body["message"]
        self._assert_no_credentials(body)

    def test_no_permission(self, client, tmp_path):
        _save_config(client, tmp_path)
        FakeOpenListClient.permission_path = REMOTE_ROOT
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is False
        assert "权限" in body["message"]
        self._assert_no_credentials(body)

    def test_success(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is True
        assert "连接成功" in body["message"]
        self._assert_no_credentials(body)

    def test_unconfigured(self, client):
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is False
        self._assert_no_credentials(body)


class TestBrowse:
    def test_unconfigured_returns_400(self, client):
        resp = client.get("/api/openlist/browse", params={"path": "/"})
        assert resp.status_code == 400
        assert "尚未配置" in resp.json()["detail"]

    def test_browse_returns_entries_and_parent(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT + "/动画"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "/夸克网盘/动画"
        assert body["parent_path"] == REMOTE_ROOT
        assert [e["name"] for e in body["entries"]] == ["冰菓"]
        assert body["entries"][0]["is_dir"] is True
        assert body["entries"][0]["remote_path"] == "/夸克网盘/动画/冰菓"

    def test_browse_root_has_no_parent(self, client, tmp_path):
        _save_config(client, tmp_path)
        body = client.get("/api/openlist/browse").json()
        assert body["path"] == REMOTE_ROOT
        assert body["parent_path"] is None

    def test_browse_outside_remote_root_rejected(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.get("/api/openlist/browse", params={"path": "/其它网盘"})
        assert resp.status_code == 400
        assert "映射根" in resp.json()["detail"]


class TestImport:
    def test_unconfigured_returns_400(self, client):
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        )
        assert resp.status_code == 400

    def test_rejects_path_outside_remote_root(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": "/其它网盘/动画", "import_family": "anime"},
        )
        assert resp.status_code == 400
        assert "映射根" in resp.json()["detail"]

    def test_rejects_unreachable_local_mount(self, client, tmp_path):
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        )
        assert resp.status_code == 400
        assert "本地挂载路径不存在" in resp.json()["detail"]

    def test_rejects_import_without_route_classification(self, client, tmp_path):
        """未归类到提供商路由的目录禁止导入（浏览不受限）。"""
        _save_config(client, tmp_path)
        _make_local_mount(tmp_path)
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        )
        assert resp.status_code == 400
        assert "尚未归类" in resp.json()["detail"]

    def test_full_import_flow(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        )
        assert resp.status_code == 200
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        assert record["result"]["plan_id"]
        assert record["result"]["preset_id"]
        assert record["result"]["video_count"] == 2
        assert record["result"]["provider_id"] == "quark"

        presets = list_presets()
        assert len(presets) == 1
        preset = presets[0]
        assert preset.source == "openlist"
        assert preset.remote_locator == "/夸克网盘/动画"
        assert preset.import_family == "anime"
        assert preset.provider_id == "quark"
        assert preset.ingest_method == "openlist_api"
        assert preset.source_route_id == "route-test"

        # 任务结果不泄露凭据
        assert "p@ssw0rd" not in str(record)

    def test_same_remote_tree_unchanged(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        first = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        ).json()
        _wait_task(client, first["task_id"])
        second = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        ).json()
        record = _wait_task(client, second["task_id"])
        assert record["status"] == "succeeded"
        assert record["result"]["unchanged"] is True
        assert record["result"]["reused_preset"] is True
        assert list_presets()[0].version_count == 1

    def test_cancel_leaves_no_residue(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        original_list = FakeOpenListClient._list_dir_default

        def slow_list_dir(self, path, page=1, per_page=100, refresh=False):
            time.sleep(0.05)
            return original_list(self, path, page=page, per_page=per_page, refresh=refresh)

        FakeOpenListClient.list_dir = slow_list_dir
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        ).json()
        client.post(f"/api/tasks/{resp['task_id']}/cancel")
        record = _wait_task(client, resp["task_id"])
        assert record["status"] == "cancelled"
        assert list_presets() == []
        assert not (tmp_path / "data" / "openlist_manifests").exists()
        snapshot_dir = tmp_path / "data" / "raw_snapshots"
        assert not snapshot_dir.exists() or not any(snapshot_dir.iterdir())

    def test_rescan_preset(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        first = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画", "import_family": "anime"},
        ).json()
        first_record = _wait_task(client, first["task_id"])
        assert first_record["status"] == "succeeded", first_record.get("error") or first_record.get("message")
        preset_id = list_presets()[0].preset_id

        # 远端与本地各新增一集
        FakeOpenListClient.tree = {
            "/夸克网盘": [("动画", True, None, None), ("说明.txt", False, 5, 1)],
            "/夸克网盘/动画": [("冰菓", True, None, None)],
            "/夸克网盘/动画/冰菓": [
                ("冰菓 - 01.mkv", False, 100, 1700000000),
                ("冰菓 - 02.mkv", False, 200, 1700000001),
                ("冰菓 - 03.mkv", False, 300, 1700000002),
            ],
        }
        (tmp_path / "quark" / "动画" / "冰菓" / "冰菓 - 03.mkv").write_bytes(b"3")

        resp = client.post(f"/api/openlist/presets/{preset_id}/rescan")
        assert resp.status_code == 200
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        assert record["result"]["unchanged"] is False
        preset = list_presets()[0]
        assert preset.version_count == 2
        assert preset.current_plan_id == record["result"]["plan_id"]


class TestCooldownInterception:
    """模块 1 阶段 B：连接冷却中，browse/prefetch/test-connection 不发请求。"""

    @staticmethod
    def _health_key() -> str:
        from app.integrations.openlist.governor import governor_connection_key

        return governor_connection_key("https://ol.example.com:5244", "quark-user")

    @staticmethod
    def _enter_cooldown():
        from app.catalog import source_health

        source_health.enter_cooldown(
            TestCooldownInterception._health_key(),
            reason_kind="risk_control",
            cooldown_seconds=3600,
        )

    def test_browse_cooling_down_without_cache_423(self, client, tmp_path):
        """冷却中且无 fresh 缓存：拒绝请求（423），不构造客户端。"""
        _save_config(client, tmp_path)
        self._enter_cooldown()
        FakeOpenListClient.instances = []
        resp = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        assert resp.status_code == 423
        assert "访问保护" in resp.json()["detail"]
        assert FakeOpenListClient.instances == []  # 未发起任何网络请求

    def test_browse_cooling_down_with_fresh_cache_served(self, client, tmp_path):
        """冷却中但缓存 fresh：直接返回缓存并标注 health=cooling_down，不发请求。"""
        _save_config(client, tmp_path)
        # 先正常浏览一次写入本地缓存
        first = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        assert first.status_code == 200
        # 随后连接进入冷却
        self._enter_cooldown()
        FakeOpenListClient.instances = []
        resp = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        assert resp.status_code == 200
        body = resp.json()
        assert body["cache"]["status"] == "fresh"
        assert body["cache"]["health"] == "cooling_down"
        assert [e["name"] for e in body["entries"]] == ["动画", "说明.txt"]
        assert FakeOpenListClient.instances == []

    def test_browse_refresh_cooling_down_423(self, client, tmp_path):
        """显式强制刷新在冷却中同样被拒绝（安全优先，不做探针请求）。"""
        _save_config(client, tmp_path)
        first = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        assert first.status_code == 200
        self._enter_cooldown()
        resp = client.get(
            "/api/openlist/browse",
            params={"path": REMOTE_ROOT, "refresh": "true"},
        )
        assert resp.status_code == 423

    def test_prefetch_cooling_down_returns_empty(self, client, tmp_path):
        """冷却中 prefetch 直接返回空结果 + health 标注，不发请求。"""
        _save_config(client, tmp_path)
        self._enter_cooldown()
        FakeOpenListClient.instances = []
        resp = client.post(
            "/api/openlist/prefetch",
            json={"paths": [REMOTE_ROOT + "/动画"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["prefetched"] == 0
        assert body["skipped"] == 0
        assert body["health"] == "cooling_down"
        assert FakeOpenListClient.instances == []

    def test_test_connection_cooling_down_blocked(self, client, tmp_path):
        """冷却中主动测试连接：ok=False + 安全消息（不发登录请求）。"""
        _save_config(client, tmp_path)
        self._enter_cooldown()
        FakeOpenListClient.instances = []
        resp = client.post(
            "/api/openlist/test-connection",
            json={
                "server_url": "https://ol.example.com:5244",
                "username": "quark-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "访问保护" in body["message"]
        assert FakeOpenListClient.instances == []

    def test_no_cooldown_browse_still_works(self, client, tmp_path):
        """无冷却记录：browse 保持原行为。"""
        _save_config(client, tmp_path)
        resp = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        assert resp.status_code == 200
        assert [e["name"] for e in resp.json()["entries"]] == ["动画", "说明.txt"]


# ============================================================
# 模块 1 Review Fix（R2）：冷却期间 API 入口零网络请求（网络准入层兜底）
# ============================================================

class TestCooldownNetworkAdmission:
    """冷却中 routes/discover 与 legacy 导入任务体不发任何物理请求。

    用真实 OpenListClient + MockTransport 替换 FakeOpenListClient，
    直接统计物理请求次数（网络准入层在 client 内部，Fake 无法模拟）。
    """

    @pytest.fixture(autouse=True)
    def real_client_factory(self, monkeypatch):
        """把 app.api.openlist.OpenListClient 换成真实客户端 + MockTransport 工厂。"""
        import httpx

        from app.integrations.openlist.client import OpenListClient as RealClient
        from app.integrations.openlist.governor import OpenListRequestGovernor

        state = {"transport_calls": 0, "instances": []}

        def handler(request):
            state["transport_calls"] += 1
            return httpx.Response(404, request=request)

        def factory(server_url, username, password, **kwargs):
            client = RealClient(
                server_url, username, password,
                transport=httpx.MockTransport(handler),
                governor=OpenListRequestGovernor(rate_per_second=1000),
                **kwargs,
            )
            state["instances"].append(client)
            return client

        monkeypatch.setattr("app.api.openlist.OpenListClient", factory)
        return state

    def _enter_cooldown(self, server_url="https://ol.example.com:5244", username="quark-user"):
        from app.catalog import source_health
        from app.integrations.openlist.governor import governor_connection_key

        key = governor_connection_key(server_url, username)
        source_health.enter_cooldown(key, reason_kind="risk_control", cooldown_seconds=3600)
        return key

    def test_routes_discover_cooling_down_zero_transport(self, client, tmp_path, real_client_factory):
        """冷却中 routes/discover 被拒且零物理请求（此前无 API 层检查，靠准入层兜底）。"""
        _save_config(client, tmp_path)
        self._enter_cooldown()
        resp = client.post("/api/openlist/routes/discover")
        assert resp.status_code == 400
        assert "访问保护" in resp.json()["detail"]
        assert real_client_factory["transport_calls"] == 0

    def test_legacy_scan_task_body_cooling_down_zero_transport(
        self, tmp_path, real_client_factory,
    ):
        """legacy import/rescan 共用的任务体：冷却时构造 client 但零物理请求。"""
        from app.api.openlist import _run_openlist_scan_task
        from app.integrations.openlist.models import OpenListSourceCoolingDownError

        self._enter_cooldown()
        mount = tmp_path / "quark"
        mount.mkdir()
        with pytest.raises(OpenListSourceCoolingDownError):
            _run_openlist_scan_task(
                "https://ol.example.com:5244",
                "quark-user",
                "p@ssw0rd",
                REMOTE_ROOT,
                str(mount),
                "anime",
                "",
            )
        assert real_client_factory["transport_calls"] == 0

    def test_routes_discover_healthy_still_works(self, client, tmp_path, real_client_factory):
        """无冷却：routes/discover 正常发出物理请求（对照）。"""
        _save_config(client, tmp_path)
        resp = client.post("/api/openlist/routes/discover")
        assert resp.status_code == 400  # MockTransport 固定 404 → 归一化为错误
        assert real_client_factory["transport_calls"] >= 1
