"""持久任务队列存储（SQLite）。

- 原子租约领取：``UPDATE ... WHERE status='queued'`` 且同 ``resource_key`` 无 running；
- 租约过期恢复：worker 启动时把超时 running 重新排队，不粗暴标 failed；
- 版本 fence：旧 worker 不能提交终态（``WHERE version = expected``）；
- 重试生成 attempt，不新建重复任务。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.database import get_connection
from app.db.transactions import transaction
from app.jobs.models import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    WAITING_REVIEW,
    Job,
)

#: 租约时长（秒）
LEASE_SECONDS = 60
#: 租约续期间隔（秒）
LEASE_RENEW_SECONDS = 20


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _row_to_job(row) -> Job | None:
    if row is None:
        return None
    data = dict(row)
    return Job(
        job_id=data["job_id"],
        job_type=data["job_type"],
        resource_key=data.get("resource_key") or "",
        payload=json.loads(data.get("payload") or "{}"),
        status=data["status"],
        priority=data.get("priority") or 0,
        parent_job_id=data.get("parent_job_id") or "",
        attempt=data.get("attempt") or 0,
        max_attempts=data.get("max_attempts") or 3,
        not_before=data.get("not_before") or "",
        lease_owner=data.get("lease_owner") or "",
        lease_until=data.get("lease_until") or "",
        cancel_requested=bool(data.get("cancel_requested")),
        progress=data.get("progress") or 0,
        message=data.get("message") or "",
        error=data.get("error") or "",
        version=data.get("version") or 0,
        created_at=data.get("created_at") or "",
        updated_at=data.get("updated_at") or "",
    )


def _job_params(job: Job) -> tuple[Any, ...]:
    return (
        job.job_id, job.job_type, job.resource_key,
        json.dumps(job.payload, ensure_ascii=False),
        job.status, job.priority, job.parent_job_id, job.attempt,
        job.max_attempts, job.not_before, job.lease_owner, job.lease_until,
        int(job.cancel_requested), job.progress, job.message, job.error,
        job.version, job.created_at, job.updated_at,
    )


def _insert_job(conn, job: Job) -> None:
    """插入 job 行（调用方负责事务边界与提交）。"""
    from app.catalog import maintenance_guard

    if maintenance_guard.is_active():
        raise RuntimeError("媒体库维护进行中，暂不接受新的任务")
    conn.execute(
        """
        INSERT INTO jobs (
            job_id, job_type, resource_key, payload, status, priority,
            parent_job_id, attempt, max_attempts, not_before, lease_owner,
            lease_until, cancel_requested, progress, message, error, version,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _job_params(job),
    )


def create_job(
    *,
    job_type: str,
    resource_key: str = "",
    payload: dict | None = None,
    parent_job_id: str = "",
    priority: int = 0,
    max_attempts: int = 3,
    not_before: str = "",
) -> Job:
    conn = get_connection()
    timestamp = now_iso()
    job = Job(
        job_id=uuid.uuid4().hex,
        job_type=job_type,
        resource_key=resource_key,
        payload=payload or {},
        status=QUEUED,
        priority=priority,
        parent_job_id=parent_job_id,
        max_attempts=max_attempts,
        not_before=not_before,
        created_at=timestamp,
        updated_at=timestamp,
    )
    _insert_job(conn, job)
    conn.commit()
    return job


def get_or_create_job(
    *,
    job_type: str,
    resource_key: str,
    payload: dict | None = None,
    parent_job_id: str = "",
    priority: int = 0,
    max_attempts: int = 3,
    not_before: str = "",
) -> tuple[Job, bool]:
    """按 job_type + resource_key 幂等取回/创建任务（get-or-create）。

    BEGIN IMMEDIATE 事务内先查 exact job_type + resource_key（任意状态，含
    succeeded/failed/cancelled/queued/running），存在则复用原 job（created=False）；
    不存在则 INSERT 新 job（created=True）。并发调用（如重复 confirm）只会得到
    同一 job 身份，不会双写。
    """
    conn = get_connection()
    timestamp = now_iso()
    with transaction(conn) as tx:
        row = tx.execute(
            "SELECT * FROM jobs WHERE job_type = ? AND resource_key = ? LIMIT 1",
            (job_type, resource_key),
        ).fetchone()
        if row is not None:
            job = _row_to_job(row)
            if job is None:  # pragma: no cover - 理论不可达（行存在即可解析）
                raise ValueError(f"无法解析已存在 job: {job_type}/{resource_key}")
            return job, False
        job = Job(
            job_id=uuid.uuid4().hex,
            job_type=job_type,
            resource_key=resource_key,
            payload=payload or {},
            status=QUEUED,
            priority=priority,
            parent_job_id=parent_job_id,
            max_attempts=max_attempts,
            not_before=not_before,
            created_at=timestamp,
            updated_at=timestamp,
        )
        _insert_job(tx, job)
    return job, True


