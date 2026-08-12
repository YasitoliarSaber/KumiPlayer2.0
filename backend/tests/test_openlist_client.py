# -*- coding: utf-8 -*-
"""OpenList 客户端聚焦测试。

使用 httpx.MockTransport 模拟 OpenList 服务端，覆盖：
登录、分页、401 单次重登、429 Retry-After、超时重试、
恶意条目名、Token 不泄露、URL 校验、字段白名单。
"""

import json

import httpx
import pytest

from app.catalog import source_health as _source_health_module
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
    OpenListNotFoundError,
    OpenListPermissionError,
    OpenListRateLimitedError,
    OpenListRedirectError,
    OpenListRiskControlError,
    OpenListSourceCoolingDownError,
    OpenListTimeoutError,
    OpenListValidationError,
)

#: R1（source_health 新语义）是否已落地：irrelevant kinds（auth/permission/
#: not_found/validation/redirect/scan_limit）不累计不冷却 + 单探针原子准入。
#: 未落地时相关用例跳过，由父会话在 R1 完成后统一验证。
_R1_IRRELEVANT_LANDED = bool(
    getattr(_source_health_module, "BREAKER_IRRELEVANT_KINDS", None)
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
    # 限速语义由 test_openlist_governor.py 单独覆盖：默认注入快速 governor，
    # 避免共享全局单例 1 req/s 拖慢既有用例（限速器本身不参与断言）；
    # 测试可显式传入自定义 governor（如计数子类）覆盖默认。
    kwargs.setdefault("governor", OpenListRequestGovernor(rate_per_second=1000))
    return OpenListClient(
        "https://ol.example.com",
        "user",
        "secret-pass",
        transport=transport,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """文件级 DB 隔离：客户端每次物理请求前都会查 source_health。

    没有独立 DB 的测试会落到共享的 .pytest_runtime/data/kumiplayer.db
    （含历史冷却记录），导致 can_request 误判 cooling_down。
    """
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "openlist_client.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


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

    def test_429_first_response_raises_no_hidden_retry(self):
        """429 第一次响应即结束：不再 sleep+continue 隐藏重试。"""
        calls = []
        sleeps = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, headers={"retry-after": "2"}, request=request)

        client = make_client(handler, max_attempts=3, sleep=sleeps.append)
        client._token = "t"
        with pytest.raises(OpenListRateLimitedError) as exc:
            client.list_dir("/")
        assert len(calls) == 1  # 物理请求只有 1 次
        assert sleeps == []  # 没有任何隐藏退避等待
        assert exc.value.retry_after == 2.0

    def test_429_exhausted_raises_rate_limited(self):
        """429 语义变更后：max_attempts 不改变行为，第一次响应即失败。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, headers={"retry-after": "1"}, request=request)

        client = make_client(handler, max_attempts=2, sleep=lambda _: None)
        client._token = "t"
        with pytest.raises(OpenListRateLimitedError):
            client.list_dir("/")
        assert len(calls) == 1

    def test_500_retries_all_through_governor(self):
        """5xx 保留有限重试：三次物理请求每次都经过 governor acquire。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(500, request=httpx.Request("POST", "http://test"))

        class _CountingGovernor(OpenListRequestGovernor):
            def __init__(self):
                super().__init__(rate_per_second=1000)
                self.acquire_calls = 0

            def acquire(self, conn_key):
                self.acquire_calls += 1
                return super().acquire(conn_key)

        governor = _CountingGovernor()
        client = make_client(handler, max_attempts=3, sleep=lambda _: None, governor=governor)
        client._token = "t"
        with pytest.raises(OpenListNetworkError):
            client.list_dir("/")
        assert len(calls) == 3
        assert governor.acquire_calls == 3  # 每个物理 attempt 都经过限速器

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

    def test_probe_401_relogin_recovers_and_retries_request(self):
        """cooldown 到期后的唯一 probe 收到 401 时，应先解除 probe 再登录并重试。"""
        from app.catalog import source_health

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)

            if request.url.path == "/api/auth/login":
                return _json_response(
                    200,
                    {
                        "code": 200,
                        "message": "success",
                        "data": {"token": "fresh-token"},
                    },
                )

            if request.url.path == "/api/fs/list":
                auth = request.headers.get("authorization")
                if auth != "fresh-token":
                    return _json_response(
                        200,
                        {
                            "code": 401,
                            "message": "token invalid",
                            "data": None,
                        },
                    )
                return _json_response(
                    200,
                    _fs_list_payload("/", [_entry("ok.mkv")]),
                )

            raise AssertionError(f"unexpected request: {request.url.path}")

        client = make_client(handler)
        client._token = "stale-token"

        # 建立一个已经到期的 cooldown。
        # 第一次 list_dir() 的最终 can_request() 会把它原子转换成 probe。
        source_health.enter_cooldown(
            client._conn_key,
            reason_kind="risk_control",
            cooldown_seconds=1,
            now=1000.0,
        )

        page = client.list_dir("/")

        assert [entry.name for entry in page.entries] == ["ok.mkv"]

        # 必须严格是：
        # probe fs/list -> 401
        # login -> fresh token
        # retry fs/list -> 200
        assert calls == [
            "/api/fs/list",
            "/api/auth/login",
            "/api/fs/list",
        ]

        health = source_health.get_health(client._conn_key)
        assert health.state == "healthy"
        assert not health.in_cooldown


    def test_probe_401_does_not_relogin_after_new_risk_cooldown(self, monkeypatch):
        """probe 收到 401 后若同时出现新的 risk_control，login 不得穿透 cooldown。"""
        from app.catalog import source_health

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)

            if request.url.path == "/api/auth/login":
                raise AssertionError(
                    "risk_control cooldown 已建立，login 不应该发出物理请求"
                )

            if request.url.path == "/api/fs/list":
                return _json_response(
                    200,
                    {
                        "code": 401,
                        "message": "token invalid",
                        "data": None,
                    },
                )

            raise AssertionError(f"unexpected request: {request.url.path}")

        client = make_client(handler)
        client._token = "stale-token"

        # 先放入一个已经到期的 cooldown，让第一次 fs/list 成为唯一 probe。
        source_health.enter_cooldown(
            client._conn_key,
            reason_kind="risk_control",
            cooldown_seconds=1,
            now=1000.0,
        )

        original_report_failure = client._report_failure

        def report_failure_with_new_risk(kind: str) -> None:
            # 先执行生产逻辑：
            # probe + auth → healthy
            original_report_failure(kind)

            if kind == "auth":
                # 模拟 401 返回以后、真正 login 之前，
                # 另一个并发请求刚刚触发新的 405 风控。
                source_health.record_failure(
                    client._conn_key,
                    "risk_control",
                )

        monkeypatch.setattr(
            client,
            "_report_failure",
            report_failure_with_new_risk,
        )

        with pytest.raises(OpenListSourceCoolingDownError):
            client.list_dir("/")

        # 只能发生第一次 probe fs/list。
        # 新 cooldown 建立以后，login 的 peek 必须直接拦截，
        # /api/auth/login 绝不能真正进入 transport。
        assert calls == ["/api/fs/list"]

        health = source_health.get_health(client._conn_key)
        assert health.state == "cooling_down"
        assert health.reason_kind == "risk_control"
        assert health.in_cooldown

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


