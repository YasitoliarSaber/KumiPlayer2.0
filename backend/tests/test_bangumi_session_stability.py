"""Bangumi 会话稳定化 CP1 回归：本地会话与远程认证状态解耦

覆盖点：
1. ``WindowsCredentialStore.read_state`` 三态（found / not_found / unavailable）；
2. ``GET /session`` 只读本地（Credential 三态 + 账户快照），0 远程请求；
3. 凭据不存在 → signed_out；凭据存储不可读 → unavailable（保守不判退出）；
4. ``DELETE /token`` 清 token 并清账户快照。

测试禁止触碰真实 Windows Credential Manager：_read_credential_state 只在
CONFIG_FILE is None 且 SECURE_CREDENTIAL_STORE.available 时才查凭据库，因此
本文件所有用例都显式替换 app.api.bangumi.SECURE_CREDENTIAL_STORE 与
CONFIG_FILE，保证凭据库回退分支要么走 FakeStore、要么被 CONFIG_FILE 门挡死。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import load_config, save_config
from app.core.credential_store import CredentialStoreError, WindowsCredentialStore
from app.integrations.bangumi import (
    BangumiAccountSnapshot,
    get_account_snapshot_path,
    save_account_snapshot,
)

SESSION_URL = "/api/integrations/bangumi/session"


class _FakeCredentialStore:
    """替代 SECURE_CREDENTIAL_STORE：绝不让测试触碰真实 Windows Credential Manager。"""

    def __init__(self, *, available: bool, read_state_value: str, token: str = "fake-token"):
        self.available = available
        self._read_state_value = read_state_value
        self._token = token

    def read(self, name: str) -> str:
        if self._read_state_value == "unavailable":
            raise CredentialStoreError("模拟凭据存储故障")
        if self._read_state_value == "found":
            return self._token
        return ""

    def read_state(self, name: str) -> str:
        return self._read_state_value


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _write_token(config, token: str) -> None:
    config.bangumi_access_token = token
    save_config(config)
    load_config(force_reload=True)


def _mock_credential_store_closed(monkeypatch, tmp_path):
    """把凭据库回退分支关死：替换 SECURE_CREDENTIAL_STORE 为不可用 FakeStore。

    _credential_storage_enabled 在测试配置（CONFIG_FILE 指向 tmp）下本就不
    启用，这里再防御一层，保证测试与平台无关、永不触碰真实凭据库。
    """
    from app.api import bangumi as bangumi_api

    monkeypatch.setattr(
        bangumi_api,
        "SECURE_CREDENTIAL_STORE",
        _FakeCredentialStore(available=False, read_state_value="not_found"),
    )


def test_read_state_tri_state(monkeypatch):
    """read_state 把 read 的三种结果归一为 found / not_found / unavailable。"""
    store = WindowsCredentialStore()  # Windows 上会初始化 ctypes，无副作用

    monkeypatch.setattr(store, "read", lambda name: "secret")
    assert store.read_state("bangumi_access_token") == "found"

    monkeypatch.setattr(store, "read", lambda name: "")
    assert store.read_state("bangumi_access_token") == "not_found"

    def _raise_read(name):
        raise CredentialStoreError("模拟故障")

    monkeypatch.setattr(store, "read", _raise_read)
    assert store.read_state("bangumi_access_token") == "unavailable"


def test_session_local_restore_zero_remote_requests(client, tmp_path, monkeypatch):
    """核心测试 A：GET /session 只读本地 Credential + 快照，0 个远程请求。"""
    from app.api import bangumi as bangumi_api

    _mock_credential_store_closed(monkeypatch, tmp_path)

    _write_token(load_config(), "test-token")
    save_account_snapshot(BangumiAccountSnapshot(
        user_id=1001,
        username="hyakka",
        nickname="冰菓",
        avatar_url="http://x/a.jpg",
        auth_status="valid",
        connectivity="online",
        last_verified_at="2026-08-15T12:00:00+08:00",
        last_success_at="2026-08-15T12:00:00+08:00",
    ))

    class SpyBangumiClient:
        instances = []

        def __init__(self, *args, **kwargs):
            SpyBangumiClient.instances.append(self)

        def get_me(self, purpose: str = ""):
            raise AssertionError("session 不得发起远程请求")

    monkeypatch.setattr(bangumi_api, "BangumiClient", SpyBangumiClient)

    response = client.get(SESSION_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["credential_state"] == "found"
    assert data["credential_saved"] is True
    assert data["status"] == "connected"
    assert data["user"]["username"] == "hyakka"
    assert data["user"]["nickname"] == "冰菓"
    assert data["auth_status"] == "valid"
    assert data["connectivity"] == "online"
    assert SpyBangumiClient.instances == [], "GET /session 不得实例化 BangumiClient"


def test_session_without_credential_is_signed_out(client, tmp_path, monkeypatch):
    """不写 token → 本地会话判定为 signed_out，user 为 None。"""
    _mock_credential_store_closed(monkeypatch, tmp_path)

    response = client.get(SESSION_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["credential_state"] == "not_found"
    assert data["credential_saved"] is False
    assert data["status"] == "signed_out"
    assert data["user"] is None


def test_session_credential_unavailable_not_signed_out(client, tmp_path, monkeypatch):
    """测试 H：凭据存储不可读时保守不判退出，仍从快照恢复账户卡。"""
    from app.api import bangumi as bangumi_api

    save_account_snapshot(BangumiAccountSnapshot(username="hyakka", auth_status="valid"))

    # 模拟 Windows Credential Manager 暂时不可读：凭据存储“启用”但 read_state 报不可用
    import app.core.config as core_config

    monkeypatch.setattr(core_config, "_credential_storage_enabled", lambda: True)
    monkeypatch.setattr(
        bangumi_api,
        "SECURE_CREDENTIAL_STORE",
        _FakeCredentialStore(available=True, read_state_value="unavailable"),
    )

    response = client.get(SESSION_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["credential_state"] == "unavailable"
    assert data["credential_saved"] is True  # 保守不判退出
    assert data["status"] == "unavailable"  # 绝不是 signed_out
    assert data["user"]["username"] == "hyakka"  # 快照用户仍返回
    assert get_account_snapshot_path().exists(), "unavailable 时不得清理快照"


def test_delete_token_clears_credential_and_snapshot(client, tmp_path, monkeypatch):
    """测试 I：DELETE /token 清 token + 清快照，随后会话回到 signed_out。"""
    _mock_credential_store_closed(monkeypatch, tmp_path)

    _write_token(load_config(), "test-token")
    save_account_snapshot(BangumiAccountSnapshot(
        user_id=1001,
        username="hyakka",
        nickname="冰菓",
        auth_status="valid",
        connectivity="online",
    ))
    assert get_account_snapshot_path().exists()

    response = client.delete("/api/integrations/bangumi/token")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    response = client.get(SESSION_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["credential_state"] == "not_found"
    assert data["status"] == "signed_out"
    assert not get_account_snapshot_path().exists(), "DELETE /token 后快照应被清除"


# ---------------------------------------------------------------------------
# CP2 回归：POST /session/verify —— 远程验证只更新账户快照，
# 任何失败分类都保留 config token 与快照文件，绝不伪装成退出登录。
# ---------------------------------------------------------------------------

VERIFY_URL = "/api/integrations/bangumi/session/verify"


class _FakeBangumiClient:
    """可编程 Fake：行为由类属性控制，端点实例化次数由 instances 统计。"""

    instances: list = []
    me_payload: dict | None = None
    raise_error: Exception | None = None

    def __init__(self, *args, **kwargs):
        _FakeBangumiClient.instances.append(self)

    def get_me(self, purpose: str = ""):
        if _FakeBangumiClient.raise_error is not None:
            raise _FakeBangumiClient.raise_error
        return _FakeBangumiClient.me_payload


def _install_fake_client(monkeypatch, *, me=None, error=None) -> None:
    """把 app.api.bangumi.BangumiClient 换成行为可控的 _FakeBangumiClient。"""
    from app.api import bangumi as bangumi_api

    _FakeBangumiClient.instances = []
    _FakeBangumiClient.me_payload = me
    _FakeBangumiClient.raise_error = error
    monkeypatch.setattr(bangumi_api, "BangumiClient", _FakeBangumiClient)


def test_verify_success_refreshes_snapshot_and_user(client, tmp_path, monkeypatch):
    """verify 成功 → 快照刷新为 valid/online，用户资料完整，无错误记录。"""
    _mock_credential_store_closed(monkeypatch, tmp_path)
    _write_token(load_config(), "test-token")
    _install_fake_client(
        monkeypatch,
        me={
            "id": 1001,
            "username": "hyakka",
            "nickname": "冰菓",
            "avatar": {"large": "http://x/a.jpg"},
            "sign": "s",
        },
    )

    response = client.post(VERIFY_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["credential_state"] == "found"
    assert data["credential_saved"] is True
    assert data["status"] == "connected"
    assert data["auth_status"] == "valid"
    assert data["connectivity"] == "online"
    assert data["user"] is not None
    assert data["user"]["id"] == 1001
    assert data["user"]["username"] == "hyakka"
    assert data["user"]["nickname"] == "冰菓"
    assert data["user"]["sign"] == "s"
    assert data["user"]["avatar"].startswith("/api/integrations/bangumi/avatar?url=")
    assert data["last_http_status"] == 200
    assert data["last_verified_at"]
    assert data["last_success_at"]
    assert data["last_error_code"] == ""
    assert data["last_error_message"] == ""


def test_verify_401_marks_reauth_required_keeps_credential(client, tmp_path, monkeypatch):
    """401 → reauth_required + online；保留 config token 与快照文件。"""
    from app.integrations.bangumi import BangumiError

    _mock_credential_store_closed(monkeypatch, tmp_path)
    _write_token(load_config(), "test-token")
    _install_fake_client(
        monkeypatch,
        error=BangumiError("401", status_code=401, error_code="auth_invalid"),
    )

    response = client.post(VERIFY_URL)
    assert response.status_code == 200  # 认证错误仍返回 session payload，不抛 5xx
    data = response.json()
    assert data["auth_status"] == "reauth_required"
    assert data["connectivity"] == "online"
    assert data["last_http_status"] == 401
    assert data["last_error_code"] == "auth_invalid"
    assert data["last_error_message"]
    assert load_config(force_reload=True).bangumi_access_token == "test-token"
    assert get_account_snapshot_path().exists(), "401 不得清理快照文件"


def test_verify_403_forbidden_not_signed_out(client, tmp_path, monkeypatch):
    """403 → forbidden（保留凭据，绝不当成 signed_out）。"""
    from app.integrations.bangumi import BangumiError

    _mock_credential_store_closed(monkeypatch, tmp_path)
    _write_token(load_config(), "test-token")
    _install_fake_client(
        monkeypatch,
        error=BangumiError("403", status_code=403, error_code="forbidden"),
    )

    response = client.post(VERIFY_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["connectivity"] == "forbidden"
    assert data["status"] != "signed_out"
    assert data["credential_saved"] is True
    assert load_config(force_reload=True).bangumi_access_token == "test-token"


def test_verify_429_rate_limited_no_retry_storm(client, tmp_path, monkeypatch):
    """429 → rate_limited；端点只实例化一次客户端（无立即重试）。"""
    from app.integrations.bangumi import BangumiError

    _mock_credential_store_closed(monkeypatch, tmp_path)
    _write_token(load_config(), "test-token")
    _install_fake_client(
        monkeypatch,
        error=BangumiError("429", status_code=429, error_code="rate_limited", retry_after="60"),
    )

    response = client.post(VERIFY_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["connectivity"] == "rate_limited"
    assert len(_FakeBangumiClient.instances) == 1, "429 不得触发立即重试"
    assert load_config(force_reload=True).bangumi_access_token == "test-token"


def test_verify_5xx_server_error_keeps_credential(client, tmp_path, monkeypatch):
    """5xx → server_error；token 保留，快照不清理。"""
    from app.integrations.bangumi import BangumiError

    _mock_credential_store_closed(monkeypatch, tmp_path)
    _write_token(load_config(), "test-token")
    _install_fake_client(
        monkeypatch,
        error=BangumiError("500", status_code=500, error_code="server_error"),
    )

    response = client.post(VERIFY_URL)
    assert response.status_code == 200
    data = response.json()
    assert data["connectivity"] == "server_error"
    assert data["last_http_status"] == 500
    assert data["last_error_code"] == "server_error"
    assert load_config(force_reload=True).bangumi_access_token == "test-token"
    assert get_account_snapshot_path().exists(), "5xx 不得清理快照文件"


def test_verify_timeout_and_proxy_offline(client, tmp_path, monkeypatch):
    """timeout / proxy_unavailable → offline；连续两次 verify 均保留 token。"""
    from app.integrations.bangumi import BangumiError

    _mock_credential_store_closed(monkeypatch, tmp_path)
    _write_token(load_config(), "test-token")

    _install_fake_client(monkeypatch, error=BangumiError("timeout", error_code="timeout"))
    first = client.post(VERIFY_URL)
    assert first.status_code == 200
    assert first.json()["connectivity"] == "offline"

    _install_fake_client(
        monkeypatch, error=BangumiError("proxy", error_code="proxy_unavailable")
    )
    second = client.post(VERIFY_URL)
    assert second.status_code == 200
    assert second.json()["connectivity"] == "offline"

    assert load_config(force_reload=True).bangumi_access_token == "test-token"
    assert get_account_snapshot_path().exists(), "offline 分类同样保留凭据与快照"


def test_observe_auth_success_updates_snapshot():
    """_observe_auth("success") 直接把快照推进为 valid/online。"""
    from app.integrations.bangumi import _observe_auth, load_account_snapshot

    save_account_snapshot(BangumiAccountSnapshot(
        username="old",
        auth_status="reauth_required",
        connectivity="offline",
    ))
    _observe_auth("success", 200)

    snapshot = load_account_snapshot()
    assert snapshot.auth_status == "valid"
    assert snapshot.connectivity == "online"
    assert snapshot.last_success_at
    assert snapshot.last_error_code == ""
    assert snapshot.last_http_status == 200


class TestRequestDiagnostics:
    """CP4：Bangumi 请求诊断日志——脱敏红线（绝不含 Token）与字段完整性。"""

    def _fake_httpx(self, monkeypatch, statuses: list[int]):
        """按顺序返回给定 HTTP 状态的 Fake httpx.Client。"""
        import httpx
        from app.integrations import bangumi as bg

        class FakeResponse:
            def __init__(self, status_code: int):
                self.status_code = status_code
                self.headers = {"Retry-After": "60"} if status_code == 429 else {}
                self.content = b'{"description": "boom"}' if status_code >= 400 else b'{"id": 1}'

            @property
            def text(self) -> str:
                return self.content.decode("utf-8")

            def json(self):
                import json as _json
                try:
                    return _json.loads(self.content)
                except ValueError:
                    return {}

        class FakeClient:
            def __init__(self, **kwargs):
                self._statuses = statuses
                self.headers_seen: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def request(self, method, url, **kwargs):
                self.headers_seen = kwargs.get("headers") or {}
                return FakeResponse(self._statuses.pop(0))

        monkeypatch.setattr(httpx, "Client", FakeClient)
        return FakeClient

    def _log_text(self) -> str:
        from app.integrations import bangumi as bg

        log_path = bg.get_data_dir() / "logs" / "bangumi_requests.log"
        if not log_path.exists():
            return ""
        return log_path.read_text(encoding="utf-8")

    def test_request_log_redacts_token_on_failure(self, monkeypatch):
        """401 失败日志：含 status/error_code/retry_after/purpose，绝不含 Token。"""
        import pytest
        from app.integrations import bangumi as bg

        fake = self._fake_httpx(monkeypatch, [401])
        client = bg.BangumiClient(
            access_token="SECRET-TOKEN-12345", base_url="https://api.bgm.tv"
        )
        with pytest.raises(bg.BangumiError) as exc:
            client.get_me(purpose="session_verify")
        assert exc.value.error_code == bg.AUTH_INVALID

        content = self._log_text()
        assert "SECRET-TOKEN-12345" not in content, "日志不得包含 Token"
        assert "Authorization" not in content, "日志不得包含 Authorization 头"
        assert "Bearer" not in content
        assert "GET /v0/me purpose=session_verify" in content
        assert "status=401" in content
        assert "error=auth_invalid" in content
        assert "credential_present=true" in content
        assert "proxy_enabled=false" in content
        assert "duration_ms=" in content

    def test_request_log_redacts_token_on_success_and_429(self, monkeypatch):
        """成功与 429 路径同样脱敏；429 记录 Retry-After。"""
        from app.integrations import bangumi as bg

        self._fake_httpx(monkeypatch, [200, 429])
        client = bg.BangumiClient(
            access_token="SECRET-TOKEN-12345", base_url="https://api.bgm.tv"
        )
        client.get_me(purpose="session_verify")  # 200
        try:
            client.get_collection("tester", 1)  # 429 → collection_read
        except bg.BangumiError:
            pass

        content = self._log_text()
        assert "SECRET-TOKEN-12345" not in content
        assert "Bearer" not in content
        assert "GET /v0/me purpose=session_verify status=200 error=ok" in content
        assert "status=429" in content
        assert "error=rate_limited" in content
        assert "retry_after=60" in content
        assert "purpose=collection_read" in content

    def test_classify_purpose_covers_main_endpoints(self):
        """purpose 自动分类覆盖方案十七列出的调用来源。"""
        from app.integrations import bangumi as bg

        assert bg._classify_purpose("GET", "/v0/me") == "me_lookup"
        assert bg._classify_purpose("POST", "/v0/search/subjects") == "subject_search"
        assert bg._classify_purpose("GET", "/v0/users/tester/collections/1") == "collection_read"
        assert bg._classify_purpose("PATCH", "/v0/users/-/collections/1/episodes") == "episode_sync"
        assert bg._classify_purpose("PUT", "/v0/users/-/collections/-/episodes/123") == "episode_sync"
        assert bg._classify_purpose("GET", "/v0/episodes") == "episode_sync"
        assert bg._classify_purpose("POST", "/v0/users/-/collections/1") == "collection_write"
        assert bg._classify_purpose("GET", "/v0/unknown") == "unknown"


class TestCredentialRecoveryAndSafety:
    """REWORK P0-1/P0-2：凭据恢复无需重启；读取失败绝不导致删除。"""

    def test_verify_uses_recovered_token_without_restart(self, client, tmp_path, monkeypatch):
        """CM 启动时不可读、之后恢复 → verify 直接使用恢复后的真实 token。"""
        import app.core.config as core_config
        from app.api import bangumi as bangumi_api
        from app.core.credential_store import CredentialStoreError

        class RecoveringStore:
            available = True

            def __init__(self):
                self.read_attempts = 0

            def read(self, name: str) -> str:
                self.read_attempts += 1
                if self.read_attempts == 1:
                    raise CredentialStoreError("模拟 CM 暂时不可读")
                return "recovered-token"

            def read_state(self, name: str) -> str:
                try:
                    return "found" if self.read(name) else "not_found"
                except CredentialStoreError:
                    return "unavailable"

        store = RecoveringStore()
        monkeypatch.setattr(bangumi_api, "SECURE_CREDENTIAL_STORE", store)
        monkeypatch.setattr(core_config, "_credential_storage_enabled", lambda: True)

        # 启动期：config cache 空 token（hydration 失败），CM 不可读 → unavailable
        assert not load_config().bangumi_access_token
        r1 = client.get(SESSION_URL)
        assert r1.status_code == 200
        assert r1.json()["credential_state"] == "unavailable"

        # CM 恢复后：verify 必须拿到真实 token（不依赖陈旧 config cache）
        captured: dict = {}

        class CapturingClient:
            def __init__(self, **kwargs):
                captured["token"] = kwargs.get("access_token", "")

            def get_me(self, purpose: str = ""):
                assert captured["token"] == "recovered-token", "verify 必须使用恢复后的真实 token"
                return {"id": 7, "username": "hyakka", "nickname": "冰菓", "avatar": {}, "sign": "s"}

        monkeypatch.setattr(bangumi_api, "BangumiClient", CapturingClient)
        r2 = client.post(VERIFY_URL)
        assert r2.status_code == 200
        data = r2.json()
        assert data["auth_status"] == "valid"
        assert data["user"]["username"] == "hyakka"
        assert store.read_attempts >= 2

    def test_save_unrelated_config_keeps_credential_after_read_error(self, monkeypatch):
        """hydration 读取失败后保存无关配置：不得删除安全凭据，显式清除才删。"""
        import app.core.config as core_config
        from app.core.credential_store import CredentialStoreError

        class Store:
            available = True

            def __init__(self):
                self.values = {"bangumi_access_token": "real-token"}
                self.deleted: list[str] = []

            def read(self, name: str) -> str:
                raise CredentialStoreError("模拟 CM 读取失败")

            def read_state(self, name: str) -> str:
                try:
                    self.read(name)
                except CredentialStoreError:
                    return "unavailable"
                return "not_found"

            def write(self, name: str, value: str) -> None:
                self.values[name] = value

            def delete(self, name: str) -> None:
                self.deleted.append(name)
                self.values.pop(name, None)

        store = Store()
        monkeypatch.setattr(core_config, "SECURE_CREDENTIAL_STORE", store)
        monkeypatch.setattr(core_config, "_credential_storage_enabled", lambda: True)

        # config 缓存为空 token（读取失败场景）
        config = load_config()
        assert not config.bangumi_access_token
        # 保存无关配置（卡片样式）
        config.series_card_image_mode = "fanart"
        save_config(config)
        assert store.deleted == [], "保存无关配置不得触发凭据删除"
        assert store.values.get("bangumi_access_token") == "real-token", "凭据必须保留"

        # 显式清除（用户退出）仍然工作
        save_config(config, cleared_keys={"bangumi_access_token"})
        assert "bangumi_access_token" in store.deleted
        assert "bangumi_access_token" not in store.values


class TestRealHttpStatusInDiagnostics:
    """REWORK P1-2：诊断日志 status 与真实响应一致（200/204/429）。"""

    def test_request_log_uses_real_http_status(self, monkeypatch, tmp_path):
        from app.integrations import bangumi as bg

        def _fake_httpx(statuses):
            import httpx

            class FakeResponse:
                def __init__(self, status_code: int):
                    self.status_code = status_code
                    self.headers = {"Retry-After": "60"} if status_code == 429 else {}
                    self.content = b'{"description": "boom"}' if status_code >= 400 else b'{"id": 1}'

                @property
                def text(self) -> str:
                    return self.content.decode("utf-8")

                def json(self):
                    import json as _json
                    try:
                        return _json.loads(self.content)
                    except ValueError:
                        return {}

            class FakeClient:
                def __init__(self, **kwargs):
                    self._statuses = statuses

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def request(self, method, url, **kwargs):
                    return FakeResponse(self._statuses.pop(0))

            monkeypatch.setattr(httpx, "Client", FakeClient)

        _fake_httpx([204, 200, 429])
        client = bg.BangumiClient(access_token="tok", base_url="https://api.bgm.tv")
        client.get_me()  # 204（空响应）
        client.get_me()  # 200
        try:
            client.get_collection("tester", 1)  # 429
        except bg.BangumiError:
            pass

        log_path = bg.get_data_dir() / "logs" / "bangumi_requests.log"
        content = log_path.read_text(encoding="utf-8")
        assert "GET /v0/me purpose=me_lookup status=204 error=ok" in content
        assert "GET /v0/me purpose=me_lookup status=200 error=ok" in content
        assert "status=429" in content
        assert "error=rate_limited" in content


class TestRuntimeCredentialResolver:
    """REWORK：统一运行时凭据解析——CM 恢复后普通 authenticated 业务请求
    无需重启即可使用真实 token；通用 CLEAR 只删指定凭据。"""

    def test_normal_authenticated_request_uses_recovered_token(self, monkeypatch):
        """config cache 空 + CM 先不可读后恢复 → 同一进程 BangumiClient()
        发起正常业务请求直接携带 recovered token 且成功。"""
        import httpx
        import app.core.config as core_config
        from app.core.credential_store import CredentialStoreError
        from app.integrations import bangumi as bg

        class RecoveringStore:
            available = True

            def __init__(self):
                self.read_attempts = 0

            def read(self, name: str) -> str:
                self.read_attempts += 1
                if self.read_attempts == 1:
                    raise CredentialStoreError("模拟 CM 暂时不可读")
                return "recovered-token"

            def read_state(self, name: str) -> str:
                try:
                    return "found" if self.read(name) else "not_found"
                except CredentialStoreError:
                    return "unavailable"

        store = RecoveringStore()
        monkeypatch.setattr(core_config, "SECURE_CREDENTIAL_STORE", store)
        monkeypatch.setattr(core_config, "_credential_storage_enabled", lambda: True)
        monkeypatch.setattr(bg, "SECURE_CREDENTIAL_STORE", store)

        # config cache 空 token（hydration 失败场景）
        assert not load_config().bangumi_access_token

        # CM 不可读期间：client 拿不到 token（不误判、不崩溃）
        client_before = bg.BangumiClient(base_url="https://api.bgm.tv")
        assert client_before.access_token == ""

        # CM 恢复后：同一进程普通业务请求立即拿到真实 token
        captured: dict = {}

        class FakeResponse:
            status_code = 200
            headers: dict = {}
            content = b'{"data": []}'

            @property
            def text(self) -> str:
                return self.content.decode("utf-8")

            def json(self):
                return {"data": []}

        class FakeHttpClient:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def request(self, method, url, **kwargs):
                captured["authorization"] = (kwargs.get("headers") or {}).get("Authorization", "")
                return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeHttpClient)

        client_after = bg.BangumiClient(base_url="https://api.bgm.tv")
        assert client_after.access_token == "recovered-token"
        result = client_after.get_collection("tester", 1)  # auth_required 正常业务请求
        assert result == {"data": []}
        assert captured["authorization"] == "Bearer recovered-token"
        assert store.read_attempts >= 2

    def test_clear_only_removes_requested_credential(self, monkeypatch):
        """cleared_keys 只删除指定凭据，其他 secure credential 完全不变。"""
        import app.core.config as core_config

        class Store:
            available = True

            def __init__(self):
                self.values = {
                    "bangumi_access_token": "bangumi-tok",
                    "tmdb_bearer_token": "tmdb-key",
                }
                self.deleted: list[str] = []

            def read(self, name: str) -> str:
                return self.values.get(name, "")

            def read_state(self, name: str) -> str:
                return "found" if self.values.get(name) else "not_found"

            def write(self, name: str, value: str) -> None:
                self.values[name] = value

            def delete(self, name: str) -> None:
                self.deleted.append(name)
                self.values.pop(name, None)

        store = Store()
        monkeypatch.setattr(core_config, "SECURE_CREDENTIAL_STORE", store)
        monkeypatch.setattr(core_config, "_credential_storage_enabled", lambda: True)

        config = load_config()
        save_config(config, cleared_keys={"bangumi_access_token"})
        assert store.deleted == ["bangumi_access_token"], "只允许删除被显式指定的凭据"
        assert "bangumi_access_token" not in store.values
        assert store.values["tmdb_bearer_token"] == "tmdb-key", "其他 secure credential 必须保持不变"
