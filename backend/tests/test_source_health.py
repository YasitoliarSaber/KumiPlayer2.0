# -*- coding: utf-8 -*-
"""Source Health Service（来源级风控健康状态机）测试。

覆盖状态机全部行为：
- healthy / cooling_down / probe 三态流转；
- risk_control / rate_limit 立即冷却；
- 连续 transient 失败达到阈值才冷却；
- 冷却期结束自动转 probe，探针成功回 healthy、再遇风险立即再冷却。

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

    def test_success_recovers_from_cooldown(self, health_db):
        """冷却中的来源成功一次立即回 healthy 并清零计数。"""
        sh.record_failure("src-recover", "risk_control", now=T0 - 100)
        assert sh.get_health("src-recover").state == sh.STATE_COOLING_DOWN

        sh.record_success("src-recover", now=T0)
        rec = sh.get_health("src-recover")
        assert rec.state == sh.STATE_HEALTHY
        assert rec.consecutive_failures == 0
        assert rec.cooldown_until == 0
        assert not rec.in_cooldown

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

        sh.record_success("src-clock", now=T0)
        assert not sh.get_health("src-clock").in_cooldown

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
