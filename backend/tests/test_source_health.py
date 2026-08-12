# -*- coding: utf-8 -*-
"""Source Health Service（来源级风控健康状态机）测试。

覆盖状态机全部行为：
- healthy / cooling_down / probe 三态流转；
- risk_control / rate_limit 立即冷却；
- 连续 transient 失败达到阈值才冷却；
- breaker irrelevant（auth/permission/not_found/validation/redirect/
  scan_limit）不累计不冷却，仅更新失败原因与时间；
- 冷却期结束后的**原子单探针**：仅第一个调用者获得探针许可，
  其余并发调用者被拒绝；探针成功回 healthy、再遇风险立即再冷却。

所有时间均通过 now 参数显式控制，不 sleep。
"""

import time

import pytest

from app.db.database import close_connection, init_db
from app.catalog import source_health as sh

# 时间基准：取真实时钟，测试内全部用相对推算（不 sleep）。
# in_cooldown 是只读属性（内部用 time.time()），基准确保"立即冷却"断言可靠。
T0 = time.time()
RISK_CONTROL_COOLDOWN_SECONDS = 6 * 3600  # 21600
RATE_LIMIT_COOLDOWN_SECONDS = 3600
TRANSIENT_FAILURE_THRESHOLD = 3
TRANSIENT_COOLDOWN_SECONDS = 30 * 60  # 1800


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    """隔离临时 SQLite 并初始化 schema v3（含 source_health 表）。"""
    import app.db.database as db_mod

    db_mod.close_connection()
    monkeypatch.setattr(db_mod, "_db_path", tmp_path / "source_health_test.db")
    init_db()
    yield
    db_mod.close_connection()


# ============================================================
# 无记录默认行为
# ============================================================

class TestGetHealthDefault:
    def test_no_record_returns_healthy_default(self, health_db):
        """无记录时 get_health 返回 healthy 默认。"""
        rec = sh.get_health("src-missing")
        assert rec.source_id == "src-missing"
        assert rec.state == sh.STATE_HEALTHY
        assert rec.reason_kind == ""
        assert rec.consecutive_failures == 0
        assert rec.cooldown_until == 0
        assert not rec.in_cooldown

    def test_can_request_without_record(self, health_db):
        """无记录时 can_request 返回 (True, healthy record)。"""
        allowed, rec = sh.can_request("src-fresh", now=T0)
        assert allowed is True
        assert rec.state == sh.STATE_HEALTHY

    def test_can_request_on_healthy_record(self, health_db):
        """已有 healthy 记录时 can_request 直接放行。"""
        sh.record_success("src-ok", now=T0 - 100)
        allowed, rec = sh.can_request("src-ok", now=T0)
        assert allowed is True
        assert rec.state == sh.STATE_HEALTHY


# ============================================================
# record_success：任意状态回 healthy
# ============================================================

class TestRecordSuccess:
    def test_success_writes_last_success_at(self, health_db):
        """成功写入 last_success_at。"""
        sh.record_success("src-success", now=T0)
        rec = sh.get_health("src-success")
        assert rec.state == sh.STATE_HEALTHY
        assert rec.last_success_at == T0
        assert rec.consecutive_failures == 0
        assert rec.cooldown_until == 0
        assert not rec.in_cooldown

    def test_success_during_cooldown_keeps_cooldown(self, health_db):
        """冷却中到达的成功（旧 in-flight）不得解除冷却：只更新 last_success_at。"""
        sh.record_failure("src-recover", "risk_control", now=T0 - 100)
        before = sh.get_health("src-recover")
        assert before.state == sh.STATE_COOLING_DOWN

        sh.record_success("src-recover", now=T0)
        rec = sh.get_health("src-recover")
        assert rec.state == sh.STATE_COOLING_DOWN  # 冷却不被滞后成功覆盖
        assert rec.cooldown_until == before.cooldown_until
        assert rec.reason_kind == "risk_control"
        assert rec.consecutive_failures == before.consecutive_failures
        assert rec.last_success_at == T0  # 只更新时间戳
        assert rec.in_cooldown

    def test_success_recovers_from_probe(self, health_db):
        """probe 状态下 record_success → healthy。"""
        sh.record_failure("src-probe-ok", "rate_limit", now=T0 - 100)
        allowed, _ = sh.can_request("src-probe-ok", now=T0 + RATE_LIMIT_COOLDOWN_SECONDS + 1)
        assert allowed is True
        assert sh.get_health("src-probe-ok").state == sh.STATE_PROBE

        sh.record_success("src-probe-ok", now=T0)
        rec = sh.get_health("src-probe-ok")
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0


# ============================================================
# 立即冷却：risk_control / rate_limit
# ============================================================

class TestImmediateCooldown:
    def test_risk_control_cooldown_six_hours(self, health_db):
        """risk_control 立即冷却 6 小时，reason_kind 记录。"""
        rec = sh.record_failure("src-risk", "risk_control", now=T0)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "risk_control"
        assert rec.cooldown_until == T0 + RISK_CONTROL_COOLDOWN_SECONDS
        assert rec.consecutive_failures == 1
        assert rec.last_failure_at == T0
        assert rec.in_cooldown

    def test_rate_limit_cooldown_one_hour(self, health_db):
        """rate_limit 立即冷却 1 小时。"""
        rec = sh.record_failure("src-rate", "rate_limit", now=T0)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "rate_limit"
        assert rec.cooldown_until == T0 + RATE_LIMIT_COOLDOWN_SECONDS
        assert rec.in_cooldown

    def test_can_request_blocked_during_cooldown(self, health_db):
        """冷却期内 can_request 返回 (False, record)。"""
        sh.record_failure("src-blocked", "risk_control", now=T0)
        allowed, rec = sh.can_request("src-blocked", now=T0 + 100)
        assert allowed is False
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.in_cooldown


