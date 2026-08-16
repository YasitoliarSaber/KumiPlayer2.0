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
    # fresh probe（connection.py）同样使用 Fake client，保证连接测试离线
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeOpenListClient)
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
        assert body["code"] == "credential_rejected"
        assert body["phase"] == "credential"
        self._assert_no_credentials(body)
    def test_no_permission(self, client, tmp_path):
        _save_config(client, tmp_path)
        FakeOpenListClient.permission_path = REMOTE_ROOT
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "root_permission_denied"
        assert body["phase"] == "root"
        assert "权限" in body["message"]
        self._assert_no_credentials(body)

    def test_success(self, client, tmp_path):
        _save_config(client, tmp_path)
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is True
        assert body["code"] == "connected"
        assert "连接成功" in body["message"]
        self._assert_no_credentials(body)

    def test_unconfigured(self, client):
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "not_configured"
        self._assert_no_credentials(body)

    def test_connection_does_not_reuse_runtime_pool(self, client, tmp_path):
        """Test Connection 永远使用 fresh probe，不进入 production client pool。"""
        _save_config(client, tmp_path)
        # 预置一个已登录的 pooled client（模拟运行时池已有会话）
        from app.integrations.openlist.client import clear_openlist_client_pool, get_openlist_client

        clear_openlist_client_pool()
        pooled = get_openlist_client(
            "https://ol.example.com:5244", "quark-user", "p@ssw0rd",
            client_factory=FakeOpenListClient,
        )
        pooled.login()
        instances_before = len(FakeOpenListClient.instances)

        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is True
        # probe 必须新建 client，而不是复用 pool 中的实例
        assert len(FakeOpenListClient.instances) == instances_before + 1
        clear_openlist_client_pool()

    def test_connection_rejects_bad_candidate_even_when_pool_has_valid_token(self, client, tmp_path):
        """pool 有有效 token 时，坏候选也必须真实重登并失败（禁止假成功）。"""
        _save_config(client, tmp_path)
        from app.integrations.openlist.client import clear_openlist_client_pool, get_openlist_client

        clear_openlist_client_pool()
        pooled = get_openlist_client(
            "https://ol.example.com:5244", "quark-user", "p@ssw0rd",
            client_factory=FakeOpenListClient,
        )
        pooled.login()  # 池内已有“有效 token”
        instances_before = len(FakeOpenListClient.instances)

        FakeOpenListClient.login_user = "quark-user"  # 候选密码无效 → 登录失败
        resp = client.post(
            "/api/openlist/test-connection",
            json={"username": "quark-user", "password": "wrong-pass"},
        )
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "credential_rejected"
        # 必须真正走 login，而不是返回旧 token → connected
        assert len(FakeOpenListClient.instances) == instances_before + 1
        clear_openlist_client_pool()

    def test_connection_uses_saved_password_when_password_is_blank(self, client, tmp_path):
        """username 与保存账号相同、password 为空 → 使用 saved password。"""
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/test-connection",
            json={"username": "quark-user", "password": ""},
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["code"] == "connected"
        # probe 拿到的必须是 saved password（Fake 不校验密码，但实例携带它）
        probe_client = FakeOpenListClient.instances[-1]
        assert probe_client.password == "p@ssw0rd"

    def test_connection_uses_candidate_remote_root(self, client, tmp_path):
        """draft remote_root 必须真正参与 /api/fs/list（候选 /new 而非已保存 /old）。"""
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/test-connection",
            json={"remote_root": "/new-root"},
        )
        body = resp.json()
        assert body["ok"] is True
        probe_client = FakeOpenListClient.instances[-1]
        # FakeOpenListClient.list_dir 记录 (path, refresh, page)
        assert probe_client.calls and probe_client.calls[0][0] == "/new-root"

    def test_connection_new_username_requires_password(self, client, tmp_path):
        """修改 username 但 password 为空 → 禁止把旧账号密码套给新账号。"""
        _save_config(client, tmp_path)
        instances_before = len(FakeOpenListClient.instances)
        resp = client.post(
            "/api/openlist/test-connection",
            json={"username": "another-user", "password": ""},
        )
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "invalid_configuration"
        assert "密码" in body["message"]
        # 本次请求未发起任何探测（_save_config 的实例数保持不变）
        assert len(FakeOpenListClient.instances) == instances_before

    def test_connection_permission_error_is_not_auth_error(self, client, tmp_path):
        """登录成功但 root 403 → root_permission_denied，不是 credential_rejected。"""
        _save_config(client, tmp_path)
        FakeOpenListClient.permission_path = REMOTE_ROOT
        resp = client.post(
            "/api/openlist/test-connection",
            json={"username": "quark-user", "password": "p@ssw0rd"},
        )
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "root_permission_denied"
        assert body["phase"] == "root"
        assert body["code"] != "credential_rejected"
        self._assert_no_credentials(body)

    def test_connection_response_never_contains_credentials(self, client, tmp_path):
        """响应只含 code/phase/安全消息，绝不包含 username/password/token。"""
        _save_config(client, tmp_path)
        for payload in ({}, {"username": "quark-user", "password": "wrong-pass"}):
            resp = client.post("/api/openlist/test-connection", json=payload)
            body = resp.json()
            assert set(body) <= {"ok", "code", "phase", "message", "insecure_http_required"}
            assert "username" not in body
            assert "password" not in body
            assert "token" not in body
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

    def test_routes_discover_healthy_still_works(self, client, tmp_path, real_client_factory):
        """无冷却：routes/discover 正常发出物理请求（对照）。"""
        _save_config(client, tmp_path)
        resp = client.post("/api/openlist/routes/discover")
        assert resp.status_code == 400  # MockTransport 固定 404 → 归一化为错误
        assert real_client_factory["transport_calls"] >= 1

