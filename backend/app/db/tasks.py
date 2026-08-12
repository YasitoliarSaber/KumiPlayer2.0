# -*- coding: utf-8 -*-
"""任务状态 SQLite 存储"""

import json
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from app.db.database import close_connection, get_connection


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def save_task(task: dict) -> None:
    """保存或更新任务"""
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO tasks
        (task_id, task_type, source, status, progress, message,
         created_at, started_at, finished_at, error, result)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task["task_id"],
        task["task_type"],
        task["source"],
        task.get("status", "pending"),
        task.get("progress", 0),
        task.get("message", ""),
        task.get("created_at", _now_iso()),
        task.get("started_at"),
        task.get("finished_at"),
        task.get("error"),
        json.dumps(task.get("result", {}), ensure_ascii=False),
    ))
    conn.commit()
    close_connection()


def get_task(task_id: str) -> Optional[dict]:
    """获取任务"""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        close_connection()
        return None
    task = _row_to_task(row)
    close_connection()
    return task


def list_tasks(
    task_type: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
) -> List[dict]:
    """列出任务"""
    conn = get_connection()
    query = "SELECT * FROM tasks WHERE 1=1"
    params = []

    if task_type:
        query += " AND task_type = ?"
        params.append(task_type)
    if source:
        query += " AND source = ?"
        params.append(source)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    tasks = [_row_to_task(row) for row in rows]
    close_connection()
    return tasks


def mark_interrupted_tasks_failed() -> int:
    """将上次进程遗留的未完成任务收口为失败。

    错误信息必须让用户知道：后端已重启、扫描未完成，需要重新发起扫描，
    而不是看到一条永远卡在“运行中”的历史任务。
    """
    conn = get_connection()
    now = _now_iso()
    message = "后端已重启，扫描未完成。请重新扫描该文件夹。"
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'failed', progress = 100, message = ?,
            finished_at = ?, error = ?
        WHERE status IN ('pending', 'running')
        """,
        (message, now, message),
    )
    conn.commit()
    changed = cursor.rowcount
    close_connection()
    return max(0, int(changed or 0))


def delete_tasks(task_ids: set[str]) -> int:
    """删除指定的已完成任务历史。运行态由 TaskManager 拦截。"""
    if not task_ids:
        return 0
    conn = get_connection()
    marks = ",".join("?" for _ in task_ids)
    cursor = conn.execute(
        f"DELETE FROM tasks WHERE task_id IN ({marks}) AND status NOT IN ('pending', 'running')",
        tuple(sorted(task_ids)),
    )
    conn.commit()
    changed = max(0, int(cursor.rowcount or 0))
    close_connection()
    return changed


def update_task_status(
    task_id: str,
    status: str,
    progress: int = 0,
    message: str = "",
    error: Optional[str] = None,
    result: Optional[dict] = None,
) -> None:
    """更新任务状态"""
    conn = get_connection()
    now = _now_iso()

    if status == "running":
        conn.execute("""
            UPDATE tasks SET status = ?, progress = ?, message = ?, started_at = ?
            WHERE task_id = ?
        """, (status, progress, message, now, task_id))
    elif status in ("succeeded", "failed"):
        conn.execute("""
            UPDATE tasks SET status = ?, progress = ?, message = ?,
            finished_at = ?, error = ?, result = ?
            WHERE task_id = ?
        """, (
            status, progress, message, now, error,
            json.dumps(result or {}, ensure_ascii=False),
            task_id,
        ))
    else:
        conn.execute("""
            UPDATE tasks SET status = ?, progress = ?, message = ?
            WHERE task_id = ?
        """, (status, progress, message, task_id))

    conn.commit()
    close_connection()


def _row_to_task(row) -> dict:
    """将数据库行转为 dict"""
    return {
        "task_id": row["task_id"],
        "task_type": row["task_type"],
        "source": row["source"],
        "status": row["status"],
        "progress": row["progress"],
        "message": row["message"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "result": json.loads(row["result"]) if row["result"] else {},
    }