# ============================================================
# 连续 transient 失败：阈值后才冷却
# ============================================================

class TestTransientFailures:
    @pytest.mark.parametrize(
        "kind",
        ["network", "timeout", "server_error", "transient", "page_consistency", "unknown"],
    )
    def test_transient_kind_preserved_before_cooldown(self, health_db, kind):
        """各类 transient 类型前两次失败不冷却，reason_kind 保留归一化类型。"""
        src = f"src-tran-{kind}"
        rec1 = sh.record_failure(src, kind, now=T0)
        assert rec1.state == sh.STATE_HEALTHY
        assert rec1.reason_kind == kind
        assert rec1.consecutive_failures == 1
        assert not rec1.in_cooldown

        rec2 = sh.record_failure(src, kind, now=T0 + 1)
        assert rec2.state == sh.STATE_HEALTHY
        assert rec2.consecutive_failures == 2
        assert not rec2.in_cooldown

    def test_third_transient_failure_enters_cooldown(self, health_db):
        """第 3 次连续 transient 失败进入冷却（30 分钟）。"""
        src = "src-tran-3"
        for i in range(2):
            sh.record_failure(src, "network", now=T0 + i)
        rec = sh.record_failure(src, "network", now=T0 + 2)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "network"
        assert rec.consecutive_failures == TRANSIENT_FAILURE_THRESHOLD
        assert rec.cooldown_until == T0 + 2 + TRANSIENT_COOLDOWN_SECONDS
        assert rec.in_cooldown

    def test_unknown_kind_normalized_to_unknown(self, health_db):
        """未知失败类型按 unknown transient 处理。"""
        rec = sh.record_failure("src-weird", "weird", now=T0)
        assert rec.state == sh.STATE_HEALTHY
        assert rec.reason_kind == "unknown"
        assert rec.consecutive_failures == 1
        assert not rec.in_cooldown

        sh.record_failure("src-weird", "weird", now=T0 + 1)
        rec3 = sh.record_failure("src-weird", "weird", now=T0 + 2)
        assert rec3.state == sh.STATE_COOLING_DOWN
        assert rec3.reason_kind == "unknown"
        assert rec3.cooldown_until == T0 + 2 + TRANSIENT_COOLDOWN_SECONDS

    def test_mixed_transient_kinds_accumulate(self, health_db):
        """不同 transient 类型混合也累计连续失败次数。"""
        sh.record_failure("src-mixed", "timeout", now=T0)
        sh.record_failure("src-mixed", "network", now=T0 + 1)
        rec = sh.record_failure("src-mixed", "server_error", now=T0 + 2)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "server_error"  # 以触发阈值的最后一次为准
        assert rec.consecutive_failures == 3


# ============================================================
# 冷却期结束 → probe；探针后再失败 → 立即再冷却
# ============================================================

class TestCooldownExpiryAndProbe:
    def _enter_cooldown(self, src, kind="risk_control", at=T0):
        sh.record_failure(src, kind, now=at)

    def test_can_request_after_cooldown_transitions_to_probe(self, health_db):
        """冷却期已过：can_request 返回 True 且状态转为 probe。"""
        self._enter_cooldown("src-expire")
        allowed, rec = sh.can_request(
            "src-expire", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        )
        assert allowed is True
        assert rec.state == sh.STATE_PROBE
        assert rec.cooldown_until == 0
        assert not rec.in_cooldown

    def test_probe_success_returns_healthy(self, health_db):
        """probe 状态 record_success → healthy。"""
        self._enter_cooldown("src-probe-succ")
        sh.can_request("src-probe-succ", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1)
        assert sh.get_health("src-probe-succ").state == sh.STATE_PROBE

        sh.record_success("src-probe-succ", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 2)
        rec = sh.get_health("src-probe-succ")
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0
        assert rec.last_success_at == T0 + RISK_CONTROL_COOLDOWN_SECONDS + 2

    def test_probe_failure_risk_control_recooldowns_immediately(self, health_db):
        """probe 状态再遇 risk_control：立即再次冷却。"""
        self._enter_cooldown("src-probe-fail", at=T0)
        sh.can_request("src-probe-fail", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1)  # 转 probe
        assert sh.get_health("src-probe-fail").state == sh.STATE_PROBE

        rec = sh.record_failure(
            "src-probe-fail", "risk_control", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 2
        )
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "risk_control"
        assert rec.cooldown_until == (
            T0 + RISK_CONTROL_COOLDOWN_SECONDS + 2 + RISK_CONTROL_COOLDOWN_SECONDS
        )
        assert rec.consecutive_failures == 2  # 冷却时 +1 累计

    def test_failure_after_cooldown_recooldowns_immediately(self, health_db):
        """冷却结束后下一次请求（探针）再遇风险 → 立即再冷却。"""
        self._enter_cooldown("src-cycle", at=T0)
        allowed, _ = sh.can_request("src-cycle", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1)
        assert allowed is True

        rec = sh.record_failure("src-cycle", "risk_control", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 2)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.cooldown_until == (
            T0 + RISK_CONTROL_COOLDOWN_SECONDS + 2 + RISK_CONTROL_COOLDOWN_SECONDS
        )

    def test_in_cooldown_property_reflects_state(self, health_db):
        """in_cooldown 属性：冷却中为 True，healthy/probe 为 False。"""
        self._enter_cooldown("src-clock")
        rec = sh.get_health("src-clock")
        assert rec.in_cooldown  # 真实时钟仍处于 6 小时冷却期内

        # 冷却中 record_success 不解除冷却（旧 in-flight success 语义）
        sh.record_success("src-clock", now=T0 + 1)
        assert sh.get_health("src-clock").in_cooldown

        sh.record_success("src-clock-ok", now=T0)
        assert not sh.get_health("src-clock-ok").in_cooldown

        self._enter_cooldown("src-clock2")
        sh.can_request("src-clock2", now=T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1)  # 转 probe
        assert not sh.get_health("src-clock2").in_cooldown