# ============================================================
# 模块 1 Review Fix（R2）：网络准入层（最后一道门）+ 冷却零请求
# ============================================================

class TestNetworkAdmission:
    """每次物理 attempt 前的 source_health 准入：冷却中零网络请求。"""


    def test_cooling_down_list_dir_zero_transport_calls(self):
        """冷却中 list_dir 直接抛 OpenListSourceCoolingDownError，物理请求数 == 0。"""
        from app.catalog import source_health

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        client = make_client(handler)
        client._token = "t"
        source_health.enter_cooldown(
            client._conn_key, reason_kind="risk_control", cooldown_seconds=3600,
        )
        with pytest.raises(OpenListSourceCoolingDownError) as exc:
            client.list_dir("/")
        assert calls == []  # 零物理请求
        assert exc.value.kind == "source_cooling_down"
        assert "访问保护" in str(exc.value)

    def test_cooling_down_login_zero_transport_calls(self):
        """冷却中 login 同样在准入处拦截，零物理请求。"""
        from app.catalog import source_health

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _json_response(200, {"code": 200, "data": {"token": "t"}})

        client = make_client(handler)
        source_health.enter_cooldown(
            client._conn_key, reason_kind="rate_limit", cooldown_seconds=3600,
        )
        with pytest.raises(OpenListSourceCoolingDownError):
            client.login()
        assert calls == []

    def test_cooldown_expiry_probe_allows_request(self):
        """冷却到期后单探针放行：list_dir 内部第一次 can_request 转 probe 并发请求。"""
        from app.catalog import source_health

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        client = make_client(handler)
        client._token = "t"
        source_health.enter_cooldown(
            client._conn_key, reason_kind="risk_control",
            cooldown_seconds=100, now=1000.0,
        )
        # 真实 now 已超过冷却窗口：客户端内部第一次 can_request 原子占用
        # 探针（返回 True），请求正常发出；探针成功后回 healthy。
        page = client.list_dir("/")
        assert page.total == 1
        assert len(calls) == 1
        record = source_health.get_health(client._conn_key)
        assert record.state == "healthy"  # 探针成功后回 healthy

    def test_probe_is_single_consumer(self):
        """冷却到期单探针：第一个调用者占用后，并发调用者仍被拒绝。"""
        from app.catalog import source_health

        client = make_client(lambda request: _json_response(200))
        client._token = "t"
        source_health.enter_cooldown(
            client._conn_key, reason_kind="risk_control",
            cooldown_seconds=100, now=1000.0,
        )
        allowed, record = source_health.can_request(client._conn_key)
        assert allowed and record.state == "probe"
        # 探针已被占用：第二个调用者（如并发线程/下一个目录）被拒绝
        allowed2, _ = source_health.can_request(client._conn_key)
        assert not allowed2

    def test_429_then_next_call_blocked_at_admission(self):
        """429 触发冷却后，下一次 list_dir 在准入处被拦（零请求）。"""
        from app.catalog import source_health

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, headers={"retry-after": "1"}, request=request)

        client = make_client(handler, max_attempts=1, sleep=lambda _: None)
        client._token = "t"
        with pytest.raises(OpenListRateLimitedError):
            client.list_dir("/")
        assert len(calls) == 1
        assert source_health.get_health(client._conn_key).in_cooldown
        # 冷却中第二次调用：准入处直接拒绝，不再发请求
        with pytest.raises(OpenListSourceCoolingDownError):
            client.list_dir("/")
        assert len(calls) == 1


