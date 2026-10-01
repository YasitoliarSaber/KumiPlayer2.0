"""缓存不能覆盖当前连接失败；全部依赖为本地模拟。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.api import openlist_v4 as api


@pytest.fixture
def setup_browse(monkeypatch):
    monkeypatch.setattr(api, "load_config", lambda: SimpleNamespace(
        openlist_remote_root="/", openlist_server_url="https://openlist.example",
    ))
    monkeypatch.setattr(api, "_credentials", lambda _config=None: ("user", "password"))
    monkeypatch.setattr(api, "read_cache", lambda *_a, **_k: {
        "fresh": True, "entries": [{"name": "old", "is_dir": True}],
        "page": 1, "per_page": 100, "total": 1, "has_more": False,
        "fetched_at": 1, "expires_at": 9999999999,
    })
    client = Mock(side_effect=AssertionError("缓存/冷却状态不得访问远端"))
    monkeypatch.setattr(api, "_client", client)
    clear = Mock()
    monkeypatch.setattr(api.source_health, "clear_cooldown", clear)
    return client, clear


def test_cooling_down_rejects_even_fresh_cache(monkeypatch, setup_browse):
    monkeypatch.setattr(api.source_health, "peek_request_allowed", lambda *_a, **_k: (
        False, SimpleNamespace(reason_kind="risk_control", last_failure_at=2, last_success_at=1),
    ))
    with pytest.raises(HTTPException) as failure:
        api.browse(path="/", refresh=False)
    assert failure.value.status_code == 423
    setup_browse[0].assert_not_called()
    setup_browse[1].assert_not_called()


@pytest.mark.parametrize("kind", ["server_error", "unreachable", "auth"])
def test_last_network_failure_cannot_be_hidden_by_fresh_cache(monkeypatch, setup_browse, kind):
    monkeypatch.setattr(api.source_health, "peek_request_allowed", lambda *_a, **_k: (
        True, SimpleNamespace(reason_kind=kind, last_failure_at=2, last_success_at=1),
    ))
    with pytest.raises(HTTPException) as failure:
        api.browse(path="/", refresh=False)
    assert "连接" in failure.value.detail
    setup_browse[0].assert_not_called()


def test_cache_hit_does_not_claim_verified_connection(monkeypatch, setup_browse):
    monkeypatch.setattr(api.source_health, "peek_request_allowed", lambda *_a, **_k: (
        True, SimpleNamespace(reason_kind="", last_failure_at=0, last_success_at=1),
    ))
    result = api.browse(path="/", refresh=False)
    assert result["connection_state"] == "unverified"
    setup_browse[0].assert_not_called()


def test_read_failure_does_not_fall_back_to_stale_cache(monkeypatch, setup_browse):
    monkeypatch.setattr(api.source_health, "peek_request_allowed", lambda *_a, **_k: (
        True, SimpleNamespace(reason_kind="", last_failure_at=0, last_success_at=1),
    ))
    original = api.read_cache
    monkeypatch.setattr(api, "read_cache", lambda *a, **k: {**original(*a, **k), "fresh": False})
    monkeypatch.setattr(api, "_client", lambda *_a: object())
    def fail(*_a, **_k):
        raise HTTPException(400, "当前无法连接网盘")
    monkeypatch.setattr(api, "_page_payload", fail)
    with pytest.raises(HTTPException, match="当前无法连接网盘"):
        api.browse(path="/", refresh=False)


@pytest.mark.parametrize("checked", [False, True])
def test_openlist_response_is_verified_only_after_explicit_upstream_check(checked):
    result = api._browse_response(
        "/", "/", [], page=1, per_page=100, total=0, has_more=False,
        cache={"cached": False, "upstream_checked": checked},
    )
    assert result["connection_state"] == ("verified" if checked else "unverified")


def test_opening_browser_without_cache_does_not_request_the_account(monkeypatch, setup_browse):
    monkeypatch.setattr(api.source_health, "peek_request_allowed", lambda *_a, **_k: (
        True, SimpleNamespace(reason_kind="", last_failure_at=0, last_success_at=0),
    ))
    monkeypatch.setattr(api, "read_cache", lambda *_a, **_k: None)
    result = api.browse(path="/", cache_only=True)
    assert result["connection_state"] == "unverified"
    assert result["entries"] == []
    setup_browse[0].assert_not_called()
