"""持久任务队列数据模型。

状态机固定为 ``queued → running → succeeded/failed/cancelled/waiting_review``；
重试生成 attempt，不新建重复业务任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class JobCancelledError(RuntimeError):
    """任务被取消（协作式）；正常终态，不属于执行失败。"""


class JobDeferredError(RuntimeError):
    """任务因来源级风控/冷却被延后（不是失败，也不是成功）。

    runner 捕获后调用 ``store.defer_job``：回到 ``queued`` 并写入
    ``not_before``，**不消耗 attempt、不标 failed、不标 succeeded**；
    冷却结束后由 claim 循环自动重新领取执行。
    """

    def __init__(self, until_unix: float, message: str = "任务已延后，冷却结束后自动重试"):
        super().__init__(message)
        #: 允许重新领取的 Unix 时间戳（秒）；对应 jobs.not_before
        self.until_unix = float(until_unix)
        self.message = str(message)


#: 任务状态
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
WAITING_REVIEW = "waiting_review"

JOB_STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED, WAITING_REVIEW)

#: 可重试错误类型（网络/限流/瞬时）
RETRYABLE_ERROR_TYPES = {"network", "rate_limit", "timeout", "transient"}


@dataclass
class Job:
    job_id: str = ""
    job_type: str = ""
    resource_key: str = ""
    payload: dict = field(default_factory=dict)
    status: str = QUEUED
    priority: int = 0
    parent_job_id: str = ""
    attempt: int = 0
    max_attempts: int = 3
    not_before: str = ""
    lease_owner: str = ""
    lease_until: str = ""
    cancel_requested: bool = False
    progress: int = 0
    message: str = ""
    error: str = ""
    version: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_record_dict(self) -> dict[str, Any]:
        """映射为 TaskRecord 兼容形状（tasks/manager façade 用）。"""
        return {
            "task_id": self.job_id,
            "task_type": self.job_type,
            "source": str(self.payload.get("source") or ""),
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at or None,
            "finished_at": self.finished_at or None,
            "error": self.error or None,
            "result": self.result or {},
        }

    @property
    def started_at(self) -> str:
        return str(self.payload.get("started_at") or "")

    @property
    def finished_at(self) -> str:
        return str(self.payload.get("finished_at") or "")

    @property
    def result(self) -> dict:
        value = self.payload.get("result")
        return value if isinstance(value, dict) else {}
