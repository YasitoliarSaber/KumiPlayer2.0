"""OpenList 扫描的目录级 frontier（断点续扫状态）。

读取阶段无法预知总量，进程重启或风控中断后内存队列必丢。把“下一个要列的远端
目录”和每页游标落库，中断后可以从断点继续，而不是从根目录重扫。

状态语义：
- ``queued``：已发现、待列目录；
- ``scanning``：正在列（中断后仍算待处理，恢复时从 ``next_page`` 继续）；
- ``completed``：已列完，重复扫描同一 ``scan_id`` 时不会被重置。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.media_v4.persistence.database import V4Database

PENDING_STATUSES = ("queued", "scanning")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_directories(
    database: V4Database,
    *,
    scan_id: str,
    remote_paths,
    depth: int = 0,
) -> int:
    """登记待扫描目录，返回新增行数。

    已存在的行**保持原状态**：这是断点续扫的前提，重复调用不会把已完成的目录
    重新排队，也不会把 ``scanning`` 行退回 ``queued``。
    """

    paths = [str(path) for path in remote_paths or [] if path is not None]
    if not paths:
        return 0
    added = 0
    now = _now()
    normalized_depth = max(0, int(depth))
    with database.connect() as conn:
        for path in paths:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO source_scan_directories "
                "(scan_id, remote_path, depth, status, next_page, discovered_at, completed_at) "
                "VALUES (?, ?, ?, 'queued', 1, ?, '')",
                (scan_id, path, normalized_depth, now),
            )
            added += int(cursor.rowcount or 0)
    return added


def next_pending_directory(database: V4Database, *, scan_id: str) -> dict | None:
    """占用下一个待扫描目录（浅层优先，保证根目录先被列出）。"""

    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT remote_path, depth, next_page FROM source_scan_directories "
                "WHERE scan_id = ? AND status IN ('queued', 'scanning') "
                "ORDER BY depth, remote_path LIMIT 1",
                (scan_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE source_scan_directories SET status = 'scanning' "
                "WHERE scan_id = ? AND remote_path = ?",
                (scan_id, str(row["remote_path"])),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "remote_path": str(row["remote_path"]),
        "depth": int(row["depth"] or 0),
        "next_page": int(row["next_page"] or 1),
    }


def mark_directory(
    database: V4Database,
    *,
    scan_id: str,
    remote_path: str,
    status: str,
    next_page: int = 1,
) -> None:
    """更新目录状态；``completed`` 时记录完成时间。"""

    if status not in {"queued", "scanning", "completed"}:
        raise ValueError(f"不支持的目录状态: {status}")
    completed_at = _now() if status == "completed" else ""
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_scan_directories "
            "SET status = ?, next_page = ?, completed_at = ? "
            "WHERE scan_id = ? AND remote_path = ?",
            (status, max(1, int(next_page)), completed_at, scan_id, str(remote_path)),
        )


def directory_counts(database: V4Database, *, scan_id: str) -> dict[str, int]:
    """返回 ``{total, completed, pending}``，供进度与风控预算使用。"""

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS total FROM source_scan_directories "
            "WHERE scan_id = ? GROUP BY status",
            (scan_id,),
        ).fetchall()
    by_status = {str(row["status"]): int(row["total"] or 0) for row in rows}
    total = sum(by_status.values())
    completed = by_status.get("completed", 0)
    return {"total": total, "completed": completed, "pending": total - completed}


def clear(database: V4Database, *, scan_id: str) -> None:
    """删除某次扫描的 frontier（重置或收口时使用）。"""

    with database.connect() as conn:
        conn.execute("DELETE FROM source_scan_directories WHERE scan_id = ?", (scan_id,))