def get_job(job_id: str) -> Job | None:
    row = get_connection().execute(
        "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return _row_to_job(row)


def enqueue_coalesced_job(
    *,
    job_type: str,
    resource_key: str,
    payload: dict | None = None,
    parent_job_id: str = "",
    priority: int = 0,
    max_attempts: int = 3,
    not_before: str = "",
) -> tuple[Job, bool]:
    """事务安全的合并入队：同一 (job_type, resource_key) 最多
    1 running + 1 trailing queued，既不任务风暴也不丢变化。

    - 已有 queued → 复用（created=False）；
    - 已有 running、无 queued → 创建 trailing queued（running 的 snapshot
      可能早于新变化，必须补一个）；
    - 已有 running + queued → 复用 queued；
    - 只有终态历史 job（succeeded/failed/cancelled）→ 创建新 job。

    与 get_or_create_job 的区别：绝不复用终态 job（否则"一个月前 succeeded
    的 job"会让新变化永远不被重建）。
    """
    conn = get_connection()
    timestamp = now_iso()
    with transaction(conn) as tx:
        queued = tx.execute(
            """
            SELECT * FROM jobs
            WHERE job_type = ? AND resource_key = ? AND status = 'queued'
            LIMIT 1
            """,
            (job_type, resource_key),
        ).fetchone()
        if queued is not None:
            job = _row_to_job(queued)
            if job is None:  # pragma: no cover - 行存在即可解析
                raise ValueError(f"无法解析已存在 job: {job_type}/{resource_key}")
            return job, False
        job = Job(
            job_id=uuid.uuid4().hex,
            job_type=job_type,
            resource_key=resource_key,
            payload=payload or {},
            status=QUEUED,
            priority=priority,
            parent_job_id=parent_job_id,
            max_attempts=max_attempts,
            not_before=not_before,
            created_at=timestamp,
            updated_at=timestamp,
        )
        _insert_job(tx, job)
    return job, True


def list_jobs(
    job_type: str = "",
    status: str = "",
    limit: int = 100,
) -> list[Job]:
    clauses: list[str] = []
    params: list[Any] = []
    if job_type:
        clauses.append("job_type = ?")
        params.append(job_type)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = get_connection().execute(
        f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?", params
    ).fetchall()
    return [item for row in rows if (item := _row_to_job(row)) is not None]


def list_discovery_jobs_for_root(root_id: str, *, limit: int = 20) -> list[Job]:
    """按持久 payload 找到某个 source root 的 discovery jobs。"""
    rows = get_connection().execute(
        """
        SELECT * FROM jobs
        WHERE job_type = 'discovery_scan'
          AND payload LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (f'%"root_id": "{root_id}"%', limit),
    ).fetchall()
    return [item for row in rows if (item := _row_to_job(row)) is not None]


def _is_valid_claim(row) -> bool:
    return row is not None and row["status"] == QUEUED


def claim_jobs(
    worker_id: str, limit: int = 1, *, now: str | None = None,
    job_types: list[str] | None = None,
) -> list[Job]:
    """原子领取可执行任务（同 resource_key 已有 running 时不可领取）。

    job_types 用于独立 worker 分流（scan/mirror/scrape 各自专用线程）；
    为空表示可领取任意类型。
    """
    from app.catalog import maintenance_guard

    # 整库维护屏障：删除进行中不领取任何任务，保证删除窗口内无新的 running 任务
    if maintenance_guard.is_active():
        return []
    conn = get_connection()
    now = now or now_iso()
    lease_until = (
        datetime.fromisoformat(now) + timedelta(seconds=LEASE_SECONDS)
    ).isoformat()
    jobs: list[Job] = []
    with transaction(conn) as tx:
        type_clause = ""
        type_params: list[str] = []
        if job_types:
            type_clause = "AND job_type IN ({}) ".format(
                ", ".join("?" for _ in job_types)
            )
            type_params = list(job_types)
        rows = tx.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'queued'
              AND (not_before = '' OR not_before <= ?)
              AND cancel_requested = 0
              """ + type_clause + """AND (
                  resource_key = ''
                  OR NOT EXISTS (
                      SELECT 1 FROM jobs AS j2
                      WHERE j2.resource_key = jobs.resource_key
                        AND j2.status = 'running'
                  )
              )
              AND (
                  parent_job_id = ''
                  OR NOT EXISTS (
                      SELECT 1 FROM jobs AS p
                      WHERE p.job_id = jobs.parent_job_id
                        AND p.status IN ('queued', 'running')
                  )
              )
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (now, *type_params, limit),
        ).fetchall()
        for row in rows:
            if not _is_valid_claim(row):
                continue
            current = _row_to_job(row)
            payload = dict(current.payload) if current else {}
            payload["started_at"] = now
            # UPDATE 再次校验资源互斥与版本，避免同批次多个同 key 任务同时领取
            cursor = tx.execute(
                """
                UPDATE jobs
                SET status = 'running', lease_owner = ?, lease_until = ?,
                    payload = ?, version = version + 1, updated_at = ?
                WHERE job_id = ? AND status = 'queued' AND version = ?
                  AND (
                      resource_key = ''
                      OR NOT EXISTS (
                          SELECT 1 FROM jobs AS j2
                          WHERE j2.resource_key = jobs.resource_key
                            AND j2.status = 'running'
                            AND j2.job_id != jobs.job_id
                      )
                  )
                """,
                (worker_id, lease_until, json.dumps(payload, ensure_ascii=False), now, row["job_id"], row["version"]),
            )
            if cursor.rowcount == 0:
                continue
            claimed = tx.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            job = _row_to_job(claimed)
            if job is not None:
                jobs.append(job)
    return jobs


def renew_lease(job_id: str, worker_id: str, *, now: str | None = None) -> bool:
    """租约续期（版本 fence 防止旧 worker 续期已移交的任务）。"""
    conn = get_connection()
    now = now or now_iso()
    lease_until = (
        datetime.fromisoformat(now) + timedelta(seconds=LEASE_SECONDS)
    ).isoformat()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET lease_until = ?, updated_at = ?
        WHERE job_id = ? AND lease_owner = ? AND status = 'running'
        """,
        (lease_until, now, job_id, worker_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def update_progress(job_id: str, worker_id: str, progress: int, message: str) -> bool:
    conn = get_connection()
    cursor = conn.execute(
        """
        UPDATE jobs SET progress = ?, message = ?, updated_at = ?
        WHERE job_id = ? AND lease_owner = ? AND status = 'running'
        """,
        (max(0, min(100, int(progress))), message, now_iso(), job_id, worker_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def finish_job(
    job_id: str,
    worker_id: str,
    status: str,
    *,
    error: str = "",
    result: dict | None = None,
    progress: int = 100,
    message: str = "",
    version: int = 0,
) -> bool:
    """提交终态；版本 fence：只有持有租约的当前 worker 且版本匹配才生效。"""
    conn = get_connection()
    payload = get_job(job_id)
    merged = dict(payload.payload) if payload else {}
    if result:
        merged["result"] = result
    if status in (SUCCEEDED, FAILED, CANCELLED, WAITING_REVIEW):
        merged["finished_at"] = now_iso()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET status = ?, payload = ?, progress = ?, message = ?, error = ?,
            version = version + 1, lease_owner = '', lease_until = '',
            updated_at = ?
        WHERE job_id = ? AND lease_owner = ? AND status = 'running' AND version = ?
        """,
        (
            status, json.dumps(merged, ensure_ascii=False), progress, message, error,
            now_iso(), job_id, worker_id, version,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def retry_job(job_id: str, worker_id: str, *, error: str, error_type: str) -> bool:
    """可重试失败：attempt+1 后重新排队（不新建业务任务）。"""
    conn = get_connection()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', attempt = attempt + 1, error = ?,
            lease_owner = '', lease_until = '', version = version + 1,
            updated_at = ?
        WHERE job_id = ? AND lease_owner = ? AND status = 'running'
        """,
        (error, now_iso(), job_id, worker_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def defer_job(
    job_id: str,
    worker_id: str,
    *,
    until_unix: float,
    message: str = "",
    error: str = "",
) -> bool:
    """来源冷却延后：回到 queued 并设置 not_before（延后领取）。

    与 retry_job 的关键区别：**不增加 attempt、不标 failed、不标
    succeeded**。冷却结束后 ``claim_jobs`` 的 ``not_before <= now``
    条件放行，任务自动重新领取执行。

    ``not_before`` 时间格式与 ``now_iso`` 一致（+08:00 ISO 字符串），
    保证与 claim 时的字符串比较语义一致。
    """
    conn = get_connection()
    not_before = datetime.fromtimestamp(
        max(0.0, float(until_unix)), tz=timezone(timedelta(hours=8))
    ).isoformat()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', not_before = ?, message = ?, error = ?,
            lease_owner = '', lease_until = '', version = version + 1,
            updated_at = ?
        WHERE job_id = ? AND lease_owner = ? AND status = 'running'
        """,
        (not_before, message, error, now_iso(), job_id, worker_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def cancel_job(job_id: str) -> bool:
    """请求取消：queued 直接终态 cancelled；running 置 cancel_requested（协作式）。"""
    conn = get_connection()
    job = get_job(job_id)
    if job is None:
        return False
    if job.status == QUEUED:
        cursor = conn.execute(
            """
            UPDATE jobs SET status = 'cancelled', version = version + 1, updated_at = ?
            WHERE job_id = ? AND status = 'queued'
            """,
            (now_iso(), job_id),
        )
    elif job.status == RUNNING:
        cursor = conn.execute(
            """
            UPDATE jobs SET cancel_requested = 1, message = '正在停止', updated_at = ?
            WHERE job_id = ?
            """,
            (now_iso(), job_id),
        )
    else:
        return False
    conn.commit()
    return cursor.rowcount > 0


def should_cancel(job_id: str) -> bool:
    row = get_connection().execute(
        "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return bool(row and row["cancel_requested"])


def requeue_expired_leases(*, now: str | None = None) -> int:
    """把租约过期的 running 任务重新排队（不标 failed），返回恢复数量。"""
    conn = get_connection()
    now = now or now_iso()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', lease_owner = '', lease_until = '',
            message = 'worker 中断后恢复', version = version + 1, updated_at = ?
        WHERE status = 'running' AND lease_until != '' AND lease_until < ?
        """,
        (now, now),
    )
    conn.commit()
    return cursor.rowcount


def update_payload(job_id: str, payload: dict, *, worker_id: str = "") -> bool:
    """更新任务 payload（可带租约 owner 约束）。"""
    conn = get_connection()
    if worker_id:
        cursor = conn.execute(
            """
            UPDATE jobs SET payload = ?, updated_at = ?
            WHERE job_id = ? AND lease_owner = ? AND status = 'running'
            """,
            (json.dumps(payload, ensure_ascii=False), now_iso(), job_id, worker_id),
        )
    else:
        cursor = conn.execute(
            "UPDATE jobs SET payload = ?, updated_at = ? WHERE job_id = ?",
            (json.dumps(payload, ensure_ascii=False), now_iso(), job_id),
        )
    conn.commit()
    return cursor.rowcount > 0


def merge_result(job_id: str, worker_id: str, result: dict) -> bool:
    """把 result_patch 合并进 payload.result（用于进度透传）。"""
    current = get_job(job_id)
    if current is None:
        return False
    payload = dict(current.payload)
    merged = dict(payload.get("result") or {})
    merged.update(result)
    payload["result"] = merged
    return update_payload(job_id, payload, worker_id=worker_id)


def record_attempt(
    job_id: str,
    attempt: int,
    *,
    started_at: str,
    finished_at: str = "",
    error_type: str = "",
    retryable: bool = False,
) -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO job_attempts (
            attempt_id, job_id, attempt, started_at, finished_at, error_type, retryable
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uuid.uuid4().hex, job_id, attempt, started_at, finished_at, error_type, int(retryable)),
    )
    conn.commit()