# ============================================================
# 显式 enter_cooldown
# ============================================================

class TestEnterCooldown:
    def test_explicit_enter_cooldown(self, health_db):
        """显式冷却按指定秒数设置冷却期。"""
        sh.enter_cooldown(
            "src-manual",
            reason_kind="manual",
            cooldown_seconds=600,
            now=T0,
        )
        rec = sh.get_health("src-manual")
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "manual"
        assert rec.cooldown_until == T0 + 600
        assert rec.last_failure_at == T0

    def test_enter_cooldown_keeps_failure_count(self, health_db):
        """显式冷却可携带连续失败计数。"""
        sh.enter_cooldown(
            "src-manual2",
            reason_kind="manual",
            cooldown_seconds=10,
            consecutive_failures=7,
            now=T0,
        )
        rec = sh.get_health("src-manual2")
        assert rec.consecutive_failures == 7


# ============================================================
# list_health
# ============================================================

class TestListHealth:
    def test_list_health_contains_source(self, health_db):
        """list_health 返回包含该来源的记录。"""
        sh.record_failure("src-list", "risk_control", now=T0)
        rows = sh.list_health()
        ids = {r.source_id for r in rows}
        assert "src-list" in ids
        by_id = {r.source_id: r for r in rows}
        assert by_id["src-list"].state == sh.STATE_COOLING_DOWN

    def test_list_health_multiple_sources(self, health_db):
        """多个来源都在列表中，且返回可序列化字典。"""
        sh.record_success("src-a", now=T0)
        sh.record_failure("src-b", "rate_limit", now=T0 + 1)
        rows = sh.list_health()
        assert {r.source_id for r in rows} == {"src-a", "src-b"}
        d = rows[0].to_dict()
        assert d["source_id"]
        assert set(d) >= {
            "state", "reason_kind", "consecutive_failures", "cooldown_until",
            "last_failure_at", "last_success_at", "updated_at",
        }


# ============================================================
# breaker irrelevant：known non-transient 不累计不冷却
# ============================================================

class TestBreakerIrrelevantKinds:
    """auth / permission(403) / not_found(404) / validation / redirect /
    scan_limit：不累计 circuit-breaker failure count、不触发冷却，
    仅更新 last_failure_at / reason_kind / updated_at。"""

    @pytest.mark.parametrize(
        "kind", ["validation", "redirect", "scan_limit"],
    )
    def test_irrelevant_kind_three_failures_no_cooldown(self, health_db, kind):
        """validation / redirect / scan_limit 各 3 次失败均不累计不冷却。"""
        src = f"src-irrel-{kind}"
        for i in range(3):
            rec = sh.record_failure(src, kind, now=T0 + i)
            assert rec.state == sh.STATE_HEALTHY
            assert rec.consecutive_failures == 0
            assert rec.reason_kind == kind
            assert rec.cooldown_until == 0
            assert not rec.in_cooldown
        rec = sh.get_health(src)
        assert rec.consecutive_failures == 0
        assert rec.reason_kind == kind
        assert rec.last_failure_at == T0 + 2

    def test_permission_403_three_failures_no_cooldown(self, health_db):
        """403(permission)×3 不冷却：state 保持 healthy、计数不增长。"""
        src = "src-403"
        for i in range(3):
            rec = sh.record_failure(src, "permission", now=T0 + i)
            assert rec.state == sh.STATE_HEALTHY
            assert rec.consecutive_failures == 0
            assert rec.reason_kind == "permission"
            assert not rec.in_cooldown
        assert sh.get_health(src).consecutive_failures == 0

    def test_not_found_404_three_failures_no_cooldown(self, health_db):
        """404(not_found)×3 不冷却：state 保持 healthy、计数不增长。"""
        src = "src-404"
        for i in range(3):
            rec = sh.record_failure(src, "not_found", now=T0 + i)
            assert rec.state == sh.STATE_HEALTHY
            assert rec.consecutive_failures == 0
            assert rec.reason_kind == "not_found"
            assert not rec.in_cooldown
        assert sh.get_health(src).consecutive_failures == 0

    def test_auth_three_failures_no_cooldown(self, health_db):
        """auth×3 不冷却：state 保持 healthy、计数不增长、reason_kind 更新。"""
        src = "src-auth"
        for i in range(3):
            rec = sh.record_failure(src, "auth", now=T0 + i)
            assert rec.state == sh.STATE_HEALTHY
            assert rec.consecutive_failures == 0
            assert rec.reason_kind == "auth"
            assert not rec.in_cooldown
        rec = sh.get_health(src)
        assert rec.consecutive_failures == 0
        assert rec.reason_kind == "auth"
        assert rec.last_failure_at == T0 + 2

    def test_irrelevant_during_cooldown_keeps_state(self, health_db):
        """冷却中的来源收到 irrelevant 错误：state/cooldown_until/原因保持原状。

        Review Fix 3：冷却原因保护——普通 404 结果不得覆盖冷却原因，
        否则冷却语义被普通旧请求结果污染（reason_kind 变 not_found）。
        """
        src = "src-cooldown-403"
        sh.record_failure(src, "risk_control", now=T0)  # 进入 6h 冷却
        before = sh.get_health(src)
        assert before.state == sh.STATE_COOLING_DOWN

        rec = sh.record_failure(src, "not_found", now=T0 + 60)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.cooldown_until == before.cooldown_until
        assert rec.consecutive_failures == 1  # risk_control 时累计的 1 次保持
        assert rec.reason_kind == "risk_control"  # 冷却原因保护：普通结果不覆盖
        assert rec.last_failure_at == T0 + 60  # 失败时间照常更新
        assert rec.in_cooldown

    def test_irrelevant_does_not_reset_transient_accumulation(self, health_db):
        """transient 累计中插入 irrelevant 失败：既不计入也不清零累计。"""
        src = "src-mix-irrel"
        sh.record_failure(src, "network", now=T0)          # 1 次
        sh.record_failure(src, "not_found", now=T0 + 1)    # irrelevant：不参与
        rec2 = sh.record_failure(src, "timeout", now=T0 + 2)  # 2 次
        assert rec2.state == sh.STATE_HEALTHY
        assert rec2.consecutive_failures == 2
        rec3 = sh.record_failure(src, "server_error", now=T0 + 3)  # 3 次 → 冷却
        assert rec3.state == sh.STATE_COOLING_DOWN
        assert rec3.consecutive_failures == TRANSIENT_FAILURE_THRESHOLD