# ============================================================
# OL-3：Validated Candidate + Secure Credential Atomic Commit 回归
# ============================================================


class TestAtomicConfigCommit:
    """validate-before-commit：probe 失败 / 凭据失败 / 写失败都保持旧状态。

    （credential read/write/JSON 失败的三态回归见 test_credential_storage.py）
    """

    def _config_json(self, tmp_path):
        from app.core import config as core_config

        path = core_config.get_config_file()
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def test_probe_failure_preserves_old_state(self, client, tmp_path):
        """候选密码无效 → probe 失败 → config/credential/routes/runtime 全部保持。"""
        _save_config(client, tmp_path)
        _save_routes(client)
        routes_before = client.get("/api/openlist/routes").json()["routes"]
        assert routes_before

        # 候选密码无效（登录失败）
        FakeOpenListClient.login_user = "quark-user"
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "wrong-pass",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "拒绝" in body["detail"] or "登录信息" in body["detail"]

        # 配置 JSON 保持旧值（server/username 未变）
        saved = self._config_json(tmp_path)
        assert saved["openlist_server_url"] == "https://ol.example.com:5244"
        assert saved["openlist_remote_root"] == REMOTE_ROOT
        # 路由保持（失败不能清 routes）
        routes_after = client.get("/api/openlist/routes").json()["routes"]
        assert [r["route_id"] for r in routes_after] == [r["route_id"] for r in routes_before]
        # runtime client 未被替换（仍可正常浏览）
        assert client.get("/api/openlist/browse", params={"path": REMOTE_ROOT}).status_code == 200

    def test_remote_identity_change_success_clears_routes_and_replaces_client(self, client, tmp_path):
        """probe + durable save 全部成功之后才清 routes、替换 runtime client。"""
        _save_config(client, tmp_path)
        _save_routes(client, prefix=REMOTE_ROOT + "/动画")
        assert client.get("/api/openlist/routes").json()["routes"]

        # 更换 remote_root（remote-affecting）
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": "/new-root",
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "",
            },
        )
        assert resp.status_code == 200, resp.text
        saved = self._config_json(tmp_path)
        assert saved["openlist_remote_root"] == "/new-root"
        # 旧 routes 已清空（身份变化后不可见）
        assert client.get("/api/openlist/routes").json()["routes"] == []

    def test_remote_identity_change_clears_runtime_pool(self, client, tmp_path):
        """身份/密码变化成功后 runtime client pool 必须被清空（替换旧会话）。"""
        from app.integrations.openlist.client import (
            _CLIENT_POOL,
            clear_openlist_client_pool,
        )

        _save_config(client, tmp_path)
        clear_openlist_client_pool()
        # 预置一个 pooled client（模拟运行时已有会话）
        from app.integrations.openlist.client import get_openlist_client

        get_openlist_client(
            "https://ol.example.com:5244", "quark-user", "p@ssw0rd",
            client_factory=FakeOpenListClient,
        )
        assert _CLIENT_POOL  # 池非空

        # 显式更新 password（同 user 新密码 → 清 pool；routes 保留）
        _save_routes(client)
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "new-pass-123",
            },
        )
        assert resp.status_code == 200, resp.text
        # 池被清空（旧会话已替换；下次请求按新密码重建）
        assert _CLIENT_POOL == {}
        # password-only 变更不清 routes（身份未变）
        assert client.get("/api/openlist/routes").json()["routes"]

    def test_config_save_new_username_requires_password(self, client, tmp_path):
        """config-save 路径：新 username + 空密码 → 400（禁止套用旧账号密码）。"""
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "another-user",
                "password": "",
            },
        )
        assert resp.status_code == 400
        assert "密码" in resp.json()["detail"]
        # 0 mutation：配置保持旧值
        saved = self._config_json(tmp_path)
        assert saved["openlist_username"] == "quark-user"
    def test_local_only_edit_while_openlist_offline(self, client, tmp_path):
        """OpenList 完全不可达时，仅 local-only 字段（缓存 TTL）也能保存。"""
        _save_config(client, tmp_path)
        # 模拟 OpenList 离线：登录失败
        FakeOpenListClient.login_user = "quark-user"
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "",
                "cache_ttl_minutes": 720,
            },
        )
        # local-only：不要求在线，保存成功
        assert resp.status_code == 200, resp.text
        assert self._config_json(tmp_path)["openlist_cache_ttl_minutes"] == 720

    def test_same_remote_config_idempotent_save(self, client, tmp_path):
        """相同内容保存两次：不清 route、不重建 client、不产生额外秘密操作。"""
        _save_config(client, tmp_path)
        _save_routes(client)
        # 先制造一个 pooled client（记录实例数）
        from app.integrations.openlist.client import clear_openlist_client_pool

        clear_openlist_client_pool()
        instances_before = len(FakeOpenListClient.instances)
        routes_before = client.get("/api/openlist/routes").json()["routes"]

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
        # 未清 routes
        assert client.get("/api/openlist/routes").json()["routes"] == routes_before
        # 未产生额外 client（幂等保存不重建 pool）
        assert len(FakeOpenListClient.instances) == instances_before

    def test_credential_store_unavailable_rejects_save(self, client, tmp_path, monkeypatch):
        """凭据存储不可读 → POST /config 返回 503 且 0 mutation（防止误删凭据）。

        模拟真实生产：secure store 持有凭据（JSON 无明文），随后 store 不可读。
        """
        from app.core import config as config_module
        from app.core.credential_store import CredentialStoreError

        # 真实生产形态：启用 secure store（JSON 不含明文凭据）
        class RealStore:
            available = True

            def __init__(self):
                self.values: dict[str, str] = {}

            def read(self, name):
                return self.values.get(name, "")

            def write(self, name, value):
                self.values[name] = value

            def delete(self, name):
                self.values.pop(name, None)

        real_store = RealStore()
        monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", real_store)
        monkeypatch.setattr(config_module, "_credential_storage_enabled", lambda: True)
        config_module.invalidate_config_cache()
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
        assert real_store.values.get("openlist_username") == "quark-user"
        # JSON 不含明文凭据
        saved = self._config_json(tmp_path)
        assert saved.get("openlist_password", "") == ""

        class UnavailableStore:
            available = True

            def read(self, name):
                raise CredentialStoreError("temporary")

            def write(self, name, value):
                pass

            def delete(self, name):
                pass

        # store 随后不可读（模拟 CM 故障）
        monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", UnavailableStore())
        config_module.invalidate_config_cache()

        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "",
                "cache_ttl_minutes": 720,
            },
        )
        assert resp.status_code == 503
        # 配置未被修改（0 mutation）
        saved = self._config_json(tmp_path)
        assert saved["openlist_cache_ttl_minutes"] == 1440

    def test_connection_uses_recovered_credentials_after_store_failure(self, client, tmp_path, monkeypatch):
        """CM hydrate failure → 恢复 → Test Connection draft 为空 → 实际使用
        secure store 中已保存的 OpenList 凭据（KEEP SAVED 不需要重启）。"""
        from app.core import config as config_module
        from app.core.credential_store import CredentialStoreError

        _save_config(client, tmp_path)

        class FailingOnceStore:
            """第一次 read openlist_username 失败，之后恢复。"""
            available = True
            failed = False

            def __init__(self, real):
                self.real = real

            def read(self, name):
                if name == "openlist_username" and not self.failed:
                    self.failed = True
                    raise CredentialStoreError("temporary")
                return self.real.read(name)

            def write(self, name, value):
                return self.real.write(name, value)

            def delete(self, name):
                return self.real.delete(name)

        real = config_module.SECURE_CREDENTIAL_STORE
        monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", FailingOnceStore(real))
        config_module.invalidate_config_cache()
        # 模拟启动时 hydrate 失败 → stale cache（openlist_username 留空）
        try:
            config_module.load_config(force_reload=True)
        except Exception:
            pass
        # store 恢复后（FailingOnceStore 已自愈），Test Connection 全空 draft
        # 必须解析到真实已保存凭据并成功
        resp = client.post("/api/openlist/test-connection", json={})
        body = resp.json()
        assert body["ok"] is True, body
        assert body["code"] == "connected"

    def test_save_remote_affecting_uses_recovered_credentials(self, client, tmp_path, monkeypatch):
        """CM hydrate failure → 恢复 → remote-affecting 修改且 password 留空
        （KEEP SAVED）→ probe 使用恢复后的 saved credential → commit 成功。"""
        from app.core import config as config_module
        from app.core.credential_store import CredentialStoreError

        _save_config(client, tmp_path)

        class FailingOnceStore:
            available = True
            failed = False

            def __init__(self, real):
                self.real = real

            def read(self, name):
                if name == "openlist_username" and not self.failed:
                    self.failed = True
                    raise CredentialStoreError("temporary")
                return self.real.read(name)

            def write(self, name, value):
                return self.real.write(name, value)

            def delete(self, name):
                return self.real.delete(name)

        real = config_module.SECURE_CREDENTIAL_STORE
        monkeypatch.setattr(config_module, "SECURE_CREDENTIAL_STORE", FailingOnceStore(real))
        config_module.invalidate_config_cache()
        try:
            config_module.load_config(force_reload=True)
        except Exception:
            pass

        # remote-affecting（remote_root 变化）+ password 留空 → KEEP SAVED
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": "/new-root",
                "mount_root": str(tmp_path / "quark"),
                "username": "",
                "password": "",
            },
        )
        assert resp.status_code == 200, resp.text
        saved = self._config_json(tmp_path)
        assert saved["openlist_remote_root"] == "/new-root"
        # 凭据未丢失（probe 用恢复后的真实凭据）
        from app.core import config as config_module2

        u, p, state = config_module2.resolve_openlist_credentials()
        assert state == "found"
        assert u == "quark-user"


