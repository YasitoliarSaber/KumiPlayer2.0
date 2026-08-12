# -*- coding: utf-8 -*-
"""OpenList 客户端聚焦测试。

使用 httpx.MockTransport 模拟 OpenList 服务端，覆盖：
登录、分页、401 单次重登、429 Retry-After、超时重试、
恶意条目名、Token 不泄露、URL 校验、字段白名单。
"""

import json

import httpx
import pytest

from app.integrations.openlist.client import (
    OpenListClient,
    join_remote_path,
    normalize_remote_path,
    validate_entry_name,
    validate_server_url,
)
from app.integrations.openlist.governor import OpenListRequestGovernor
from app.integrations.openlist.models import (
    OpenListAuthError,
    OpenListEntry,
    OpenListError,
    OpenListNetworkError,
    OpenListPermissionError,
    OpenListRateLimitedError,
    OpenListRedirectError,
    OpenListRiskControlError,
    OpenListTimeoutError,
    OpenListValidationError,
)


def _json_response(status: int = 200, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload or {}, request=httpx.Request("POST", "http://test"))


def _fs_list_payload(path: str, entries: list[dict], total: int | None = None) -> dict:
    return {"code": 200, "message": "success", "data": {"content": entries, "total": total or len(entries)}}


def _entry(name: str, is_dir: bool = False, size: int | None = None, modified: int | None = None, **extra) -> dict:
    item = {"name": name, "is_dir": is_dir, "size": size, "modified": modified}
    item.update(extra)
    return item


def make_client(handler, **kwargs) -> OpenListClient:
    transport = httpx.MockTransport(handler)
    return OpenListClient(
        "https://ol.example.com",
        "user",
        "secret-pass",
        transport=transport,
        # 限速语义由 test_openlist_governor.py 单独覆盖：这里注入快速 governor，
        # 避免共享全局单例 1 req/s 拖慢既有用例（限速器本身不参与断言）。
        governor=OpenListRequestGovernor(rate_per_second=1000),
        **kwargs,
    )


# ============================================================
# URL 与名称校验
# ============================================================

class TestUrlValidation:
    def test_https_any_host_ok(self):
        ok, _ = validate_server_url("https://ol.example.com:5244/")
        assert ok

    def test_http_public_host_rejected(self):
        ok, reason = validate_server_url("http://ol.example.com")
        assert not ok and "公网 HTTP" in reason

    def test_http_loopback_allowed(self):
        ok, _ = validate_server_url("http://127.0.0.1:5244")
        assert ok
        ok, _ = validate_server_url("http://localhost:5244")
        assert ok

    def test_http_private_lan_allowed(self):
        ok, _ = validate_server_url("http://192.168.1.10:5244")
        assert ok
        ok, _ = validate_server_url("http://10.0.0.8:5244")
        assert ok

    def test_ftp_rejected(self):
        ok, _ = validate_server_url("ftp://ol.example.com")
        assert not ok

    def test_userinfo_rejected(self):
        ok, reason = validate_server_url("https://user:pass@ol.example.com")
        assert not ok and "用户名或密码" in reason

    def test_query_fragment_rejected(self):
        assert not validate_server_url("https://ol.example.com/?a=1")[0]
        assert not validate_server_url("https://ol.example.com/#frag")[0]

    def test_empty_rejected(self):
        assert not validate_server_url("")[0]


class TestPathNameValidation:
    def test_normalize_remote_path(self):
        assert normalize_remote_path("/夸克网盘/动画/") == "/夸克网盘/动画"
        assert normalize_remote_path("夸克网盘/动画") == "/夸克网盘/动画"
        assert normalize_remote_path("") == "/"

    def test_normalize_rejects_parent_segments(self):
        with pytest.raises(OpenListValidationError):
            normalize_remote_path("/夸克网盘/../动画")

    def test_entry_name_rejects_dangerous(self):
        for name in ("..", ".", "a/../b", "a\\b", "a\x00b", "a<b", "a:b", "a?b", "a*b", "a|b", "a\"b"):
            with pytest.raises(OpenListValidationError):
                validate_entry_name(name)

    def test_entry_name_rejects_windows_trailing(self):
        with pytest.raises(OpenListValidationError):
            validate_entry_name("dir.")
        with pytest.raises(OpenListValidationError):
            validate_entry_name("dir..")

    def test_entry_name_strips_whitespace(self):
        assert validate_entry_name("  视频.mkv  ") == "视频.mkv"

    def test_join_remote_path(self):
        assert join_remote_path("/夸克网盘", "动画") == "/夸克网盘/动画"
        assert join_remote_path("/", "动画") == "/动画"


