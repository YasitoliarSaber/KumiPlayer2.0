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
- 冷却结束后的下一次请求按 probe 处理：成功回 healthy，再遇风险立刻再冷却。

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

#: 触发立即冷却的失败类型
_IMMEDIATE_COOLDOWN_KINDS = {"risk_control", "rate_limit"}
#: 计入连续失败计数的类型（其余未知类型一律按 transient 处理）
_TRANSIENT_KINDS = {"network", "timeout", "server_error", "transient", "page_consistency", "unknown"}


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
    """
    now = now if now is not None else time.time()
    current = get_health(source_id)

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
    - 冷却期已过 → 自动转为 probe 状态并返回 (True, record)，允许下一次
      请求作为探针（probe 成功回 healthy，再遇风险立即再冷却）；
    - 无记录 / healthy → (True, record)。
    """
    now = now if now is not None else time.time()
    record = get_health(source_id)
    if record.state != STATE_COOLING_DOWN:
        return True, record
    if now < record.cooldown_until:
        return False, record
    # 冷却期已结束：转 probe，允许下一次请求探活
    _upsert(
        source_id,
        state=STATE_PROBE,
        reason_kind=record.reason_kind,
        consecutive_failures=record.consecutive_failures,
        cooldown_until=0.0,
        last_failure_at=record.last_failure_at,
        last_success_at=record.last_success_at,
        now=now,
    )
    return True, get_health(source_id)


def list_health() -> list[SourceHealthRecord]:
    """列出全部来源健康记录（按最后更新倒序）。"""
    rows = get_connection().execute(
        "SELECT * FROM source_health ORDER BY updated_at DESC"
    ).fetchall()
    return [SourceHealthRecord(row) for row in rows]
