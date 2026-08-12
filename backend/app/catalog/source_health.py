"""来源级风控健康状态服务（Source Health Service）。

模块 1（OpenList / 网盘访问安全与风控保护）的统一实现：
- 状态：healthy / cooling_down / probe（不引入十几个状态）；
- 持久化：``source_health`` 表（按 source_id，即 OpenList 连接键）；
- 所有 SQL 集中在本文件，不允许散落在 client / API / scanner / discovery；
- 阈值集中定义在文件头部，禁止散落魔法数字。

安全语义（保守边界）：
- 明确 risk_control（405 风控页）→ 立即 cooling_down，冷却期内所有后台请求停止；
- 最终收到 429 → cooling_down（不让 JobRunner 短时间高频自动重试）；
- 连续 transient 失败（network/timeout/5xx 等）→ 达到阈值后才进入冷却；
  单个 500 不会把整个账号封死，几十个连续失败也不会永远扫下去；
- 目录级/根级 known non-transient 错误（auth/permission/not_found/validation/
  redirect/scan_limit）**不参与 circuit-breaker**：不累计失败计数、不触发冷却，
  仅更新最近失败原因与时间——403/404/认证失败是资源/路径问题，绝不能让它们
  把整个网盘连接冻住 30 分钟；
- 冷却结束后通过**原子单探针**放行：只有第一个调用者获得探针许可
  （state 置 probe），其余并发调用者仍被拒绝，避免多探针并发；
  探针成功回 healthy，再遇风险立刻再冷却。

外部调用方通过 :func:`can_request` 拦截发请求前的检查；通过
:func:`record_success` / :func:`record_failure` 上报每次最终结果。
"""

from __future__ import annotations

import time
from typing import Any

from app.db.database import get_connection

# ---------------------------------------------------------------------------
# 阈值集中定义（单位：秒 / 次数）
# ---------------------------------------------------------------------------
#: 明确风控（risk_control）的冷却时长：保守安全边界，宁可多停
RISK_CONTROL_COOLDOWN_SECONDS = 6 * 3600
#: 最终收到 429 后的冷却时长
RATE_LIMIT_COOLDOWN_SECONDS = 3600
#: 连续 transient 失败进入冷却的阈值次数（目录请求粒度）
TRANSIENT_FAILURE_THRESHOLD = 3
#: 连续 transient 失败触顶后的冷却时长
TRANSIENT_COOLDOWN_SECONDS = 30 * 60

#: 稳定状态集合
STATE_HEALTHY = "healthy"
STATE_COOLING_DOWN = "cooling_down"
STATE_PROBE = "probe"

#: breaker relevant：参与 circuit-breaker 累计的失败类型。
#: risk_control/rate_limit 立即冷却；其余累计 consecutive_failures 达
#: TRANSIENT_FAILURE_THRESHOLD 后冷却（未知 kind 归入 "unknown"）。
BREAKER_RELEVANT_KINDS = {
    "risk_control",
    "rate_limit",
    "network",
    "timeout",
    "server_error",
    "transient",
    "page_consistency",
    "unknown",
}

#: breaker irrelevant：目录级/根级 known non-transient 问题。
#: 不累计 circuit-breaker failure count、不触发冷却，仅更新
#: last_failure_at / reason_kind / updated_at（state / consecutive_failures /
#: cooldown_until 保持原状）——403/404/auth 绝不能让整个连接进入冷却。
BREAKER_IRRELEVANT_KINDS = {
    "auth",
    "permission",
    "not_found",
    "validation",
    "redirect",
    "scan_limit",
}

#: 触发立即冷却的失败类型（breaker relevant 子集）
_IMMEDIATE_COOLDOWN_KINDS = {"risk_control", "rate_limit"}
#: 计入连续失败计数的类型（breaker relevant 减去立即冷却部分；
#: 其余未知类型一律归一化为 unknown 计入累计）
_TRANSIENT_KINDS = BREAKER_RELEVANT_KINDS - _IMMEDIATE_COOLDOWN_KINDS