# ============================================================
# 登录
# ============================================================

class TestLogin:
    def test_login_success_returns_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/auth/login"
            body = request.read().decode("utf-8")
            assert '"username":"user"' in body and '"password":"secret-pass"' in body
            return _json_response(200, {"code": 200, "message": "success", "data": {"token": "jwt-token-1"}})

        client = make_client(handler)
        assert client.login() == "jwt-token-1"

    def test_login_failure_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, {"code": 401, "message": "wrong password", "data": None})

        client = make_client(handler)
        with pytest.raises(OpenListAuthError):
            client.login()

    def test_login_token_never_leaks_in_error(self):
        """服务端错误即使回传，也不得把 Token 或密码带进异常消息。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, {"code": 403, "message": "forbidden", "data": None})

        client = make_client(handler)
        with pytest.raises(OpenListAuthError) as exc:
            client.login()
        assert "secret-pass" not in str(exc.value)
        assert "jwt" not in str(exc.value)


# ============================================================
# fs/list
# ============================================================

class TestListDir:
    def test_list_dir_sends_authorization_and_caps_per_page(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            body = json.loads(request.read())
            seen["body"] = body
            return _json_response(
                200,
                _fs_list_payload("/动画", [_entry("视频.mkv", size=1024, modified=1700000000)]),
            )

        client = make_client(handler)
        client._token = "jwt-token-1"
        page = client.list_dir("/动画", page=1, per_page=500)
        assert seen["auth"] == "jwt-token-1"
        assert seen["body"]["path"] == "/动画"
        assert seen["body"]["per_page"] == 100  # 上限封顶
        assert page.total == 1
        assert page.entries[0].name == "视频.mkv"
        assert page.entries[0].size == 1024
        assert page.entries[0].modified == 1700000000.0
        assert page.entries[0].remote_path == "/动画/视频.mkv"

    def test_list_dir_discards_non_whitelist_fields(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                200,
                _fs_list_payload(
                    "/",
                    [
                        _entry(
                            "视频.mkv",
                            size=10,
                            modified=1700000000,
                            path="/storage/internal/secret",
                            sign="sign-abc",
                            thumb="http://cdn/thumb",
                            hashinfo="md5:abc",
                            id="id-1",
                        )
                    ],
                ),
            )

        client = make_client(handler)
        client._token = "t"
        entry = client.list_dir("/").entries[0]
        assert isinstance(entry, OpenListEntry)
        assert entry.name == "视频.mkv"
        assert entry.remote_path == "/视频.mkv"  # 派生路径，不使用服务端 path
        assert not hasattr(entry, "sign")
        assert not hasattr(entry, "hashinfo")

    def test_malicious_entry_name_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _fs_list_payload("/", [_entry("../escape")]))

        client = make_client(handler)
        client._token = "t"
        with pytest.raises(OpenListValidationError):
            client.list_dir("/")

    def test_401_triggers_single_relogin_retry(self):
        """Token 失效时进程内单次重登后重试一次，不写磁盘。"""
        login_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                login_calls.append(1)
                return _json_response(200, {"code": 200, "message": "success", "data": {"token": "fresh-token"}})
            auth = request.headers.get("authorization")
            if auth != "fresh-token":
                return _json_response(200, {"code": 401, "message": "token invalid", "data": None})
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        client = make_client(handler)
        client._token = "stale-token"
        page = client.list_dir("/")
        assert len(login_calls) == 1
        assert [e.name for e in page.entries] == ["ok.mkv"]

    def test_401_relogin_failure_raises_auth(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return _json_response(200, {"code": 401, "message": "bad", "data": None})
            return _json_response(200, {"code": 401, "message": "bad", "data": None})

        client = make_client(handler)
        client._token = "stale"
        with pytest.raises(OpenListAuthError):
            client.list_dir("/")

    def test_429_respects_retry_after_then_succeeds(self):
        sleeps = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return _json_response(200, {"code": 200, "message": "success", "data": {"token": "t"}})
            if len(sleeps) == 0:
                return httpx.Response(429, headers={"retry-after": "2"}, request=request)
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        client = make_client(handler, sleep=sleeps.append)
        page = client.list_dir("/")
        assert sleeps == [2.0]
        assert [e.name for e in page.entries] == ["ok.mkv"]

    def test_429_exhausted_raises_rate_limited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "1"}, request=request)

        client = make_client(handler, max_attempts=2, sleep=lambda _: None)
        with pytest.raises(OpenListRateLimitedError):
            client.list_dir("/")

    def test_timeout_retries_then_raises(self):
        sleeps = []

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout")

        client = make_client(handler, max_attempts=3, sleep=sleeps.append)
        with pytest.raises(OpenListTimeoutError):
            client.list_dir("/")
        assert len(sleeps) == 2  # 前两次重试等待

    def test_connection_error_raises_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = make_client(handler, max_attempts=2, sleep=lambda _: None)
        with pytest.raises(OpenListNetworkError):
            client.list_dir("/")

    def test_redirect_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://evil.example.com"}, request=request)

        client = make_client(handler)
        with pytest.raises(OpenListRedirectError):
            client.list_dir("/")

    def test_403_raises_permission(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, {"code": 403, "message": "no permission", "data": None})

        client = make_client(handler)
        client._token = "t"
        with pytest.raises(OpenListPermissionError):
            client.list_dir("/")

    def test_error_message_never_contains_secret(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, {"code": 500, "message": "secret-pass leaked", "data": None})

        client = make_client(handler)
        client._token = "t"
        with pytest.raises(Exception) as exc:
            client.list_dir("/")
        assert "secret-pass" not in str(exc.value)


# ============================================================
# 风控拦截页（模块 1：OpenList / 网盘访问安全与风控保护）
# ============================================================

class TestRiskControl:
    """405 风控 HTML 页 → risk_control，且不继续自动重试。"""

    _ALIYUN_HTML = (
        "<!DOCTYPE html><html><head><title>访问被阻断</title></head><body>"
        "<script src='https://errors.aliyun.com/robots/blocked'></script>"
        "<p>访问被阻断</p></body></html>"
    )

    def _make_405_html_handler(self, calls: list):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(
                405,
                content=self._ALIYUN_HTML.encode("utf-8"),
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request("POST", "http://test"),
            )
        return handler

    def test_405_aliyun_html_raises_risk_control(self):
        calls: list = []
        client = make_client(self._make_405_html_handler(calls))
        client._token = "t"
        with pytest.raises(OpenListRiskControlError):
            client.list_dir("/")
        assert calls == [1]  # 立即失败，无第二、第三次自动重试

    def test_405_risk_control_message_is_safe_text(self):
        """对外只返回安全文本：不包含 HTML、URL、Token、Authorization。"""
        calls: list = []
        client = make_client(self._make_405_html_handler(calls))
        client._token = "t"
        with pytest.raises(OpenListRiskControlError) as exc:
            client.list_dir("/")
        message = str(exc.value)
        assert "疑似触发访问保护" in message
        assert "errors.aliyun.com" not in message
        assert "<html" not in message
        assert "jwt" not in message.lower()
        assert "authorization" not in message.lower()

    def test_405_html_with_blocked_marker_raises_risk_control(self):
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(
                405,
                content="<html><body>访问被阻断，请稍后再试</body></html>".encode("utf-8"),
                headers={"content-type": "text/html"},
                request=httpx.Request("POST", "http://test"),
            )

        client = make_client(handler)
        client._token = "t"
        with pytest.raises(OpenListRiskControlError):
            client.list_dir("/")
        assert calls == [1]

    def test_405_json_body_not_risk_control(self):
        """405 但 content-type 不是 HTML：保持普通错误语义（不误判风控）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(405, {"code": 405, "message": "method not allowed", "data": None})

        client = make_client(handler)
        client._token = "t"
        with pytest.raises(OpenListError) as exc:
            client.list_dir("/")
        assert not isinstance(exc.value, OpenListRiskControlError)
        assert exc.value.kind != "risk_control"

    def test_405_html_without_markers_not_risk_control(self):
        """405 HTML 但没有任何风控特征：不归为 risk_control。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                405,
                content="<html><body>not allowed</body></html>".encode("utf-8"),
                headers={"content-type": "text/html"},
                request=httpx.Request("POST", "http://test"),
            )

        client = make_client(handler)
        client._token = "t"
        with pytest.raises(OpenListError) as exc:
            client.list_dir("/")
        assert not isinstance(exc.value, OpenListRiskControlError)

    def test_risk_control_does_not_retry_even_with_max_attempts(self):
        """风控优先级高于重试：即便允许 3 次尝试也只发 1 个请求。"""
        calls: list = []
        client = make_client(self._make_405_html_handler(calls), max_attempts=3)
        client._token = "t"
        with pytest.raises(OpenListRiskControlError):
            client.list_dir("/")
        assert calls == [1]

    def test_login_405_aliyun_raises_risk_control(self):
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(
                405,
                content=self._ALIYUN_HTML.encode("utf-8"),
                headers={"content-type": "text/html"},
                request=httpx.Request("POST", "http://test"),
            )

        client = make_client(handler)
        with pytest.raises(OpenListRiskControlError):
            client.login()
        assert calls == [1]


# ============================================================
# 模块 1 阶段 B：请求最终结果写入 source_health
# ============================================================

class TestSourceHealthReporting:
    """客户端最终结果按连接键上报 source_health（临时 SQLite 隔离）。"""

    @pytest.fixture(autouse=True)
    def db(self, tmp_path, monkeypatch):
        from app.db.database import close_connection, init_db

        db_path = tmp_path / "health.db"
        monkeypatch.setattr("app.db.database._db_path", db_path)
        import app.db.database as db_mod

        if hasattr(db_mod._local, "connection"):
            db_mod._local.connection = None
        init_db()
        yield
        close_connection()

    def test_success_list_dir_records_healthy(self):
        from app.catalog import source_health

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        client = make_client(handler)
        client._token = "t"
        page = client.list_dir("/")
        assert page.total == 1
        record = source_health.get_health(client._conn_key)
        assert record.state == "healthy"
        assert record.last_success_at > 0
        assert not record.in_cooldown

    def test_429_exhausted_records_cooling_down(self):
        """429 最终失败（max_attempts=1 不再重试）→ rate_limit 冷却。"""
        from app.catalog import source_health

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "1"}, request=request)

        client = make_client(handler, max_attempts=1, sleep=lambda _: None)
        with pytest.raises(OpenListRateLimitedError):
            client.list_dir("/")
        record = source_health.get_health(client._conn_key)
        assert record.state == "cooling_down"
        assert record.reason_kind == "rate_limit"
        assert record.in_cooldown

    def test_risk_control_records_cooling_down(self):
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

        client = make_client(handler)
        client._token = "t"
        with pytest.raises(OpenListRiskControlError):
            client.list_dir("/")
        record = source_health.get_health(client._conn_key)
        assert record.state == "cooling_down"
        assert record.reason_kind == "risk_control"
        assert record.in_cooldown

    def test_timeout_failure_accumulates_transient(self):
        """transient（timeout）不立即冷却：记录连续失败但保持可请求。"""
        from app.catalog import source_health

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout")

        client = make_client(handler, max_attempts=1, sleep=lambda _: None)
        with pytest.raises(OpenListTimeoutError):
            client.list_dir("/")
        record = source_health.get_health(client._conn_key)
        assert record.reason_kind == "timeout"
        assert record.consecutive_failures == 1
        assert not record.in_cooldown  # 未达阈值，不冷却
