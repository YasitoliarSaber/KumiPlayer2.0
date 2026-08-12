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

原子化与冷却保护（Review Fix 3）：
- 所有「读当前值 → 决定 transition → 写入」都在单个 ``BEGIN IMMEDIATE``
  事务内完成（:func:`_transition`）：线程本地连接 + busy_timeout=5000 把
  并发写串行化，消除 TOCTOU 竞态——冷却中并发的滞后 success / 普通失败
  结果不可能用读到的陈旧 state 覆盖刚写入的冷却；
- 冷却时长**单调不缩短**：``cooldown_until = max(当前, now + 秒数)``，
  rate_limit(1h) 不会缩短 risk_control(6h) 的安全冷却；
- 冷却原因保护：risk_control 是最强安全事件（总是覆盖冷却原因），
  其余普通结果（rate_limit / transient / irrelevant / 滞后 success）
  在冷却期间**不降级、不重写** reason_kind。

外部调用方通过 :func:`can_request` 拦截发请求前的检查；通过
:func:`record_success` / :func:`record_failure` 上报每次最终结果。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable

from app.db.database import get_connection
from app.db.transactions import transaction

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

#: 最强安全事件：总是覆盖冷却原因，其余原因不得将其降级
_RISK_CONTROL_KIND = "risk_control"


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


#: 测试注入点（仅并发回归测试使用；生产路径恒为 None）。
#: ``_transition`` 在事务内 SELECT 之后、决策/写入之前调用它，让测试可以
#: 精确制造「先读到旧值 → 并发写入冷却 → 继续用旧值写」的 TOCTOU 交错。
_test_after_read_hook: Callable[[sqlite3.Connection, str, SourceHealthRecord], None] | None = None


