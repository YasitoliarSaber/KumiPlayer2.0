"""OL-6：OpenList Connection Reliability 事故矩阵 E2E 回归门。

覆盖施工说明「OL-6 必须覆盖的事故矩阵」A–O：
A  saved good + pool bad          → test 用 saved 成功（见 test_openlist_api）
B  pool valid token + bad cand    → test 必失败（见 test_openlist_api）
C  password A→B                   → pool 替换（见 test_openlist_client）
D  T0 late 401 after T1           → T1 存活（见 test_openlist_client）
E  credential read error + save   → secret 保留（见 test_credential_storage）
F  credential write error         → 无 config mutation（见 test_credential_storage）
G  config write fail after secure → secure 回滚（见 test_credential_storage）
H  candidate root 改 + probe 403  → 旧 config/routes 保持（本文件）
I  local-only + offline           → save 成功（见 test_openlist_api）
J  login ok + root 403            → root_permission_denied（见 test_openlist_api）
K  429                            → rate_limit / cooldown，不循环 login（本文件）
L  405 风控 HTML                  → risk_control（见 test_openlist_client）
M  unrelated routes               → 失败 candidate save 不清（见 test_openlist_api）
N  response/log 零凭据            → 见各零泄露断言
O  相同配置二次保存                → 稳定无重复（见 test_openlist_api）
"""

import json

import httpx
import pytest

from app.integrations.openlist.client import (
    OpenListClient,
    clear_openlist_client_pool,
)
from app.integrations.openlist.connection import probe_openlist_connection
from app.integrations.openlist.governor import OpenListRequestGovernor


def _json_response(status: int = 200, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload or {}, request=httpx.Request("POST", "http://test"))


def _fs_list_payload(path: str, entries: list[dict]) -> dict:
    return {"code": 200, "message": "success", "data": {"content": entries, "total": len(entries)}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """DB 隔离 + pool 清理（与 test_openlist_client 一致）。"""
    from app.db.database import close_connection, init_db

    clear_openlist_client_pool()
    db_path = tmp_path / "reliability.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    clear_openlist_client_pool()
    close_connection()


def make_client(handler, **kwargs) -> OpenListClient:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("governor", OpenListRequestGovernor(rate_per_second=1000))
    return OpenListClient(
        "https://ol.example.com",
        "user",
        "secret-pass",
        transport=transport,
        **kwargs,
    )


# ============================================================
# H：候选 remote_root 变化 + probe 权限拒绝 → 旧 config/routes 保持
# ============================================================


class TestMatrixH:
    def test_probe_permission_denied_preserves_config_and_routes(self, tmp_path, monkeypatch):
        """登录成功但候选 root 403 → probe 失败 → 配置与路由全部保持旧值。"""
        from app.core import config as config_module

        config_file = tmp_path / "config.json"
        monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)
        config_module.invalidate_config_cache()

        from app.core.config import AppConfig

        config_module.save_config(AppConfig(
            openlist_server_url="https://ol.example.com:5244",
            openlist_remote_root="/old-root",
            openlist_mount_root="K:\\",
            openlist_username="user",
            openlist_password="pass",
        ))
        # 种子旧路由（直接写配置对象再保存，模拟已保存状态）
        from app.integrations.openlist.providers import OpenListRouteConfig

        cfg = config_module.load_config(force_reload=True)
        cfg.openlist_routes = [
            OpenListRouteConfig(route_id="r1", label="动画", remote_prefix="/old-root/动画",
                                provider_id="quark", enabled=True)
        ]
        config_module.save_config(cfg)
        routes_before = [r.remote_prefix for r in config_module.load_config(force_reload=True).openlist_routes]
        # 候选 /new-root 返回 403 权限拒绝
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return _json_response(200, {"code": 200, "data": {"token": "t1"}})
            body = json.loads(request.read().decode("utf-8"))
            if body.get("path") == "/new-root":
                return _json_response(403, {"code": 403})
            return _json_response(200, _fs_list_payload(body.get("path", "/"), []))

        # probe 使用独立 transport 的 client（注入 MockTransport）
        import app.integrations.openlist.connection as connection_mod

        original_client = connection_mod.OpenListClient
        try:
            connection_mod.OpenListClient = lambda *a, **k: make_client(handler)

            result = probe_openlist_connection(
                server_url="https://ol.example.com:5244",
                remote_root="/new-root",
                username="user",
                password="pass",
                allow_insecure_http=False,
            )
        finally:
            connection_mod.OpenListClient = original_client
        assert result.ok is False
        assert result.code == "root_permission_denied"

        # 旧配置与路由保持（probe 不产生任何 mutation）
        cfg = config_module.load_config(force_reload=True)
        assert cfg.openlist_remote_root == "/old-root"
        assert [r.remote_prefix for r in cfg.openlist_routes] == routes_before


