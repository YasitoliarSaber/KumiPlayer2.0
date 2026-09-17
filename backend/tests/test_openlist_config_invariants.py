"""OpenList 配置保存的不变量（配置环节审计 F1/F3 的回归门）。

真因与危害：
- F3：`remote_changed` 把用户名/密码变化也算作"远端变更"，命中即清空
  `openlist_routes` → **只改密码会静默丢掉用户的内容来源路由**，之后注册扫描
  直接 409（前端还不同步显示）。
- F1：后端按**规范化后**的值判断变更，前端按原始字符串判断；两者不等价时
  （如把地址从 `http://host` 改成 `http://host/dav/`）后端跳过探测，前端却显示
  "连接正常"。因此保存响应必须带 `verified`，只有真的探测成功才允许显示已连接。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import openlist_v4
from app.core.config import load_config, save_config
from app.integrations.openlist.providers import OpenListRouteConfig
from app.main import app

SERVER = "http://localhost:5244"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _seed(routes: list[OpenListRouteConfig], *, password: str = "old-pass") -> None:
    current = load_config()
    current.openlist_server_url = SERVER
    current.openlist_remote_root = "/"
    current.openlist_mount_root = "K:\\"
    current.openlist_username = "admin"
    current.openlist_password = password
    current.openlist_routes = list(routes)
    save_config(current)


def _route(route_id: str = "route-a", provider: str = "quark") -> OpenListRouteConfig:
    return OpenListRouteConfig(
        route_id=route_id, label="夸克网盘", remote_prefix="/夸克网盘", provider_id=provider
    )


def _patch_probe(monkeypatch, *, ok: bool = True) -> list[dict]:
    calls: list[dict] = []

    def fake_probe(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return SimpleNamespace(
            ok=ok,
            code="connected" if ok else "server_unavailable",
            phase="root",
            message="连接成功" if ok else "服务暂时不可用",
        )

    monkeypatch.setattr(openlist_v4, "probe_openlist_connection", fake_probe)
    monkeypatch.setattr(
        openlist_v4, "resolve_openlist_credentials", lambda: ("admin", "old-pass", "ok")
    )
    return calls


def _payload(**overrides) -> dict:
    body = {
        "server_url": SERVER,
        "remote_root": "/",
        "mount_root": "K:\\",
        "username": "admin",
        "password": "new-pass",
    }
    body.update(overrides)
    return body


def test_password_change_keeps_content_routes(client, monkeypatch):
    """只改密码绝不允许清空用户的内容来源路由。"""

    _seed([_route()])
    calls = _patch_probe(monkeypatch)

    response = client.post("/api/openlist/config", json=_payload())

    assert response.status_code == 200, response.text
    assert calls, "凭据变化必须重新验证"
    routes = load_config().openlist_routes
    assert len(routes) == 1 and routes[0].provider_id == "quark", "路由被误清空"


def test_endpoint_change_resets_routes_and_says_so(client, monkeypatch):
    """远端地址变化才作废路由，并且必须在返回文案里说明。"""

    _seed([_route()])
    _patch_probe(monkeypatch)

    response = client.post(
        "/api/openlist/config", json=_payload(server_url="http://localhost:5300")
    )

    assert response.status_code == 200, response.text
    assert load_config().openlist_routes == []
    assert "路由已重置" in response.json()["message"]
    assert response.json()["routes_reset"] is True


def test_save_without_probe_reports_verified_false(client, monkeypatch):
    """没有真的探测过就不许让前端显示"连接正常"。"""

    _seed([_route()])
    calls = _patch_probe(monkeypatch)

    # 地址用带尾斜杠的等价写法、密码原样重填 → 后端判定"无变更"，不探测
    response = client.post(
        "/api/openlist/config",
        json=_payload(server_url=SERVER + "/", password="old-pass"),
    )

    assert response.status_code == 200, response.text
    assert calls == [], "等价写法不该触发探测（前提）"
    assert response.json()["verified"] is False, "未探测却声称已验证"


def test_save_with_probe_reports_verified_true(client, monkeypatch):
    _seed([_route()])
    calls = _patch_probe(monkeypatch)

    response = client.post("/api/openlist/config", json=_payload())

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert response.json()["verified"] is True


def test_test_connection_does_not_claim_unconfigured_when_store_unavailable(client, monkeypatch):
    """凭据管理器不可用 ≠ 尚未配置。"""

    _seed([_route()])
    monkeypatch.setattr(
        openlist_v4, "resolve_openlist_credentials", lambda: ("", "", "unavailable")
    )

    body = client.post("/api/openlist/test-connection", json={"server_url": SERVER}).json()

    assert body["ok"] is False
    assert body["code"] == "credential_store_unavailable"
    assert "凭据管理器" in body["message"]


def test_skipped_verification_still_reports_verified_false(client, monkeypatch):
    _seed([_route()])
    calls = _patch_probe(monkeypatch)

    response = client.post(
        "/api/openlist/config",
        json=_payload(server_url="http://localhost:5300", skip_verification=True),
    )

    assert response.status_code == 200, response.text
    assert calls == []
    assert response.json()["verified"] is False
    assert "未验证" in response.json()["message"]
