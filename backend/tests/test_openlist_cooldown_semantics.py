"""OpenList 冷却语义：区分「连不上」与「远端风控」，并让用户操作能自救。

修复的实测故障：用户忘记启动 OpenList → 浏览失败被当成"疑似触发访问保护"
→ 来源进入 30 分钟冷却（持久化）→ 即使把服务重新打开，浏览、刷新、重试
全部零请求被拒，怎么点都出不来。

新的语义边界：
- 连接层失败（服务没启动/端口无人监听/DNS 失败）→ ``unreachable``，
  **不进入冷却**（请求根本没到达远端，冷却保护不了任何东西），如实报错；
- 对端可达但 5xx / 超时 / 网络抖动 → 仍然是 transient，达到阈值后冷却
  （保护批量扫描，不因本次修复而放松）；
- 用户显式操作（浏览/刷新/检查连接）不被**非滥用类**冷却拦截；
  真正滥用信号（risk_control / rate_limit）依然拦截，且不可被手动解除。
"""

from __future__ import annotations

import httpx
import pytest
from app.integrations.openlist.client import OpenListClient
from app.integrations.openlist.governor import OpenListRequestGovernor
from app.integrations.openlist.models import (
    OpenListNetworkError,
    OpenListServerError,
    OpenListUnreachableError,
)
from app.media_v4.sources import health as source_health


def _make_client(handler, **kwargs) -> OpenListClient:
    """MockTransport 客户端；注入快速 governor，避免 1 req/s 拖慢用例。"""

    kwargs.setdefault("governor", OpenListRequestGovernor(rate_per_second=1000))
    return OpenListClient(
        "https://ol.example.com",
        "user",
        "secret-pass",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_unreachable_failures_never_enter_cooldown():
    source_id = "conn-unreachable"

    for _ in range(10):
        source_health.record_failure(source_id, "unreachable")

    record = source_health.get_health(source_id)
    assert record.state != source_health.STATE_COOLING_DOWN
    assert record.in_cooldown is False
    assert record.reason_kind == "unreachable", "原因仍要如实记录，便于 UI 说明"
    assert source_health.peek_request_allowed(source_id)[0] is True


def test_reachable_failures_still_enter_cooldown():
    """反向保护：可达但对端异常（network/timeout/5xx）仍按阈值冷却。"""

    source_id = "conn-transient"

    for _ in range(source_health.TRANSIENT_FAILURE_THRESHOLD):
        source_health.record_failure(source_id, "network")

    record = source_health.get_health(source_id)
    assert record.state == source_health.STATE_COOLING_DOWN
    assert record.reason_kind == "network"
    assert record.in_cooldown is True


def test_interactive_admission_bypasses_transient_cooldown_only():
    transient = "conn-bypass-transient"
    source_health.enter_cooldown(transient, reason_kind="network", cooldown_seconds=1800)

    assert source_health.peek_request_allowed(transient)[0] is False, "后台批量仍被拦截"
    assert source_health.peek_request_allowed(transient, interactive=True)[0] is True
    assert source_health.can_request(transient, interactive=True)[0] is True
    # 交互放行不消费探针、不改状态
    assert source_health.get_health(transient).state == source_health.STATE_COOLING_DOWN

    abuse = "conn-bypass-abuse"
    source_health.enter_cooldown(abuse, reason_kind="risk_control", cooldown_seconds=1800)
    assert source_health.peek_request_allowed(abuse, interactive=True)[0] is False
    assert source_health.can_request(abuse, interactive=True)[0] is False


def test_clear_cooldown_clears_transient_and_refuses_abuse():
    transient = "conn-clear-transient"
    source_health.enter_cooldown(transient, reason_kind="timeout", cooldown_seconds=1800)

    assert source_health.clear_cooldown(transient) is True
    cleared = source_health.get_health(transient)
    assert cleared.state == source_health.STATE_HEALTHY
    assert cleared.cooldown_until == 0.0

    abuse = "conn-clear-abuse"
    source_health.enter_cooldown(abuse, reason_kind="risk_control", cooldown_seconds=21600)

    assert source_health.clear_cooldown(abuse) is False, "风控冷却不能被手动解除"
    assert source_health.get_health(abuse).in_cooldown is True


def test_connect_error_is_unreachable_and_never_cools_down():
    """端到端：连接被拒（OpenList 没开）不得把来源冻住。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _make_client(handler, max_attempts=1)

    for _ in range(5):
        with pytest.raises(OpenListNetworkError) as excinfo:  # 父类语义保持兼容
            client.list_dir("/")
        assert isinstance(excinfo.value, OpenListUnreachableError)
        assert excinfo.value.kind == "unreachable"

    record = source_health.get_health(client._conn_key)
    assert record.state != source_health.STATE_COOLING_DOWN, "服务没开不该触发来源冷却"
    assert source_health.peek_request_allowed(client._conn_key)[0] is True


def test_5xx_is_server_error_and_still_counts_toward_breaker():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": 503, "message": "busy"})

    client = _make_client(handler, max_attempts=1)

    with pytest.raises(OpenListServerError) as excinfo:
        client.list_dir("/")

    assert excinfo.value.kind == "server_error"
    assert isinstance(excinfo.value, OpenListNetworkError)