# ============================================================
# K：429 触发 rate_limit / cooldown，且不循环 login
# ============================================================


class TestMatrixK:
    def test_429_raises_rate_limited_without_login_loop(self):
        """429 第一次响应即结束：不重试、不循环登录，直接 rate_limited。"""
        login_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                login_calls.append(request.url.path)
                return _json_response(200, {"code": 200, "data": {"token": "t1"}})
            return httpx.Response(429, headers={"retry-after": "1"}, request=request)

        client = make_client(handler)
        client.login()

        from app.integrations.openlist.models import OpenListRateLimitedError

        with pytest.raises(OpenListRateLimitedError):
            client.list_dir("/动画")
        # 429 不触发第二次登录
        assert len(login_calls) == 1

    def test_risk_control_405_maps_to_risk_control_and_no_retry(self):
        """405 风控 HTML → risk_control，不重试不重登。"""
        login_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                login_calls.append(request.url.path)
                return _json_response(200, {"code": 200, "data": {"token": "t1"}})
            return httpx.Response(
                405,
                content="<html><body>errors.aliyun.com 访问被阻断</body></html>".encode(),
                headers={"content-type": "text/html"},
                request=request,
            )

        client = make_client(handler)
        client.login()

        from app.integrations.openlist.models import OpenListRiskControlError

        with pytest.raises(OpenListRiskControlError):
            client.list_dir("/动画")
        assert len(login_calls) == 1

    def test_probe_maps_429_to_rate_limited(self):
        """probe 对 429 返回 rate_limited（前端据此显示「已暂停请求」）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return _json_response(200, {"code": 200, "data": {"token": "t1"}})
            return httpx.Response(429, headers={"retry-after": "5"}, request=request)

        # probe 使用独立 transport 的 client 不可直接注入 → 通过 connection 模块 monkeypatch
        import app.integrations.openlist.connection as connection_mod

        original_client = connection_mod.OpenListClient
        try:
            connection_mod.OpenListClient = lambda *a, **k: make_client(handler)
            result = probe_openlist_connection(
                server_url="https://ol.example.com",
                remote_root="/",
                username="user",
                password="secret-pass",
                allow_insecure_http=False,
            )
        finally:
            connection_mod.OpenListClient = original_client
        assert result.ok is False
        assert result.code == "rate_limited"


# ============================================================
# 并发探针：探针不进入 production pool（OL-1 不变量在 E2E 层复核）
# ============================================================


class TestProbePoolIsolation:
    def test_probe_never_touches_production_pool(self):
        """probe 构造的 client 不得进入 production pool（Fresh Probe 隔离）。"""
        from app.integrations.openlist.client import _CLIENT_POOL

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return _json_response(200, {"code": 200, "data": {"token": "t1"}})
            return _json_response(200, _fs_list_payload("/", []))

        import app.integrations.openlist.connection as connection_mod

        original_client = connection_mod.OpenListClient
        try:
            connection_mod.OpenListClient = lambda *a, **k: make_client(handler)
            result = probe_openlist_connection(
                server_url="https://ol.example.com",
                remote_root="/",
                username="user",
                password="secret-pass",
                allow_insecure_http=False,
            )
        finally:
            connection_mod.OpenListClient = original_client
        assert result.ok is True
        # pool 未被 probe 污染
        assert _CLIENT_POOL == {}
