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
    OpenListError,
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


def test_stuck_probe_is_reclaimable_after_timeout():
    """探针状态必须有超时兜底，否则来源会被永久锁死。

    复现路径：冷却到期 → 某次请求抢到唯一探针（state=probe）→ 该请求的进程/线程
    在回报结果前退出（崩溃、强杀、关应用、扫描线程异常）→ 这一行永远停在 probe，
    之后**所有**请求（含后台扫描与用户刷新）一律被拒，而且没有任何通道解除。
    """

    source_id = "conn-stuck-probe"
    source_health.enter_cooldown(source_id, reason_kind="network", cooldown_seconds=1, now=1000.0)

    allowed, record = source_health.can_request(source_id, now=2000.0)
    assert allowed is True and record.state == source_health.STATE_PROBE, "到期冷却应被抢占为探针"

    # 模拟"探针请求再也没回来"
    later = 2000.0 + 24 * 3600
    assert source_health.peek_request_allowed(source_id, now=later)[0] is True, (
        "探针超时后必须重新放行，否则来源被永久锁死"
    )
    assert source_health.can_request(source_id, now=later)[0] is True


def test_clear_cooldown_also_releases_a_stale_probe():
    """用户显式重试也要能解除卡住的探针（否则只能等超时）。"""

    source_id = "conn-stale-probe-clear"
    source_health.enter_cooldown(source_id, reason_kind="network", cooldown_seconds=1, now=1000.0)
    source_health.can_request(source_id, now=2000.0)

    assert source_health.clear_cooldown(source_id, now=2000.0 + 24 * 3600) is True
    record = source_health.get_health(source_id)
    assert record.state == source_health.STATE_HEALTHY


def test_rate_limit_cooldown_honours_retry_after():
    """服务端给了 Retry-After 就必须尊重，而不是写死 1 小时。"""

    honored = "conn-retry-after"
    source_health.record_failure(honored, "rate_limit", now=1000.0, retry_after=45.0)
    record = source_health.get_health(honored)
    assert record.state == source_health.STATE_COOLING_DOWN
    assert abs(record.cooldown_until - 1045.0) < 1.0, "应按 Retry-After=45s 冷却"

    default = "conn-retry-after-default"
    source_health.record_failure(default, "rate_limit", now=1000.0)
    assert abs(source_health.get_health(default).cooldown_until - 4600.0) < 1.0, "未给值时仍是 1h"

    capped = "conn-retry-after-huge"
    source_health.record_failure(capped, "rate_limit", now=1000.0, retry_after=10 ** 9)
    assert source_health.get_health(capped).cooldown_until <= 1000.0 + 6 * 3600, "异常巨大值要封顶"


def test_unhandled_4xx_is_not_retried_and_not_breaker_relevant():
    """未显式归属的 4xx：不重试，也不该被当成 unknown 计入熔断。"""

    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"code": 400, "message": "bad request"})

    client = _make_client(handler, max_attempts=3)

    with pytest.raises(OpenListError) as excinfo:
        client.list_dir("/x")

    assert len(calls) == 1, "4xx 是客户端错误，重试没有意义"
    assert excinfo.value.kind == "validation", "必须显式归属，不能落成 kind=error→unknown"

    for _ in range(5):
        with pytest.raises(OpenListError):
            client.list_dir("/x")
    record = source_health.get_health(client._conn_key)
    assert record.state != source_health.STATE_COOLING_DOWN, "4xx 不该触发来源冷却"


def test_missing_object_heuristic_ignores_storage_level_failures():
    """文案判定必须限定在目录列表语境，别把存储级故障误判成幽灵目录。"""

    assert OpenListClient._looks_like_missing_object(
        {"message": "failed get objs: failed get dir: object not found"}
    )
    assert not OpenListClient._looks_like_missing_object(
        {"message": "storage [quark] does not exist"}
    )
    assert not OpenListClient._looks_like_missing_object({"message": "no such file or directory"})


def test_body_level_5xx_is_retried_and_reported_as_server_error():
    """OpenList 用 HTTP 200 + body.code=5xx 表达上游驱动失败（夸克常见）。

    修复前它落进通用分支：不重试、kind=error（被 health 归一化成 unknown 计入
    熔断）、文案是无法行动的"请求失败（500）"——反复浏览就会被冻结 30 分钟。
    现在按 HTTP 5xx 同语义处理：有限重试 + server_error + 可行动文案。
    """

    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"code": 500, "message": "quark upstream error"})

    client = _make_client(handler, max_attempts=2)

    with pytest.raises(OpenListServerError) as excinfo:
        client.list_dir("/")

    assert len(calls) == 2, "body.code=5xx 必须重试一次（与 HTTP 5xx 同语义）"
    assert excinfo.value.kind == "server_error"
    assert "quark upstream error" not in str(excinfo.value), "上游原始 message 不得外泄"


def test_any_5xx_body_code_is_server_error_without_leaking_upstream_message():
    """任意 >=500 的业务码都按服务端错误处理，且绝不泄漏上游 message。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 999, "message": "internal detail: token=abc"})

    client = _make_client(handler, max_attempts=1)

    with pytest.raises(OpenListServerError) as excinfo:
        client.list_dir("/")

    assert excinfo.value.kind == "server_error"
    assert str(excinfo.value) == "OpenList 服务暂时不可用，请稍后重试"
    assert "token=abc" not in str(excinfo.value)