# ============================================================
# breaker relevant：network/timeout/server_error/transient 仍累计
# ============================================================

class TestBreakerRelevantKinds:
    @pytest.mark.parametrize("kind", ["network", "timeout", "server_error", "transient"])
    def test_relevant_kind_three_failures_enter_cooldown(self, health_db, kind):
        """network/timeout/server_error/transient 仍累计：3 次后冷却 30 分钟。"""
        src = f"src-rel-{kind}"
        for i in range(2):
            sh.record_failure(src, kind, now=T0 + i)
        rec = sh.record_failure(src, kind, now=T0 + 2)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == kind
        assert rec.consecutive_failures == TRANSIENT_FAILURE_THRESHOLD
        assert rec.cooldown_until == T0 + 2 + TRANSIENT_COOLDOWN_SECONDS
        assert rec.in_cooldown


# ============================================================
# 原子单探针：冷却到期后仅第一个调用者获得探针许可
# ============================================================

class TestSingleProbe:
    def _enter_cooldown(self, src, kind="risk_control", at=T0):
        sh.record_failure(src, kind, now=at)

    def test_first_caller_gets_probe_second_denied(self, health_db):
        """冷却到期：第一个 can_request True 且 state=probe；第二个（同 now）False。"""
        self._enter_cooldown("src-sp-1")
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        allowed1, rec1 = sh.can_request("src-sp-1", now=expiry)
        assert allowed1 is True
        assert rec1.state == sh.STATE_PROBE
        assert rec1.cooldown_until == 0
        assert not rec1.in_cooldown

        allowed2, rec2 = sh.can_request("src-sp-1", now=expiry)
        assert allowed2 is False
        assert rec2.state == sh.STATE_PROBE
        assert rec2.cooldown_until == 0

    def test_probe_blocks_until_outcome_reported(self, health_db):
        """探针结果上报前，probe 状态继续拒绝其他调用者；成功后恢复放行。"""
        self._enter_cooldown("src-sp-2")
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        sh.can_request("src-sp-2", now=expiry)

        allowed, _ = sh.can_request("src-sp-2", now=expiry + 10)
        assert allowed is False

        sh.record_success("src-sp-2", now=expiry + 20)
        allowed, rec = sh.can_request("src-sp-2", now=expiry + 30)
        assert allowed is True
        assert rec.state == sh.STATE_HEALTHY

    def test_probe_success_returns_healthy(self, health_db):
        """探针成功 → healthy 并清零计数。"""
        self._enter_cooldown("src-sp-succ")
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        sh.can_request("src-sp-succ", now=expiry)
        assert sh.get_health("src-sp-succ").state == sh.STATE_PROBE

        sh.record_success("src-sp-succ", now=expiry + 1)
        rec = sh.get_health("src-sp-succ")
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0
        assert rec.cooldown_until == 0

    def test_probe_risk_control_cooldowns_immediately(self, health_db):
        """探针遇 risk_control：立即再次冷却。"""
        self._enter_cooldown("src-sp-risk")
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        allowed, _ = sh.can_request("src-sp-risk", now=expiry)
        assert allowed is True

        rec = sh.record_failure("src-sp-risk", "risk_control", now=expiry + 1)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.cooldown_until == expiry + 1 + RISK_CONTROL_COOLDOWN_SECONDS
        assert rec.in_cooldown

    def test_probe_irrelevant_recovers_healthy(self, health_db):
        """探针遇 403/404 等 known non-transient：breaker 恢复 healthy（远端可达）。

        probe + irrelevant 错误 = 探针请求已真实到达远端并被正常处理
        （只是业务上 403/404），证明连接未被风控：state 回 healthy、
        计数清零；业务错误仍由调用方抛出。
        """
        self._enter_cooldown("src-sp-403")
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        allowed, _ = sh.can_request("src-sp-403", now=expiry)
        assert allowed is True

        rec = sh.record_failure("src-sp-403", "permission", now=expiry + 1)
        assert rec.state == sh.STATE_HEALTHY  # probe + irrelevant → healthy
        assert rec.consecutive_failures == 0
        assert rec.cooldown_until == 0
        assert not rec.in_cooldown
        assert rec.reason_kind == ""  # breaker 恢复后原因清空（Review Fix 3）
        assert rec.last_failure_at == expiry + 1