@pytest.mark.skipif(
    not _R1_IRRELEVANT_LANDED,
    reason="依赖 R1：source_health irrelevant kinds 未落地，由父会话在 R1 完成后统一验证",
)
class TestIrrelevantKindsNoCooldown:
    """403/404/auth 连续失败不进入来源冷却（R1 契约：irrelevant kinds 不累计）。"""


    def _assert_no_cooldown(self, client):
        from app.catalog import source_health

        record = source_health.get_health(client._conn_key)
        assert not record.in_cooldown
        assert record.consecutive_failures == 0

    def test_403_times_three_no_cooldown(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, {"code": 403, "message": "no", "data": None})

        client = make_client(handler)
        client._token = "t"
        for _ in range(3):
            with pytest.raises(OpenListPermissionError):
                client.list_dir("/")
        self._assert_no_cooldown(client)

    def test_404_times_three_no_cooldown(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, {"code": 404, "message": "gone", "data": None})

        client = make_client(handler)
        client._token = "t"
        for _ in range(3):
            with pytest.raises(OpenListNotFoundError):
                client.list_dir("/")
        self._assert_no_cooldown(client)

    def test_auth_times_three_no_cooldown(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return _json_response(200, {"code": 401, "message": "bad", "data": None})
            return _json_response(200, {"code": 401, "message": "bad", "data": None})

        client = make_client(handler)
        client._token = "stale"
        for _ in range(3):
            with pytest.raises(OpenListAuthError):
                client.list_dir("/")
        self._assert_no_cooldown(client)


# ============================================================
# 模块 1 最终补丁（R3）：冷却期准入拒绝零副作用 + 探针 404 解锁
# ============================================================

class TestFinalPatchCooldownAdmission:
    """最终补丁回归（规划员点名）：

    1. risk_control 冷却中连续 10 次本地准入拒绝（OpenListSourceCoolingDown
       Error）：cooldown_until / reason_kind / consecutive_failures 完全
       不变，transport 调用数 == 0（双保险：client 外层 re-raise +
       record_failure('source_cooling_down') NO-OP）；
    2. 冷却到期 → probe → 真实请求返回 404（irrelevant）→ breaker 恢复
       healthy → 下一个请求有资格执行（连接未锁死）。
    """

    def test_ten_admission_denials_do_not_mutate_health(self):
        """冷却中连续 10 次 list_dir：健康状态完全不变，零物理请求。"""
        from app.catalog import source_health

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        client = make_client(handler)
        client._token = "t"
        source_health.enter_cooldown(
            client._conn_key, reason_kind="risk_control", cooldown_seconds=6 * 3600,
        )
        before = source_health.get_health(client._conn_key)
        assert before.state == "cooling_down"
        assert before.reason_kind == "risk_control"

        for _ in range(10):
            with pytest.raises(OpenListSourceCoolingDownError):
                client.list_dir("/")

        after = source_health.get_health(client._conn_key)
        assert calls == []  # 零物理请求
        assert after.state == "cooling_down"
        assert after.reason_kind == before.reason_kind  # 仍 risk_control
        assert after.cooldown_until == before.cooldown_until  # 未被刷新
        assert after.consecutive_failures == before.consecutive_failures  # 不累计
        assert after.last_failure_at == before.last_failure_at  # 连失败时间都不动

    def test_probe_404_recovers_healthy_then_next_request_allowed(self):
        """冷却到期探针返回 404：breaker 回 healthy，后续请求有资格执行。"""
        from app.catalog import source_health

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if len(calls) == 1:
                # 探针请求：远端可达但目录不存在（business error，非风控）
                return httpx.Response(404, request=request)
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        client = make_client(handler)
        client._token = "t"
        source_health.enter_cooldown(
            client._conn_key, reason_kind="risk_control",
            cooldown_seconds=100, now=1000.0,
        )

        # 第一次请求（冷却已到期）：can_request 抢占 probe → 真实请求 → 404
        with pytest.raises(OpenListNotFoundError):
            client.list_dir("/")
        record = source_health.get_health(client._conn_key)
        assert record.state == "healthy"  # probe + not_found(irrelevant) → healthy
        assert record.consecutive_failures == 0
        assert record.cooldown_until == 0
        assert not record.in_cooldown

        # 下一个请求有资格执行（breaker 已恢复，连接未锁死）
        page = client.list_dir("/")
        assert page.total == 1
        assert len(calls) == 2


# ============================================================
# 模块 1 最终准入顺序：governor 等待期间进入 cooldown → transport 0 次
# ============================================================

class TestFinalAdmissionGovernorOrder:
    """规划员第四次审核点名（P0 准入竞态）：Client 最终网络准入顺序。

    旧顺序：can_request（拿 admission）→ governor.acquire（等待 1 秒）→
    HTTP——governor 等待期间另一请求返回 405 进入冷却，本请求已拿过
    admission，睡完仍会发。

    新顺序（每个 physical attempt）：peek（只读预检、不消费探针）→
    governor.acquire（限速等待）→ can_request（真正最终准入、消费探针）
    → HTTP：最后一道门在 governor 之后，等待期间新建立的冷却在最终准入
    处拦截，transport 零调用。

    以下测试用自定义阻塞 governor 精确制造交错（Event 同步，禁 sleep
    猜时序）：请求已通过 peek 并进入 governor 等待 → 另一线程把来源置为
    risk_control 冷却 → 放行 governor → 最终 can_request 拒绝 → 断言
    transport calls==0 且抛 OpenListSourceCoolingDownError。
    """

    def test_governor_wait_cooldown_blocks_transport(self):
        import threading

        from app.catalog import source_health
        from app.db import database as db_mod

        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        class _BlockingGovernor(OpenListRequestGovernor):
            """acquire 处 Event 阻塞：模拟限速等待期间另一请求触发冷却。"""

            def __init__(self):
                super().__init__(rate_per_second=1000)
                self.entered = threading.Event()
                self.release = threading.Event()

            def acquire(self, conn_key):
                self.entered.set()
                if not self.release.wait(timeout=10):
                    raise TimeoutError("governor acquire timed out")
                return super().acquire(conn_key)

        governor = _BlockingGovernor()
        client = make_client(handler, governor=governor)
        client._token = "t"
        conn_key = client._conn_key

        errors: list[BaseException] = []

        def run_request():
            try:
                client.list_dir("/")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        thread = threading.Thread(target=run_request)
        thread.start()
        assert governor.entered.wait(timeout=10)  # 已通过 peek、进入 governor 等待
        # 请求等待限速期间：另一请求返回 405/429 → 来源进入冷却
        source_health.record_failure(conn_key, "risk_control")
        governor.release.set()  # 放行 governor
        thread.join(timeout=10)

        assert calls == []  # 物理请求 0 次：governor 之后的最终准入拦截
        assert len(errors) == 1
        assert isinstance(errors[0], OpenListSourceCoolingDownError)
        assert errors[0].kind == "source_cooling_down"
        assert source_health.get_health(conn_key).in_cooldown

    def test_peek_deny_does_not_enter_governor(self):
        """明确未到期冷却：peek 直接拒绝，不进入限速队列、零请求。"""
        import threading

        from app.catalog import source_health

        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _json_response(200, _fs_list_payload("/", [_entry("ok.mkv")]))

        class _CountingGovernor(OpenListRequestGovernor):
            def __init__(self):
                super().__init__(rate_per_second=1000)
                self.acquire_calls = 0

            def acquire(self, conn_key):
                self.acquire_calls += 1
                return super().acquire(conn_key)

        governor = _CountingGovernor()
        client = make_client(handler, governor=governor)
        client._token = "t"
        source_health.enter_cooldown(
            client._conn_key, reason_kind="risk_control", cooldown_seconds=3600,
        )

        with pytest.raises(OpenListSourceCoolingDownError):
            client.list_dir("/")

        assert calls == []  # 零物理请求
        assert governor.acquire_calls == 0  # peek 拒绝不进入限速队列
        # probe 未被消费：冷却保持 cooling_down（peek 只读）
        assert source_health.get_health(client._conn_key).state == "cooling_down"