class SourceHealthRecord:
    """source_health 一行的只读投影。"""

    __slots__ = (
        "source_id",
        "state",
        "reason_kind",
        "consecutive_failures",
        "cooldown_until",
        "last_failure_at",
        "last_success_at",
        "updated_at",
    )

    def __init__(self, row: Any | None = None):
        data = dict(row) if row is not None else {}
        self.source_id = str(data.get("source_id") or "")
        self.state = str(data.get("state") or STATE_HEALTHY)
        self.reason_kind = str(data.get("reason_kind") or "")
        self.consecutive_failures = int(data.get("consecutive_failures") or 0)
        self.cooldown_until = float(data.get("cooldown_until") or 0)
        self.last_failure_at = float(data.get("last_failure_at") or 0)
        self.last_success_at = float(data.get("last_success_at") or 0)
        self.updated_at = float(data.get("updated_at") or 0)

    @property
    def in_cooldown(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self.state == STATE_COOLING_DOWN and now < self.cooldown_until

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "state": self.state,
            "reason_kind": self.reason_kind,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_until": self.cooldown_until,
            "last_failure_at": self.last_failure_at,
            "last_success_at": self.last_success_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def get_health(source_id: str) -> SourceHealthRecord:
    """读取来源健康记录；无记录时返回 healthy 默认。"""
    row = get_connection().execute(
        "SELECT * FROM source_health WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return SourceHealthRecord({"source_id": source_id})
    return SourceHealthRecord(row)


def _upsert(
    source_id: str,
    *,
    state: str,
    reason_kind: str = "",
    consecutive_failures: int = 0,
    cooldown_until: float = 0.0,
    last_failure_at: float = 0.0,
    last_success_at: float = 0.0,
    now: float | None = None,
) -> None:
    now = now if now is not None else time.time()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO source_health (
            source_id, state, reason_kind, consecutive_failures,
            cooldown_until, last_failure_at, last_success_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            state = excluded.state,
            reason_kind = excluded.reason_kind,
            consecutive_failures = excluded.consecutive_failures,
            cooldown_until = excluded.cooldown_until,
            last_failure_at = excluded.last_failure_at,
            last_success_at = excluded.last_success_at,
            updated_at = excluded.updated_at
        """,
        (
            source_id, state, reason_kind, consecutive_failures,
            cooldown_until, last_failure_at, last_success_at, now,
        ),
    )
    conn.commit()


def record_success(source_id: str, *, now: float | None = None) -> None:
    """记录一次最终成功：回到 healthy 并清零连续失败计数。"""
    now = now if now is not None else time.time()
    _upsert(
        source_id,
        state=STATE_HEALTHY,
        reason_kind="",
        consecutive_failures=0,
        cooldown_until=0.0,
        last_success_at=now,
        now=now,
    )


def enter_cooldown(
    source_id: str,
    *,
    reason_kind: str,
    cooldown_seconds: float,
    consecutive_failures: int = 0,
    last_failure_at: float | None = None,
    now: float | None = None,
) -> None:
    """显式把来源置为 cooling_down（冷却期由调用方指定秒数）。"""
    now = now if now is not None else time.time()
    last_failure_at = last_failure_at if last_failure_at is not None else now
    _upsert(
        source_id,
        state=STATE_COOLING_DOWN,
        reason_kind=reason_kind,
        consecutive_failures=consecutive_failures,
        cooldown_until=now + max(0.0, cooldown_seconds),
        last_failure_at=last_failure_at,
        now=now,
    )


def record_failure(source_id: str, kind: str, *, now: float | None = None) -> SourceHealthRecord:
    """记录一次最终失败，并按失败类型推进状态机。

    返回更新后的记录；调用方可通过 ``in_cooldown`` 判断是否需要停止后续请求。

    分类语义：
    - breaker relevant：risk_control/rate_limit 立即冷却；其余累计连续失败，
      达阈值才冷却（未知 kind 归为 ``unknown``）；
    - breaker irrelevant（auth/permission/not_found/validation/redirect/
      scan_limit）：目录级/根级 known non-transient 问题，不累计不冷却，
      仅更新 last_failure_at / reason_kind / updated_at，state /
      consecutive_failures / cooldown_until 保持原状。
    """
    now = now if now is not None else time.time()
    current = get_health(source_id)

    if kind in BREAKER_IRRELEVANT_KINDS:
        # known non-transient：不累计 circuit-breaker 计数、不触发冷却，
        # 仅记录最近一次失败原因与时间；state / cooldown_until 保持原状。
        _upsert(
            source_id,
            state=current.state,
            reason_kind=kind,
            consecutive_failures=current.consecutive_failures,
            cooldown_until=current.cooldown_until,
            last_failure_at=now,
            last_success_at=current.last_success_at,
            now=now,
        )
        return get_health(source_id)

    if kind in _IMMEDIATE_COOLDOWN_KINDS:
        cooldown = (
            RISK_CONTROL_COOLDOWN_SECONDS
            if kind == "risk_control"
            else RATE_LIMIT_COOLDOWN_SECONDS
        )
        enter_cooldown(
            source_id,
            reason_kind=kind,
            cooldown_seconds=cooldown,
            consecutive_failures=current.consecutive_failures + 1,
            last_failure_at=now,
            now=now,
        )
        return get_health(source_id)

    # transient 类：累计连续失败，达到阈值才冷却
    normalized = kind if kind in _TRANSIENT_KINDS else "unknown"
    consecutive = current.consecutive_failures + 1
    if consecutive >= TRANSIENT_FAILURE_THRESHOLD:
        enter_cooldown(
            source_id,
            reason_kind=normalized,
            cooldown_seconds=TRANSIENT_COOLDOWN_SECONDS,
            consecutive_failures=consecutive,
            last_failure_at=now,
            now=now,
        )
    else:
        _upsert(
            source_id,
            state=STATE_HEALTHY if current.state != STATE_COOLING_DOWN else current.state,
            reason_kind=normalized,
            consecutive_failures=consecutive,
            cooldown_until=current.cooldown_until,
            last_failure_at=now,
            last_success_at=current.last_success_at,
            now=now,
        )
    return get_health(source_id)


def can_request(source_id: str, *, now: float | None = None) -> tuple[bool, SourceHealthRecord]:
    """判断当前是否允许向该来源发请求。

    返回 (allowed, record)：
    - 冷却中 → (False, record)；
    - 冷却期已过 → **原子单探针抢占**：仅第一个调用者获得探针许可，
      (True, probe_record)（state 置 probe、cooldown_until 置 0）；
      并发/后续调用者 (False, 最新 record)，防止多探针并发；
    - probe 中（探针已发出、尚未上报结果）→ (False, record)；
    - 无记录 / healthy → (True, record)。

    抢占通过 ``UPDATE ... WHERE source_id=? AND state='cooling_down'`` 保证
    原子性：并发下只有一个调用者能匹配该条件（affected rowcount=1），
    其余调用者 rowcount=0 判定未抢到。
    """
    now = now if now is not None else time.time()
    record = get_health(source_id)
    if record.state != STATE_COOLING_DOWN:
        # probe 中不允许再发请求（单探针：一个来源同一时刻只允许一个探针）
        if record.state == STATE_PROBE:
            return False, record
        return True, record
    if now < record.cooldown_until:
        return False, record
    # 冷却期已结束：原子抢占单探针许可（并发下只有一个调用者能抢到）
    conn = get_connection()
    cursor = conn.execute(
        """
        UPDATE source_health
        SET state = ?, cooldown_until = ?, updated_at = ?
        WHERE source_id = ? AND state = ?
        """,
        (STATE_PROBE, 0.0, now, source_id, STATE_COOLING_DOWN),
    )
    if cursor.rowcount == 0:
        # 已被并发调用者抢先转为 probe：本调用者不得放行
        return False, get_health(source_id)
    conn.commit()
    return True, get_health(source_id)


def list_health() -> list[SourceHealthRecord]:
    """列出全部来源健康记录（按最后更新倒序）。"""
    rows = get_connection().execute(
        "SELECT * FROM source_health ORDER BY updated_at DESC"
    ).fetchall()
    return [SourceHealthRecord(row) for row in rows]