# ============================================================
# 并发抢占：冷却到期后两个并发调用者只有一个获得探针许可
# ============================================================

class TestConcurrentSingleProbe:
    def test_concurrent_claim_single_winner(self, health_db):
        """两线程并发抢占：恰好一个 True、一个 False，state 最终 probe。

        原子性由 ``UPDATE ... WHERE source_id=? AND state='cooling_down'``
        的 affected rowcount 保证：并发下只有一个调用者能匹配 WHERE 条件。
        """
        import threading

        from app.db import database as db_mod

        sh.record_failure("src-race", "risk_control", now=T0)
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def caller() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(sh.can_request("src-race", now=expiry)[0])
            finally:
                db_mod.close_connection()  # 关闭子线程本地连接

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert sorted(results) == [False, True]
        assert sh.get_health("src-race").state == sh.STATE_PROBE


# ============================================================
# peek_request_allowed：只读预检，绝不消费探针
# ============================================================

class TestPeekRequestAllowed:
    """peek 与 can_request 的职责分离（模块 1 最终补丁）：

    - peek 只读：冷却中拒绝；冷却已到期放行但**不修改 state**（探针
      保留给物理请求前的 can_request 原子抢占）；
    - can_request 是唯一消费探针的入口。
    """

    def test_peek_without_record_allows(self, health_db):
        """无记录时 peek 放行。"""
        allowed, rec = sh.peek_request_allowed("peek-none", now=T0)
        assert allowed is True
        assert rec.state == sh.STATE_HEALTHY

    def test_peek_healthy_allows(self, health_db):
        """healthy 时 peek 放行。"""
        sh.record_success("peek-ok", now=T0 - 10)
        allowed, rec = sh.peek_request_allowed("peek-ok", now=T0)
        assert allowed is True
        assert rec.state == sh.STATE_HEALTHY

    def test_peek_during_cooldown_denies(self, health_db):
        """冷却未到期：peek 返回 (False, record)，不改状态。"""
        sh.record_failure("peek-cool", "risk_control", now=T0)
        allowed, rec = sh.peek_request_allowed("peek-cool", now=T0 + 100)
        assert allowed is False
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.in_cooldown
        assert sh.get_health("peek-cool").state == sh.STATE_COOLING_DOWN

    def test_peek_expired_cooldown_allows_without_consuming_probe(self, health_db):
        """冷却已到期：peek 放行且**不改 state（不消费探针）**。"""
        sh.record_failure("peek-exp", "risk_control", now=T0)
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        allowed, rec = sh.peek_request_allowed("peek-exp", now=expiry)
        assert allowed is True
        assert rec.state == sh.STATE_COOLING_DOWN  # 关键：peek 不改 state
        assert sh.get_health("peek-exp").state == sh.STATE_COOLING_DOWN

        # peek 之后 can_request 仍能抢占探针（探针未被 peek 消耗）
        allowed2, rec2 = sh.can_request("peek-exp", now=expiry)
        assert allowed2 is True
        assert rec2.state == sh.STATE_PROBE

    def test_peek_probe_denies(self, health_db):
        """probe 中 peek 返回 (False, record)。"""
        sh.record_failure("peek-probe", "rate_limit", now=T0)
        expiry = T0 + RATE_LIMIT_COOLDOWN_SECONDS + 1
        sh.can_request("peek-probe", now=expiry)  # 抢占探针
        allowed, rec = sh.peek_request_allowed("peek-probe", now=expiry + 1)
        assert allowed is False
        assert rec.state == sh.STATE_PROBE


# ============================================================
# 最终补丁：source_cooling_down NO-OP / 冷却中 success 保护 /
# probe + transient 保守累计
# ============================================================