def _upsert(
    conn: sqlite3.Connection,
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
    """UPSERT 一行（**不提交**；由调用方事务统一提交/回滚）。"""
    now = now if now is not None else time.time()
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


def _transition(
    source_id: str,
    fn: Callable[[SourceHealthRecord], dict | None],
    *,
    now: float | None = None,
) -> SourceHealthRecord:
    """原子「读 → 决策 → 写」helper：单 ``BEGIN IMMEDIATE`` 事务内完成。

    SELECT 当前行（无记录按 healthy 默认构造）→ 调用 ``fn(current)`` 计算
    要写入的字段 → UPSERT，事务结束时统一提交；异常统一回滚。

    - ``fn(current)`` 返回与 :func:`_upsert` 同名参数的字段 dict，或
      None 表示 NO-OP（不写任何字段）；
    - 返回事务提交后重读的最新记录（NO-OP 时返回事务内读到的当前值）。

    并发线程各自持有线程本地连接；BEGIN IMMEDIATE + busy_timeout=5000
    把写事务串行化，事务内 SELECT 再 UPDATE 即原子——读取的 state 一定
    是最近一次已提交的写结果，读后不可能被并发事务改写。
    """
    now = now if now is not None else time.time()
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM source_health WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        current = (
            SourceHealthRecord(row)
            if row is not None
            else SourceHealthRecord({"source_id": source_id})
        )
        if _test_after_read_hook is not None:
            _test_after_read_hook(conn, source_id, current)
        fields = fn(current)
        if fields is not None:
            _upsert(conn, source_id, now=now, **fields)
    return get_health(source_id)


def _cooldown_fields(
    current: SourceHealthRecord,
    reason_kind: str,
    cooldown_seconds: float,
    consecutive_failures: int,
    now: float,
    last_failure_at: float,
) -> dict:
    """计算冷却写入字段：**单调不缩短** + 冷却原因防降级。

    - ``cooldown_until = max(current.cooldown_until, now + cooldown_seconds)``
      —— 429 的 1h 冷却不会覆盖 risk_control 的 6h（安全冷却不被缩短）；
    - reason：risk_control 是最强安全事件，总是覆盖；已有冷却期间非
      risk_control 的新原因（rate_limit / manual / transient 等）保持
      当前冷却原因（不降级）。
    """
    new_until = now + max(0.0, cooldown_seconds)
    if current.state == STATE_COOLING_DOWN:
        new_until = max(current.cooldown_until, new_until)
        reason = reason_kind if reason_kind == _RISK_CONTROL_KIND else current.reason_kind
    else:
        reason = reason_kind
    return {
        "state": STATE_COOLING_DOWN,
        "reason_kind": reason,
        "consecutive_failures": consecutive_failures,
        "cooldown_until": new_until,
        "last_failure_at": last_failure_at,
        "last_success_at": current.last_success_at,
    }


def record_success(source_id: str, *, now: float | None = None) -> None:
    """记录一次最终成功（原子事务内完成读-判-写）。

    - 无记录 / healthy / probe：清除 breaker（state=healthy、
      consecutive=0、cooldown_until=0、reason_kind=''），更新 last_success_at；
    - **cooling_down：只更新 last_success_at / updated_at**，保持
      state / reason_kind / consecutive_failures / cooldown_until 不变。
      冷却中到达的成功是旧 in-flight 请求的滞后结果，不得覆盖新触发的
      冷却（否则 risk_control 冷却会被并发旧请求的成功瞬间解除）；
      事务串行化保证并发交错下冷却同样不被清除。
    """
    now = now if now is not None else time.time()

    def _fn(current: SourceHealthRecord) -> dict:
        if current.state == STATE_COOLING_DOWN:
            return {
                "state": current.state,
                "reason_kind": current.reason_kind,
                "consecutive_failures": current.consecutive_failures,
                "cooldown_until": current.cooldown_until,
                "last_failure_at": current.last_failure_at,
                "last_success_at": now,
            }
        return {
            "state": STATE_HEALTHY,
            "reason_kind": "",
            "consecutive_failures": 0,
            "cooldown_until": 0.0,
            "last_failure_at": current.last_failure_at,
            "last_success_at": now,
        }

    _transition(source_id, _fn, now=now)


def enter_cooldown(
    source_id: str,
    *,
    reason_kind: str,
    cooldown_seconds: float,
    consecutive_failures: int = 0,
    last_failure_at: float | None = None,
    now: float | None = None,
) -> None:
    """显式把来源置为 cooling_down（冷却期由调用方指定秒数）。

    **单调不缩短**：若已有冷却，新冷却期取 ``max(当前, now + 秒数)``，
    较短的新冷却不会覆盖更长的安全冷却；冷却原因防降级见
    :func:`_cooldown_fields`。
    """
    now = now if now is not None else time.time()
    last_failure_at = last_failure_at if last_failure_at is not None else now

    def _fn(current: SourceHealthRecord) -> dict:
        return _cooldown_fields(
            current,
            reason_kind,
            cooldown_seconds,
            consecutive_failures,
            now,
            last_failure_at,
        )

    _transition(source_id, _fn, now=now)


def record_failure(source_id: str, kind: str, *, now: float | None = None) -> SourceHealthRecord:
    """记录一次最终失败，并按失败类型推进状态机（原子事务内完成读-判-写）。

    返回更新后的记录；调用方可通过 ``in_cooldown`` 判断是否需要停止后续请求。

    分类语义：
    - ``source_cooling_down``（本地准入拒绝）：**defensive NO-OP**，
      什么都不改直接返回当前记录——准入拒绝不是上游失败，绝不刷新/推进冷却；
    - breaker relevant：risk_control/rate_limit 立即冷却；其余累计连续失败，
      达阈值才冷却（未知 kind 归为 ``unknown``）；probe 下遇 transient 只
      累计不转 healthy（探针失败不能证明远端可达，也不解锁单探针保护）；
    - breaker irrelevant（auth/permission/not_found/validation/redirect/
      scan_limit）：不累计不冷却；**若当前处于 probe，视为探针成功证明远端
      可达 → breaker 恢复 healthy**（state=healthy、consecutive=0、
      cooldown_until=0、reason_kind 清空），业务错误仍由调用方抛出；
      其他状态仅更新 last_failure_at / reason_kind / updated_at。

    冷却期保护（Review Fix 3，修复 TOCTOU 竞态与语义覆盖）：
    - 冷却中任何普通结果（success / irrelevant / transient）都不得改写
      state / reason_kind / cooldown_until / consecutive_failures——
      冷却事实由触发它的安全事件独占，普通旧请求的滞后结果只更新时间戳；
      事务串行化保证并发交错下冷却与原因不被陈旧读值覆盖；
    - 冷却时长单调不缩短：risk_control(6h) 不会被 rate_limit(1h) 缩短；
    - 冷却中 transient **不累计**（避免污染冷却原因的统计语义，只更新时间）。
    """
    now = now if now is not None else time.time()

    def _fn(current: SourceHealthRecord) -> dict | None:
        if kind == "source_cooling_down":
            # 本地准入拒绝（冷却拦截）不是上游失败：完全 NO-OP，
            # state/reason_kind/consecutive_failures/cooldown_until/
            # last_failure_at 一律不动（client 外层已第一保险直接 re-raise，
            # 这里是第二保险）。
            return None

        if kind in BREAKER_IRRELEVANT_KINDS:
            if current.state == STATE_COOLING_DOWN:
                # 保持冷却：仅更新失败时间，不重写冷却原因/结束时间/计数
                return {
                    "state": current.state,
                    "reason_kind": current.reason_kind,
                    "consecutive_failures": current.consecutive_failures,
                    "cooldown_until": current.cooldown_until,
                    "last_failure_at": now,
                    "last_success_at": current.last_success_at,
                }
            if current.state == STATE_PROBE:
                # 探针返回 known non-transient：证明远端可达（非风控/非网络
                # 故障），breaker 恢复 healthy 并清空原因；业务错误
                # （403/404/auth 等）仍由调用方抛出。
                return {
                    "state": STATE_HEALTHY,
                    "reason_kind": "",
                    "consecutive_failures": 0,
                    "cooldown_until": 0.0,
                    "last_failure_at": now,
                    "last_success_at": current.last_success_at,
                }
            # known non-transient：不累计 circuit-breaker 计数、不触发冷却，
            # 仅记录最近一次失败原因与时间；state / cooldown_until 保持原状。
            return {
                "state": current.state,
                "reason_kind": kind,
                "consecutive_failures": current.consecutive_failures,
                "cooldown_until": current.cooldown_until,
                "last_failure_at": now,
                "last_success_at": current.last_success_at,
            }

        if kind in _IMMEDIATE_COOLDOWN_KINDS:
            cooldown = (
                RISK_CONTROL_COOLDOWN_SECONDS
                if kind == "risk_control"
                else RATE_LIMIT_COOLDOWN_SECONDS
            )
            return _cooldown_fields(
                current,
                kind,
                cooldown,
                consecutive_failures=current.consecutive_failures + 1,
                now=now,
                last_failure_at=now,
            )

        # transient 类：累计连续失败，达到阈值才冷却
        normalized = kind if kind in _TRANSIENT_KINDS else "unknown"
        if current.state == STATE_COOLING_DOWN:
            # 冷却中 transient 不累计、不重写冷却事实：只更新时间戳
            # （避免污染冷却原因的统计语义）
            return {
                "state": current.state,
                "reason_kind": current.reason_kind,
                "consecutive_failures": current.consecutive_failures,
                "cooldown_until": current.cooldown_until,
                "last_failure_at": now,
                "last_success_at": current.last_success_at,
            }
        consecutive = current.consecutive_failures + 1
        if consecutive >= TRANSIENT_FAILURE_THRESHOLD:
            return _cooldown_fields(
                current,
                normalized,
                TRANSIENT_COOLDOWN_SECONDS,
                consecutive_failures=consecutive,
                now=now,
                last_failure_at=now,
            )
        # 未达阈值：保持当前 state 继续累计（probe 下保持 probe、不转
        # healthy——探针失败不能证明远端可达，也不解锁单探针保护；
        # 达阈值时统一进入冷却）。
        return {
            "state": current.state,
            "reason_kind": normalized,
            "consecutive_failures": consecutive,
            "cooldown_until": current.cooldown_until,
            "last_failure_at": now,
            "last_success_at": current.last_success_at,
        }

    return _transition(source_id, _fn, now=now)


def peek_request_allowed(source_id: str, *, now: float | None = None) -> tuple[bool, SourceHealthRecord]:
    """只读准入预检：**绝不修改任何状态、绝不消费探针**。

    返回 (allowed, record)：
    - 无记录 / healthy → (True, record)；
    - cooling_down 且未到期 → (False, record)；
    - cooling_down 且已到期 → (True, record)，**不改 state**——探针留给
      物理 HTTP 请求前的 :func:`can_request` 原子抢占（上层预检不得抢
      占探针，否则真实客户端物理请求前会被自己拦截）；
    - probe → (False, record)。

    用于任务开始前的低成本拦截（discovery handler / API 入口）：冷却中
    直接拒绝、零网络请求；冷却已到期时放行，由唯一的消费入口
    :func:`can_request` 在真正发请求前抢占探针。
    """
    now = now if now is not None else time.time()
    record = get_health(source_id)
    if record.state == STATE_COOLING_DOWN:
        if now < record.cooldown_until:
            return False, record
        return True, record
    if record.state == STATE_PROBE:
        return False, record
    return True, record


def can_request(source_id: str, *, now: float | None = None) -> tuple[bool, SourceHealthRecord]:
    """判断当前是否允许向该来源发请求（**唯一消费探针的入口**）。

    **只应在物理 HTTP 请求前调用**；任务开始前的预检一律使用
    :func:`peek_request_allowed`，避免上层预检抢占唯一探针许可。

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
