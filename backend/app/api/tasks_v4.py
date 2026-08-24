"""V4 jobs 查询与取消入口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.media_v4 import get_database

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_payload(row) -> dict:
    status = "pending" if row["status"] == "queued" else row["status"]
    return {
        "task_id": row["job_id"],
        "task_type": row["job_type"],
        "source": row["provider"] or "",
        "status": status,
        "progress": 1 if status == "succeeded" else 0,
        "message": row["last_error"] if status == "failed" else status,
        "created_at": row["created_at"],
        "started_at": row["updated_at"] if row["status"] == "running" else "",
        "finished_at": row["updated_at"] if status in {"succeeded", "failed", "cancelled"} else "",
        "error": row["last_error"] or "",
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
        if row["status"] == "running":
            raise HTTPException(status_code=409, detail="运行中的任务不能安全取消")
        if row["status"] == "queued":
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = datetime('now') WHERE job_id = ?",
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
        if conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = ?",
            (row["revision_id"],),
        ).fetchone()[0] != "confirmed":
            raise HTTPException(status_code=409, detail="任务所属 revision 已失效，不能重试")
        conn.execute(
            "UPDATE jobs SET status = 'queued', last_error = '', updated_at = datetime('now') WHERE job_id = ?",
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