class TestFinalPatchSemantics:
    """规划员最终固定状态机回归：

    - cooling_down + 本地 source_cooling_down → 完全 NO-OP；
    - cooling_down + 旧 in-flight success → 保持 cooling_down；
    - probe + irrelevant → healthy（breaker 恢复）；
    - probe + transient → 保持 probe 累计，达阈值才冷却。
    """

    def test_record_failure_source_cooling_down_noop_during_cooldown(self, health_db):
        """冷却中 record_failure('source_cooling_down')：完全 NO-OP。"""
        sh.record_failure("noop-cool", "risk_control", now=T0)
        before = sh.get_health("noop-cool")

        for i in range(10):
            rec = sh.record_failure("noop-cool", "source_cooling_down", now=T0 + i + 1)
            assert rec.state == sh.STATE_COOLING_DOWN
            assert rec.cooldown_until == before.cooldown_until
            assert rec.reason_kind == "risk_control"
            assert rec.consecutive_failures == before.consecutive_failures
            assert rec.last_failure_at == before.last_failure_at  # 连失败时间都不动

    def test_record_failure_source_cooling_down_noop_on_healthy(self, health_db):
        """healthy 上 record_failure('source_cooling_down') 也完全 NO-OP。"""
        sh.record_success("noop-ok", now=T0 - 10)
        before = sh.get_health("noop-ok")
        rec = sh.record_failure("noop-ok", "source_cooling_down", now=T0)
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0
        assert rec.last_failure_at == before.last_failure_at
        assert rec.last_success_at == before.last_success_at

    def test_record_failure_source_cooling_down_noop_without_record(self, health_db):
        """无记录来源：record_failure('source_cooling_down') 不落库（NO-OP）。"""
        rec = sh.record_failure("noop-none", "source_cooling_down", now=T0)
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0
        # 确认没有写入任何记录（保持无记录默认）
        from app.catalog.source_health import get_connection

        row = get_connection().execute(
            "SELECT COUNT(*) AS n FROM source_health WHERE source_id = ?",
            ("noop-none",),
        ).fetchone()
        assert row["n"] == 0

    def test_success_during_probe_still_recovers(self, health_db):
        """probe 中 success → healthy（清 breaker）。"""
        sh.record_failure("succ-probe", "rate_limit", now=T0 - 100)
        expiry = T0 + RATE_LIMIT_COOLDOWN_SECONDS + 1
        sh.can_request("succ-probe", now=expiry)
        assert sh.get_health("succ-probe").state == sh.STATE_PROBE

        sh.record_success("succ-probe", now=expiry + 1)
        rec = sh.get_health("succ-probe")
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0
        assert rec.cooldown_until == 0

    def test_probe_transient_keeps_probe_and_accumulates(self, health_db):
        """probe 下遇 transient：保持 probe、累计计数、不立即冷却（保守做法）。"""
        sh.record_failure("probe-tran", "risk_control", now=T0)
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        allowed, _ = sh.can_request("probe-tran", now=expiry)
        assert allowed is True
        assert sh.get_health("probe-tran").state == sh.STATE_PROBE

        # risk_control 冷却时已累计 1 次（consecutive=1）；probe 下遇 transient
        # 保持 probe 继续累计（2 → 3 达阈值才冷却）
        rec = sh.record_failure("probe-tran", "network", now=expiry + 1)
        assert rec.state == sh.STATE_PROBE  # 不转 healthy（探针失败不证明远端可达）
        assert rec.consecutive_failures == 2
        assert not rec.in_cooldown

        # 第 3 次累计达阈值 → 正常冷却
        rec3 = sh.record_failure("probe-tran", "network", now=expiry + 2)
        assert rec3.state == sh.STATE_COOLING_DOWN
        assert rec3.consecutive_failures == TRANSIENT_FAILURE_THRESHOLD
        assert rec3.cooldown_until == expiry + 2 + TRANSIENT_COOLDOWN_SECONDS

    def test_irrelevant_three_failures_then_probe_recovers(self, health_db):
        """irrelevant 错误不累计；probe + irrelevant → healthy（回归组合）。"""
        # 连续 3 次 404：不冷却、不累计（现有语义保持）
        for i in range(3):
            rec = sh.record_failure("irrel-probe", "not_found", now=T0 + i)
            assert rec.state == sh.STATE_HEALTHY
            assert rec.consecutive_failures == 0
        # 触发冷却 → 到期 → 探针 → 404 → healthy
        sh.record_failure("irrel-probe", "risk_control", now=T0 + 10)
        expiry = T0 + 10 + RISK_CONTROL_COOLDOWN_SECONDS + 1
        allowed, _ = sh.can_request("irrel-probe", now=expiry)
        assert allowed is True
        rec = sh.record_failure("irrel-probe", "not_found", now=expiry + 1)
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0
        assert rec.cooldown_until == 0


# ============================================================
# Review Fix 3：原子化「读→判→写」+ 单调冷却（真并发交错回归）
# ============================================================

