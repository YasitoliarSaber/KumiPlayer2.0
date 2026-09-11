"""V4 jobs 查询与取消入口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.media_v4 import get_database
from app.media_v4.revisions.service import _friendly_job_error

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_TASK_STATUS_MESSAGES = {
    "pending": "等待任务执行",
    "running": "任务进行中",
    "succeeded": "任务已完成",
    "cancelled": "任务已终止",
}


def _task_payload(row) -> dict:
    status = "pending" if row["status"] == "queued" else row["status"]
    cancelling = bool(row["cancel_requested"]) and status in {"pending", "running"}
    user_error = _friendly_job_error(str(row["last_error"] or ""))
    return {
        "task_id": row["job_id"],
        "task_type": row["job_type"],
        "source": row["provider"] or "",
        "status": status,
        "progress": 1 if status == "succeeded" else 0,
        "message": user_error if status == "failed" else ("正在终止" if cancelling else _TASK_STATUS_MESSAGES.get(status, "任务状态未知")),
        "created_at": row["created_at"],
        "started_at": row["started_at"] if row["status"] == "running" else "",
        "finished_at": row["finished_at"] if status in {"succeeded", "failed", "cancelled"} else "",
        "error": user_error,
        "cancel_requested": cancelling,
        "result": {
            "revision_id": row["revision_id"],
            "work_id": row["work_id"],
        },
    }


def _rows(*, source: str | None = None, task_type: str | None = None, type_prefix: str | None = None):
    query = """
        SELECT j.*, sr.provider
        FROM jobs j
        JOIN import_revisions ir ON ir.revision_id = j.revision_id
        JOIN source_roots sr ON sr.root_id = ir.root_id
        WHERE 1 = 1
    """
    params: list[str] = []
    if source and source != "all":
        query += " AND sr.provider = ?"
        params.append(source)
    if task_type:
        query += " AND j.job_type = ?"
        params.append(task_type)
    if type_prefix:
        query += " AND j.job_type LIKE ?"
        params.append(type_prefix + "%")
    query += " ORDER BY j.created_at DESC, j.job_id DESC"
    with get_database().connect() as conn:
        return conn.execute(query, params).fetchall()


@router.get("")
def list_tasks(
    source: str | None = None,
    task_type: str | None = None,
    type_prefix: str | None = None,
    limit: int = 20,
):
    safe_limit = max(1, min(int(limit or 20), 100))
    return {"tasks": [_task_payload(row) for row in _rows(
        source=source,
        task_type=task_type,
        type_prefix=type_prefix,
    )[:safe_limit]]}


@router.get("/{task_id}")
def get_task(task_id: str):
    with get_database().connect() as conn:
        row = conn.execute(
            """
            SELECT j.*, sr.provider
            FROM jobs j
            JOIN import_revisions ir ON ir.revision_id = j.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE j.job_id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _task_payload(row)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    with get_database().connect() as conn:
        row = conn.execute(
            """
            SELECT j.*, sr.provider
            FROM jobs j
            JOIN import_revisions ir ON ir.revision_id = j.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE j.job_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if row["status"] in {"queued", "running"}:
        # 一条 revision 的 outbox 是同一份来源导入的下游链；不能只停当前
        # worker 而把依赖任务留成永久 queued。运行中任务采用协作式终止。
        from app.media_v4.jobs.runner import V4JobRunner

        V4JobRunner(get_database()).cancel_revision(str(row["revision_id"]))
    with get_database().connect() as conn:
        row = conn.execute(
            """
            SELECT j.*, sr.provider
            FROM jobs j
            JOIN import_revisions ir ON ir.revision_id = j.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE j.job_id = ?
            """,
            (task_id,),
        ).fetchone()
    return _task_payload(row)


@router.post("/{task_id}/retry")
def retry_task(task_id: str):
    with get_database().connect() as conn:
        row = conn.execute(
            """
            SELECT j.*, sr.provider
            FROM jobs j
            JOIN import_revisions ir ON ir.revision_id = j.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE j.job_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
        if row["status"] not in {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="只有失败或已取消任务可以重试")
        if row["work_id"] and conn.execute(
            "SELECT 1 FROM works WHERE work_id = ? AND status = 'active'",
            (row["work_id"],),
        ).fetchone() is None:
            raise HTTPException(status_code=409, detail="任务对应的作品已退出媒体库，不能重试")
        if conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = ?",
            (row["revision_id"],),
        ).fetchone()[0] != "confirmed":
            raise HTTPException(status_code=409, detail="任务所属 revision 已失效，不能重试")
        if row["status"] == "cancelled":
            # 终止操作按 revision 收口：运行中任务协作停止，尚未开始的依赖
            # 同时转成 cancelled。只重试用户点击的一项会让投影继续等待仍
            # 被取消的前置/后置任务，来源卡表面恢复却永远无法完成。
            conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', cancel_requested = 0, heartbeat_at = '',
                    started_at = '', finished_at = '', last_error = '', updated_at = datetime('now')
                WHERE revision_id = ? AND status = 'cancelled'
                  AND (
                    work_id = '' OR EXISTS (
                      SELECT 1 FROM works w
                      WHERE w.work_id = jobs.work_id AND w.status = 'active'
                    )
                  )
                """,
                (row["revision_id"],),
            )
        else:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', cancel_requested = 0, heartbeat_at = '',
                    started_at = '', finished_at = '', last_error = '', updated_at = datetime('now')
                WHERE job_id = ?
                """,
                (task_id,),
            )
        row = conn.execute(
            """
            SELECT j.*, sr.provider
            FROM jobs j
            JOIN import_revisions ir ON ir.revision_id = j.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE j.job_id = ?
            """,
            (task_id,),
        ).fetchone()
    return _task_payload(row)
