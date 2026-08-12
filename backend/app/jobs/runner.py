"""持久任务 worker（单线程领取循环）。

- 启动时把租约过期的 running 任务重新排队（不粗暴标 failed）；
- 领取 → 执行 handler → 终态（succeeded/failed/cancelled/waiting_review）；
- 可重试失败按 attempt 重排；达到 max_attempts 后标 failed；
- 执行期间定时续租并汇报进度；取消为协作式检查；
- 旧 worker 提交终态被版本 fence 拒绝（store.finish_job 保证）。
"""

from __future__ import annotations

import threading
import time
import uuid

from app.jobs import store
from app.jobs.models import (
    CANCELLED,
    FAILED,
    RETRYABLE_ERROR_TYPES,
    SUCCEEDED,
    Job,
    JobCancelledError,
)
from app.jobs.registry import get_handler


class JobRunner:
    def __init__(
        self,
        *,
        worker_id: str = "",
        poll_interval: float = 0.5,
        claim_limit: int = 1,
        job_types: list[str] | None = None,
    ):
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.claim_limit = claim_limit
        # 独立 worker 分流：只领取指定 job_type（空 = 任意类型）
        self.job_types = job_types
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- 生命周期 ----------------------------------------------------

    def start(self) -> None:
        """启动前恢复租约过期任务，然后开始领取循环。"""
        store.requeue_expired_leases()
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"durable-jobs-{self.worker_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # -- 主循环 ------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                claimed = store.claim_jobs(
                    self.worker_id, limit=self.claim_limit, job_types=self.job_types,
                )
            except Exception:
                claimed = []
            for job in claimed:
                if self._stop_event.is_set():
                    # 退出前把未完成任务重新排队
                    store.requeue_expired_leases()
                    return
                self._execute(job)
            time.sleep(self.poll_interval)

    # -- 单任务执行 --------------------------------------------------

    def _execute(self, job: Job) -> None:
        handler = get_handler(job.job_type)
        if handler is None:
            store.finish_job(
                job.job_id, self.worker_id, FAILED,
                error=f"handler 不可恢复（未注册）: {job.job_type}",
                version=job.version,
            )
            return

        last_renew = time.monotonic()
        store.record_attempt(
            job.job_id, job.attempt,
            started_at=job.started_at or store.now_iso(),
        )

        def progress_callback(progress: int, message: str = "", result_patch: dict | None = None) -> None:
            nonlocal last_renew
            store.update_progress(job.job_id, self.worker_id, progress, message)
            if result_patch:
                store.merge_result(job.job_id, self.worker_id, result_patch)
            now = time.monotonic()
            if now - last_renew >= store.LEASE_RENEW_SECONDS:
                store.renew_lease(job.job_id, self.worker_id)
                last_renew = now

        def should_cancel() -> bool:
            return store.should_cancel(job.job_id)

        try:
            result = handler(
                job.payload,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            ) or {}
            if store.should_cancel(job.job_id):
                store.finish_job(
                    job.job_id, self.worker_id, CANCELLED,
                    message="任务已取消", version=job.version,
                )
                return
            store.finish_job(
                job.job_id, self.worker_id, SUCCEEDED,
                result=result if isinstance(result, dict) else {},
                version=job.version,
            )
        except JobCancelledError:
            store.finish_job(
                job.job_id, self.worker_id, CANCELLED,
                message="任务已取消", version=job.version,
            )
        except Exception as exc:
            error_type = getattr(exc, "kind", "") or type(exc).__name__.lower()
            retryable = error_type in RETRYABLE_ERROR_TYPES or isinstance(exc, (TimeoutError, ConnectionError))
            store.record_attempt(
                job.job_id, job.attempt,
                started_at=job.started_at or store.now_iso(),
                finished_at=store.now_iso(),
                error_type=error_type,
                retryable=retryable,
            )
            if retryable and job.attempt + 1 < job.max_attempts:
                store.retry_job(job.job_id, self.worker_id, error=str(exc), error_type=error_type)
            else:
                store.finish_job(
                    job.job_id, self.worker_id, FAILED,
                    error=str(exc), version=job.version,
                )


def merge_result(job_id: str, result: dict) -> None:
    """把结果合并进 payload（供 handler 或 façade 使用）。"""
    current = store.get_job(job_id)
    if current is None:
        return
    payload = dict(current.payload)
    merged = dict(payload.get("result") or {})
    merged.update(result)
    payload["result"] = merged
    store.update_payload(job_id, payload)