class TestAtomicTransitions:
    """规划员第三次审核：SourceHealth 的「读 → 判断 → 写」必须原子。

    旧实现存在 TOCTOU 竞态：record_success / record_failure 先用
    get_health() 读当前值、再 _upsert() 写入，两个操作之间并发线程可能
    写入冷却状态，旧线程继续用读到的陈旧值覆盖——冷却被滞后结果解除、
    冷却原因被普通 404 覆盖。

    修复：所有读-判-写收敛到 _transition()，单 BEGIN IMMEDIATE 事务内
    完成（线程本地连接 + busy_timeout=5000 串行化写）。以下测试通过
    _test_after_read_hook（事务内 SELECT 之后、写入之前触发）强制制造
    「先读到旧值 → 并发写入冷却 → 继续用旧值写」的交错，验证串行化后
    冷却事实不被陈旧普通结果覆盖。不用 sleep 猜时序，全部 Event/Barrier
    精确同步。
    """

    def test_a_stale_success_cannot_clear_risk_cooldown(self, health_db, monkeypatch):
        """Test A（真并发）：滞后 success 旧读(healthy) 与 risk_control 冷却交错 → 冷却保持。

        交错制造：success 线程事务内读到 healthy 后经 hook 暂停（写锁持有）；
        risk 线程开始写冷却事务（被写锁阻塞）；放行 success 提交 healthy 后
        risk 获得锁，基于最新状态写入 cooling_down 6h。串行化保证冷却写入
        永远基于最新状态，最终冷却保持。
        """
        import threading

        from app.db import database as db_mod

        src = "atomic-a"
        sh.record_success(src, now=T0 - 100)  # 预置 healthy 记录
        read_done = threading.Event()
        proceed = threading.Event()
        risk_entered = threading.Event()
        errors: list[BaseException] = []

        def hook(conn, source_id, current):
            # 事务内（写锁持有）SELECT 之后暂停，模拟旧实现 TOCTOU 窗口
            read_done.set()
            if not proceed.wait(timeout=10):
                raise TimeoutError("hook wait timed out")

        def run_success():
            try:
                sh.record_success(src, now=T0)  # 基于读到的旧值(healthy) 写
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        def run_risk():
            try:
                risk_entered.set()
                sh.record_failure(src, "risk_control", now=T0)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        monkeypatch.setattr(sh, "_test_after_read_hook", hook)
        t_success = threading.Thread(target=run_success)
        t_risk = threading.Thread(target=run_risk)
        t_success.start()
        assert read_done.wait(timeout=10)  # success 已读到 healthy 并暂停
        t_risk.start()
        assert risk_entered.wait(timeout=10)  # risk 已发起（在 BEGIN 处等写锁）
        proceed.set()  # 放行 success：提交其陈旧 healthy 写入
        t_success.join(timeout=10)
        t_risk.join(timeout=10)
        monkeypatch.setattr(sh, "_test_after_read_hook", None)

        assert not errors
        rec = sh.get_health(src)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "risk_control"
        assert rec.cooldown_until == T0 + RISK_CONTROL_COOLDOWN_SECONDS
        assert rec.in_cooldown

    def test_b_stale_irrelevant_cannot_overwrite_risk_cooldown(self, health_db, monkeypatch):
        """Test B：404/irrelevant 旧读 与 risk_control 冷却交错 → 冷却与原因保持。

        旧实现：irrelevant 读 healthy 后暂停，risk 写入冷却，irrelevant 继续
        用旧值写 state=healthy + reason=not_found → 冷却丢失且冷却原因被
        普通 404 覆盖。新实现：事务串行化 + 冷却原因保护，最终冷却保持
        risk_control 6h。
        """
        import threading

        from app.db import database as db_mod

        src = "atomic-b"
        sh.record_success(src, now=T0 - 100)  # 预置 healthy 记录
        read_done = threading.Event()
        proceed = threading.Event()
        risk_entered = threading.Event()
        errors: list[BaseException] = []

        def hook(conn, source_id, current):
            read_done.set()
            if not proceed.wait(timeout=10):
                raise TimeoutError("hook wait timed out")

        def run_irrelevant():
            try:
                sh.record_failure(src, "not_found", now=T0)  # 基于旧值(healthy) 写
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        def run_risk():
            try:
                risk_entered.set()
                sh.record_failure(src, "risk_control", now=T0)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        monkeypatch.setattr(sh, "_test_after_read_hook", hook)
        t_irrel = threading.Thread(target=run_irrelevant)
        t_risk = threading.Thread(target=run_risk)
        t_irrel.start()
        assert read_done.wait(timeout=10)  # irrelevant 已读到 healthy 并暂停
        t_risk.start()
        assert risk_entered.wait(timeout=10)  # risk 已发起（在 BEGIN 处等写锁）
        proceed.set()  # 放行 irrelevant：提交其陈旧 healthy 写入
        t_irrel.join(timeout=10)
        t_risk.join(timeout=10)
        monkeypatch.setattr(sh, "_test_after_read_hook", None)

        assert not errors
        rec = sh.get_health(src)
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "risk_control"  # 冷却原因不被 404 覆盖
        assert rec.cooldown_until == T0 + RISK_CONTROL_COOLDOWN_SECONDS
        assert rec.in_cooldown

    def test_c_cooldown_monotonic_never_shrinks(self, health_db):
        """Test C（顺序版）：冷却单调不缩短，双向验证。

        - 先 risk_control(6h) 再 rate_limit(1h)：冷却结束点保持 6h 值、
          reason 不降级（仍 risk_control）；
        - 先 rate_limit(1h) 再 risk_control(6h)：冷却延长到 6h。
        """
        # 正向：risk 6h 在先，rate 1h 在后 → 不缩短、原因不降级
        sh.enter_cooldown(
            "atomic-c1",
            reason_kind="risk_control",
            cooldown_seconds=RISK_CONTROL_COOLDOWN_SECONDS,
            now=T0,
        )
        rec1 = sh.get_health("atomic-c1")
        assert rec1.cooldown_until == T0 + RISK_CONTROL_COOLDOWN_SECONDS

        sh.enter_cooldown(
            "atomic-c1",
            reason_kind="rate_limit",
            cooldown_seconds=RATE_LIMIT_COOLDOWN_SECONDS,
            now=T0 + 60,
        )
        rec2 = sh.get_health("atomic-c1")
        assert rec2.cooldown_until == rec1.cooldown_until  # 6h 结束点不变
        assert rec2.reason_kind == "risk_control"  # rate_limit 不降级 risk 原因

        # 反向：rate 1h 在先，risk 6h 在后 → 延长到 6h
        sh.enter_cooldown(
            "atomic-c2",
            reason_kind="rate_limit",
            cooldown_seconds=RATE_LIMIT_COOLDOWN_SECONDS,
            now=T0,
        )
        sh.enter_cooldown(
            "atomic-c2",
            reason_kind="risk_control",
            cooldown_seconds=RISK_CONTROL_COOLDOWN_SECONDS,
            now=T0 + 60,
        )
        rec = sh.get_health("atomic-c2")
        assert rec.cooldown_until == T0 + 60 + RISK_CONTROL_COOLDOWN_SECONDS
        assert rec.reason_kind == "risk_control"

    def test_c2_concurrent_cooldown_never_shrinks(self, health_db):
        """Test C（真并发版，尽力而为）：两线程同时 enter_cooldown（6h vs 1h）。

        Barrier 精确同发；BEGIN IMMEDIATE 串行化后最终冷却期不短于较长者
        （= 6h 结束点），且无论提交顺序如何 reason 最终都是 risk_control
        （rate_limit 不降级 risk 原因）。
        """
        import threading

        from app.db import database as db_mod

        src = "atomic-c3"
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(reason_kind: str, seconds: float):
            try:
                barrier.wait(timeout=5)
                sh.enter_cooldown(
                    src,
                    reason_kind=reason_kind,
                    cooldown_seconds=seconds,
                    now=T0,
                )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        threads = [
            threading.Thread(
                target=worker,
                args=("risk_control", RISK_CONTROL_COOLDOWN_SECONDS),
            ),
            threading.Thread(
                target=worker,
                args=("rate_limit", RATE_LIMIT_COOLDOWN_SECONDS),
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        rec = sh.get_health(src)
        expected = T0 + max(
            RISK_CONTROL_COOLDOWN_SECONDS, RATE_LIMIT_COOLDOWN_SECONDS
        )
        assert rec.cooldown_until >= expected  # 单调：不短于较长冷却
        assert rec.reason_kind == "risk_control"  # 更强原因最终保持


# ============================================================
# 模块 1 最终准入原子化：can_request 陈旧过期判断不能绕过新 cooldown
# ============================================================

class TestFinalAdmissionAtomicCanRequest:
    """规划员第四次审核点名（P0 准入竞态）：can_request 自身原子化。

    旧实现：先 ``get_health()`` 读、再独立 ``UPDATE ... WHERE state=
    'cooling_down'``（**不检查 cooldown_until**）→ commit()，两步分离：
    - 线程 A 读到旧 cooldown 已过期；
    - 线程 B ``record_failure('risk_control')`` 把 cooldown 延长到未来 6h
      （state 仍 cooling_down）；
    - A 的 UPDATE 仍命中（WHERE 只看 state）→ 把 B 刚建立的 6h 风控改写成
      probe / cooldown=0，绕过。

    新实现：can_request 在单个 ``BEGIN IMMEDIATE`` 事务内完成「SELECT 最新
    state → 判断 → 若冷却且过期则转 probe」。以下测试用
    ``_test_after_read_hook`` 精确制造交错：A 在事务内读到过期值后暂停
    （持有写锁），B 的 risk_control 冷却在锁外排队；放行 A 提交 probe 后
    B 基于最新值写入 cooling_down 6h——最终冷却保持，A 的陈旧过期判断
    不可能绕过 B 刚建立的新冷却。
    """

    def test_stale_expired_read_cannot_bypass_new_cooldown(self, health_db, monkeypatch):
        """A 陈旧过期读(6h 已过) 与 B risk_control 延长冷却交错 → 冷却保持。

        最终断言：state==cooling_down、allowed==False、cooldown_until==
        B 时刻 + 6h、**不是 probe**。旧实现（独立 UPDATE 无 cooldown_until
        条件）下 A 的 UPDATE 会把 B 的冷却改写成 probe → 本测试失败。
        """
        import threading

        from app.db import database as db_mod

        src = "final-admission-1"
        # 预置冷却：旧 cooldown_until = T0 + 6h（相对 now=expiry 已过期）
        sh.record_failure(src, "risk_control", now=T0)
        expiry = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 1  # A 视角：旧冷却已过期
        b_now = T0 + RISK_CONTROL_COOLDOWN_SECONDS + 2  # B 的冷却写入时刻

        read_done = threading.Event()
        proceed = threading.Event()
        risk_entered = threading.Event()
        errors: list[BaseException] = []
        paused = False

        def hook(conn, source_id, current):
            nonlocal paused
            # 只暂停第一个进入事务的调用者（A 的 can_request）；B 的
            # record_failure 在 A 提交后才获得写锁，此时不再暂停
            if source_id == src and not paused:
                paused = True
                read_done.set()
                if not proceed.wait(timeout=10):
                    raise TimeoutError("hook wait timed out")

        def run_caller_a():
            try:
                # A：事务内读到旧 cooldown 已过期，经 hook 暂停等待放行
                sh.can_request(src, now=expiry)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        def run_risk_b():
            try:
                risk_entered.set()
                # B：把冷却延长到未来 6h（state 仍 cooling_down）
                sh.record_failure(src, "risk_control", now=b_now)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_mod.close_connection()

        monkeypatch.setattr(sh, "_test_after_read_hook", hook)
        t_a = threading.Thread(target=run_caller_a)
        t_b = threading.Thread(target=run_risk_b)
        t_a.start()
        assert read_done.wait(timeout=10)  # A 已读到过期值并暂停（写锁持有）
        t_b.start()
        assert risk_entered.wait(timeout=10)  # B 已发起（在 BEGIN 处等写锁）
        proceed.set()  # 放行 A：提交其「过期→probe」转换
        t_a.join(timeout=10)
        t_b.join(timeout=10)
        monkeypatch.setattr(sh, "_test_after_read_hook", None)

        assert not errors
        rec = sh.get_health(src)
        # B 的冷却必须保持：不是 probe、cooldown_until 是未来 6h 值
        assert rec.state == sh.STATE_COOLING_DOWN
        assert rec.reason_kind == "risk_control"
        assert rec.cooldown_until == b_now + RISK_CONTROL_COOLDOWN_SECONDS
        assert rec.in_cooldown

        # B 提交后任何准入调用（即使以 A 的陈旧时刻）都返回 False：
        # 新冷却未被绕过
        allowed, rec2 = sh.can_request(src, now=expiry)
        assert allowed is False
        assert rec2.state == sh.STATE_COOLING_DOWN
        assert rec2.cooldown_until == b_now + RISK_CONTROL_COOLDOWN_SECONDS
