"""SourceScan 持久阶段与恢复常量（C 段合同）。

扫描的执行真相只存在于 SQLite：阶段、计数、心跳、取消标记都由领取者
或恢复器以条件更新写入。这里集中定义阈值与阶段语义，供 runner、
durable_scan 状态读取和来源卡投影共用；测试用可注入时钟验证，不允许
真的等待阈值时间。
"""

from __future__ import annotations

from datetime import UTC, datetime

#: 进程失联判定阈值；沿用普通 job 的 recover_stale_jobs 语义。
STALE_SCAN_AFTER_SECONDS = 120

#: 活动执行者的心跳刷新上限。
SCAN_HEARTBEAT_INTERVAL_SECONDS = 5

#: 进度写入节流：同阶段内最多每 250ms 或每 128 条写一次。
SCAN_PROGRESS_MIN_INTERVAL_SECONDS = 0.25
SCAN_PROGRESS_MIN_ITEMS = 128

INTERRUPTED_STAGE_LABEL = "上次扫描意外中断，请重新扫描"


def parse_scan_timestamp(raw: str | None) -> datetime | None:
    """解析 ISO 时间戳；空串/非法值返回 None，由调用方决定保守语义。"""

    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def scan_is_stale(
    heartbeat_at: str | None,
    *,
    now_ts: float | None = None,
    max_age_seconds: int = STALE_SCAN_AFTER_SECONDS,
) -> bool:
    """心跳缺失或超过阈值即视为失联。

    心跳为空/非法的行按失联处理：没有执行者会长时间不刷新心跳，这些行
    永远不可能自行恢复为终态。
    """

    parsed = parse_scan_timestamp(heartbeat_at)
    if parsed is None:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now_ts if now_ts is not None else datetime.now(UTC).timestamp()
    try:
        stamp = parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return True
    return stamp <= current - max(1, max_age_seconds)
