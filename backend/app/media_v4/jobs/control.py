"""V4 outbox 的短事务控制原语。

业务执行器不持有数据库连接做网络或文件 I/O；它们只在每个可恢复边界
读取取消标记、写入心跳，并由这里统一完成状态转换。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.media_v4.persistence.database import V4Database


def now() -> str:
    return datetime.now(UTC).isoformat()


def claim_running(database: V4Database, job_id: str) -> bool:
    stamp = now()
    with database.connect() as conn:
        cursor = conn.execute(
            """
            UPDATE jobs
            SET status = 'running', attempts = attempts + 1,
                started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                heartbeat_at = ?, updated_at = ?
            WHERE job_id = ? AND status = 'queued' AND cancel_requested = 0
            """,
            (stamp, stamp, stamp, job_id),
        )
    return cursor.rowcount == 1


def cancel_requested(database: V4Database, job_id: str) -> bool:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return row is not None and bool(row["cancel_requested"])


def heartbeat(database: V4Database, job_id: str) -> None:
    stamp = now()
    with database.connect() as conn:
        conn.execute(
            "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ? AND status = 'running'",
            (stamp, stamp, job_id),
        )


def mark_cancelled(database: V4Database, job_id: str) -> None:
    stamp = now()
    with database.connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled', last_error = '', heartbeat_at = ?, finished_at = ?, updated_at = ?
            WHERE job_id = ? AND status IN ('queued', 'running')
            """,
            (stamp, stamp, stamp, job_id),
        )
