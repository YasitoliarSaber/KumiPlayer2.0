# -*- coding: utf-8 -*-
"""模块 1 阶段 B：统一连接级请求限速（governor）聚焦测试。

覆盖：
- 两个 OpenListClient 默认共享同一 governor 单例；
- 同连接键串行化限速（间隔 >= 1/rate_per_second），不同连接键互不阻塞；
- governor_connection_key 匿名化（不含用户名/密码/地址明文，密码无关）；
- 客户端请求最终结果写入 source_health（成功 healthy / 405 风控冷却）。
"""

import time

import httpx
import pytest

from app.db.database import close_connection, init_db
from app.integrations.openlist.client import OpenListClient
from app.integrations.openlist.governor import (
    DEFAULT_RATE_PER_SECOND,
    OpenListRequestGovernor,
    get_governor,
    governor_connection_key,
)
from app.integrations.openlist.models import OpenListRiskControlError


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """临时 SQLite：governor 本身不依赖 DB，健康上报测试需要 source_health 表。"""
    db_path = tmp_path / "governor.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _json_response(status: int = 200, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload or {}, request=httpx.Request("POST", "http://test"))


def _fs_list_payload(path: str, entries: list[dict]) -> dict:
    return {"code": 200, "message": "success", "data": {"content": entries, "total": len(entries)}}


def _make_client(handler, password: str = "secret-pass", **kwargs) -> OpenListClient:
    return OpenListClient(
        "https://ol.example.com",
        "quark-user",
        password,
        transport=httpx.MockTransport(handler),
        governor=OpenListRequestGovernor(rate_per_second=1000),
        **kwargs,
    )


# ============================================================
# 单例与连接键
# ============================================================

class TestGovernorSingleton:
    def test_default_clients_share_same_governor(self):
        """未显式指定 governor 的客户端默认共享进程级单例。"""
        handler = lambda request: _json_response(200)
        client_a = OpenListClient(
            "https://ol.example.com", "u1", "p1",
            transport=httpx.MockTransport(handler),
        )
        client_b = OpenListClient(
            "https://ol.example.com", "u1", "p1",
            transport=httpx.MockTransport(handler),
        )
        assert client_a._governor is client_b._governor
        assert client_a._governor is get_governor()

    def test_get_governor_is_process_singleton(self):
        assert get_governor() is get_governor()
        assert isinstance(get_governor(), OpenListRequestGovernor)
        assert get_governor().rate_per_second == DEFAULT_RATE_PER_SECOND

    def test_explicit_governor_overrides_singleton(self):
        custom = OpenListRequestGovernor(rate_per_second=50)
        client = OpenListClient(
            "https://ol.example.com", "u", "p",
            transport=httpx.MockTransport(lambda request: _json_response(200)),
            governor=custom,
        )
        assert client._governor is custom


class TestConnectionKey:
    def test_password_independent(self):
        """不同密码、相同 server/username → 相同连接键（密码绝不进入 key）。"""
        handler = lambda request: _json_response(200)
        key_a = _make_client(handler, password="pass-1")._conn_key
        key_b = _make_client(handler, password="pass-2")._conn_key
        assert key_a == key_b

    def test_key_is_anonymized(self):
        """key 中不得出现用户名 / 密码 / 服务地址明文。"""
        key = governor_connection_key("https://ol.example.com", "quark-user")
        assert key == governor_connection_key("https://ol.example.com", "quark-user")
        assert "quark-user" not in key
        assert "ol.example.com" not in key
        assert key == key.lower() and len(key) == 64  # sha256 十六进制

    def test_different_username_different_key(self):
        assert (
            governor_connection_key("https://ol.example.com", "user-a")
            != governor_connection_key("https://ol.example.com", "user-b")
        )

    def test_different_server_different_key(self):
        assert (
            governor_connection_key("https://a.example.com", "u")
            != governor_connection_key("https://b.example.com", "u")
        )


# ============================================================
# acquire 限速语义
# ============================================================

class TestAcquire:
    def test_fast_rate_does_not_block(self):
        """rate_per_second 调大后（如 1000）acquire 不阻塞。"""
        governor = OpenListRequestGovernor(rate_per_second=1000)
        start = time.monotonic()
        governor.acquire("k1")
        governor.acquire("k1")
        assert time.monotonic() - start < 0.1

    def test_same_key_respects_interval(self):
        """调小（2/s）：同 key 两次 acquire 间隔接近 1/rate（0.5s），sleep 注入记录。"""
        sleeps: list[float] = []
        governor = OpenListRequestGovernor(rate_per_second=2.0, sleep=sleeps.append)
        governor.acquire("k1")
        governor.acquire("k1")
        assert len(sleeps) == 1
        assert sleeps[0] >= 0.45  # 1/2=0.5s，留少量抖动余量

    def test_different_keys_do_not_block_each_other(self):
        """不同连接键互不阻塞：key2 第一次 acquire 立即通过，不 sleep。"""
        sleeps: list[float] = []
        governor = OpenListRequestGovernor(rate_per_second=2.0, sleep=sleeps.append)
        governor.acquire("k1")
        governor.acquire("k2")  # 不同键：不等待
        assert sleeps == []

    def test_rate_zero_disables_limiting(self):
        governor = OpenListRequestGovernor(rate_per_second=0, sleep=lambda _: (_ for _ in ()).throw(AssertionError()))
        governor.acquire("k1")
        governor.acquire("k1")

    def test_runtime_rate_adjustment(self):
        """rate_per_second 运行时调整立即生效（测试调大后几乎不等待）。"""
        sleeps: list[float] = []
        governor = OpenListRequestGovernor(rate_per_second=2.0, sleep=sleeps.append)
        governor.acquire("k1")
        governor.rate_per_second = 1000.0
        governor.acquire("k1")
        assert len(sleeps) == 1
        assert sleeps[0] < 0.1  # 调大后等待时间从 0.5s 降到 1ms 量级


# ============================================================
# 健康上报（source_health 集成）
# ============================================================

class TestHealthReporting:
    def test_success_records_healthy(self):
        from app.catalog import source_health

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _fs_list_payload("/", [{"name": "ok.mkv", "is_dir": False}]))

        client = _make_client(handler)
        client._token = "t"
        client.list_dir("/")
        record = source_health.get_health(client._conn_key)
        assert record.state == "healthy"
        assert record.last_success_at > 0
        assert not record.in_cooldown

    def test_risk_control_failure_enters_cooldown(self):
        from app.catalog import source_health

        aliyun_html = (
            "<!DOCTYPE html><html><head><title>访问被阻断</title></head><body>"
            "<script src='https://errors.aliyun.com/robots/blocked'></script>"
            "<p>访问被阻断</p></body></html>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                405,
                content=aliyun_html.encode("utf-8"),
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request("POST", "http://test"),
            )

        client = _make_client(handler)
        client._token = "t"
        with pytest.raises(OpenListRiskControlError):
            client.list_dir("/")
        record = source_health.get_health(client._conn_key)
        assert record.in_cooldown
        assert record.reason_kind == "risk_control"
        assert record.state == "cooling_down"

    def test_login_success_records_healthy(self):
        from app.catalog import source_health

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, {"code": 200, "message": "success", "data": {"token": "t"}})

        client = _make_client(handler)
        client.login()
        record = source_health.get_health(client._conn_key)
        assert record.state == "healthy"
