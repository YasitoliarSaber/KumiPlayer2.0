"""HYB-6：OpenList 请求成本遥测（KumiPlayer → OpenList 方向）。

只统计本应用真实发出的物理请求次数（fs/list、login 等），
**不冒充上游网盘真实配额消耗**——OpenList 可能命中自身缓存，
因此实际网盘上游请求可能更少。UI 文案必须写明是
「KumiPlayer → OpenList 请求」，仅作访问风险参考值。

数据落 SQLite（openlist_telemetry 表，按 conn_hash + day + operation
聚合），restart 不丢；只增不减，保留自然滚动（可按需清理旧日期）。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from app.db.database import get_connection

#: 本地时区（与项目其余部分一致：UTC+8）
_LOCAL_TZ = timezone(timedelta(hours=8))

#: 遥测操作分类
OP_FS_LIST = "fs/list"
OP_LOGIN = "login"


def _today() -> str:
    return datetime.now(_LOCAL_TZ).strftime("%Y-%m-%d")


def record_request(conn_hash: str, operation: str) -> None:
    """记录一次 KumiPlayer → OpenList 物理请求（幂等累加，失败静默）。

    遥测不得影响请求主路径：任何数据库异常都吞掉，只保证计数尽力而为。
    """
    if not conn_hash or not operation:
        return
    try:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO openlist_telemetry (conn_hash, day, operation, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(conn_hash, day, operation)
            DO UPDATE SET count = count + 1
            """,
            (conn_hash, _today(), operation),
        )
        conn.commit()
    except Exception:
        # 遥测尽力而为：数据库暂不可用时不影响请求链路
        pass


def daily_counts(conn_hash: str, day: str = "") -> dict[str, int]:
    """某连接某天的遥测计数，返回 {operation: count}（无记录返回空 dict）。"""
    day = day or _today()
    rows = get_connection().execute(
        """
        SELECT operation, count FROM openlist_telemetry
        WHERE conn_hash = ? AND day = ?
        """,
        (conn_hash, day),
    ).fetchall()
    return {str(row["operation"]): int(row["count"]) for row in rows}


def daily_summary(conn_hash: str, day: str = "") -> dict:
    """今日请求摘要（供来源卡展示）。

    - fs_list / login：KumiPlayer → OpenList 真实物理请求次数；
    - disclaimer：固定提示文案（OpenList 可能命中自身缓存）。
    """
    counts = daily_counts(conn_hash, day)
    return {
        "fs_list": counts.get(OP_FS_LIST, 0),
        "login": counts.get(OP_LOGIN, 0),
        "total": counts.get(OP_FS_LIST, 0) + counts.get(OP_LOGIN, 0),
        "disclaimer": (
            "这是 KumiPlayer → OpenList 的请求次数（访问风险参考值）；"
            "OpenList 可能命中自身缓存，因此实际网盘上游请求可能更少。"
        ),
    }
