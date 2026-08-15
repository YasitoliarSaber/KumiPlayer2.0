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

    def __init__(self, *, available: bool, read_state_value: str):
        self.available = available
        self._read_state_value = read_state_value

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

        def get_me(self):
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
