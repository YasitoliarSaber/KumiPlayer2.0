"""内存任务管理器

第一版使用进程内内存 registry，不要求后端重启后恢复任务。
代码结构允许后续替换为持久化实现。
"""

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from inspect import signature

from app.jobs import store as job_store
from app.jobs.registry import validate_payload
from app.tasks.models import TaskCancelledError, TaskRecord

# 默认最大并发
_DEFAULT_MAX_WORKERS = 2


class TaskManager:
    """内存任务管理器"""

    def __init__(self, max_workers: int = _DEFAULT_MAX_WORKERS):
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        # 正在运行的任务 key: (task_type, source) -> task_id
        self._running: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._queue_active: dict[str, str] = {}
        self._queue_waiting: dict[str, list[str]] = {}
        self._queued_jobs: dict[str, tuple] = {}
        self._maintenance_reason = ""
        self._last_progress_persist_at: dict[str, float] = {}

    def _now(self) -> str:
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def _running_key(self, task_type: str, source: str) -> str:
        return f"{task_type}:{source}"

    def _create_task(
        self,
        task_type: str,
        source: str,
        message: str = "",
        *,
        reserve_running: bool = True,
        initial_result: dict | None = None,
    ) -> TaskRecord:
        """创建新任务（内部方法，由 submit 调用）

        检查并发 + 创建 task + 写入 running 在同一个 lock 里完成，避免竞态。
        如果同 task_type + source 已有运行中任务，抛出 ValueError。
        """
        rkey = self._running_key(task_type, source)
        with self._lock:
            if self._maintenance_reason:
                raise ValueError(f"KumiPlayer 正在{self._maintenance_reason}，暂时不能启动后台任务")
            if reserve_running and rkey in self._running:
                raise ValueError(f"同一来源已有 {task_type} 任务运行中: {self._running[rkey]}")

            task_id = f"task_{uuid.uuid4().hex[:12]}"
            now = self._now()
            record = TaskRecord(
                task_id=task_id,
                task_type=task_type,
                source=source,
                status="pending",
                progress=0,
                message=message,
                created_at=now,
                result=dict(initial_result or {}),
            )
            self._tasks[task_id] = record
            # 原子写入 running，防止竞态
            if reserve_running:
                self._running[rkey] = task_id
        self._persist_task(deepcopy(record))
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        """查询任务状态（durable job 优先，未命中走旧路径）"""
        durable = self._get_durable_record(task_id)
        if durable is not None:
            return durable
        with self._lock:
            task = self._tasks.get(task_id)
        if task:
            return task

        # 刷新页面或前端状态丢失时，允许从 SQLite 恢复最后一次持久化状态。
        # 这不恢复已经中断的线程，只用于展示日志、进度和完成结果。
        try:
            from app.db.database import init_db
            from app.db.tasks import get_task as get_db_task
            init_db()
            data = get_db_task(task_id)
            if data:
                return TaskRecord(**data)
        except Exception:
            pass
        return None

    def list_tasks(self, task_type: str | None = None, source: str | None = None) -> list:
        """列出任务（durable jobs + 内存任务 + 旧 SQLite 历史）"""
        durable = self._list_durable_records(task_type, source)
        with self._lock:
            tasks = list(self._tasks.values())
        seen = {item.task_id for item in durable}
        tasks = [item for item in tasks if item.task_id not in seen] + durable

        seen = {t.task_id for t in tasks}
        try:
            from app.db.database import init_db
            from app.db.tasks import list_tasks as list_db_tasks
            init_db()
            for data in list_db_tasks(task_type=task_type, source=source, limit=100):
                if data["task_id"] not in seen:
                    tasks.append(TaskRecord(**data))
                    seen.add(data["task_id"])
        except Exception:
            pass

        if task_type:
            tasks = [t for t in tasks if t.task_type == task_type]
        if source:
            tasks = [t for t in tasks if t.source == source]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def find_running_scrape_task(self, source: str | None = None) -> TaskRecord | None:
        """查找当前运行中的刮削任务。

        scrape_auto 和 scrape_select 都会访问 TMDB、写 NFO、写 scrape_map。
        为避免手动刮削与全面自动刮削互相抢资源，刮削任务按全局单通道处理。
        """
        # 只以内存中的当前线程为准。SQLite 里的 running 可能是上次异常退出
        # 遗留的状态，适合展示历史，不适合阻塞新任务。
        with self._lock:
            running_ids = {
                task_id
                for key, task_id in self._running.items()
                if key.startswith("scrape_")
            }
            tasks = [self._tasks[task_id] for task_id in running_ids if task_id in self._tasks]

        for task in sorted(tasks, key=lambda t: t.created_at, reverse=True):
            if not task.task_type.startswith("scrape_"):
                continue
            if source and task.source not in (source, "all") and source != "all":
                continue
            return task
        return None

    def cancel_running_scrape_tasks(self, source: str | None = None) -> int:
        """Request cooperative cancellation for in-memory scrape tasks.

        This is intentionally process-local. Persisted SQLite "running" rows can
        be stale after a browser refresh or backend restart, so they must not
        block a new full scrape.
        """
        cancelled = 0
        snapshots: list[TaskRecord] = []
        with self._lock:
            for task in list(self._tasks.values()):
                if not task.task_type.startswith("scrape_"):
                    continue
                if task.status not in ("pending", "running"):
                    continue
                if source and source != "all" and task.source not in (source, "all"):
                    continue
                self._cancelled.add(task.task_id)
                task.error = ""
                task.message = "正在停止"
                snapshots.append(deepcopy(task))
                cancelled += 1

        for snapshot in snapshots:
            self._persist_task(snapshot)
        return cancelled

    def cancel_running_tracking_tasks(self, source: str | None = None) -> int:
        """按来源请求停止追更扫描，防止清理后写回旧绑定。"""
        cancelled = 0
        snapshots: list[TaskRecord] = []
        with self._lock:
            for task in list(self._tasks.values()):
                if task.task_type not in {"tracking_scan", "tracking_scan_all"}:
                    continue
                if task.status not in ("pending", "running"):
                    continue
                if source and source != "all" and task.source not in {source, "all", "tracking"}:
                    continue
                self._cancelled.add(task.task_id)
                task.error = ""
                task.message = "正在停止"
                snapshots.append(deepcopy(task))
                cancelled += 1
        for snapshot in snapshots:
            self._persist_task(snapshot)
        return cancelled

    def has_running_tasks(self) -> bool:
        """是否存在当前进程内正在运行的后台任务。"""
        with self._lock:
            return any(
                task.status in ("pending", "running")
                for task in self._tasks.values()
            )

    @contextmanager
    def maintenance(self, reason: str):
        """阻止新任务启动，并确保维护开始时没有活动任务。"""
        with self._lock:
            if self._maintenance_reason:
                raise ValueError(f"KumiPlayer 正在{self._maintenance_reason}")
            if any(task.status in ("pending", "running") for task in self._tasks.values()):
                raise ValueError("存在正在运行的后台任务")
            self._maintenance_reason = reason
        try:
            yield
        finally:
            with self._lock:
                self._maintenance_reason = ""

    def _update_queue_positions_locked(self, queue_name: str) -> list[TaskRecord]:
        snapshots: list[TaskRecord] = []
        active_id = self._queue_active.get(queue_name)
        if active_id and active_id in self._tasks:
            active = self._tasks[active_id]
            active.result.update({"queue_name": queue_name, "queue_position": 0})
            snapshots.append(deepcopy(active))
        for position, task_id in enumerate(self._queue_waiting.get(queue_name, []), start=1):
            task = self._tasks.get(task_id)
            if task is None:
                continue
            task.result.update({"queue_name": queue_name, "queue_position": position})
            task.message = f"排队等待（前方 {position} 个任务）"
            snapshots.append(deepcopy(task))
        return snapshots

    def _start_next_queued(self, queue_name: str) -> None:
        with self._lock:
            if queue_name in self._queue_active:
                return
            waiting = self._queue_waiting.get(queue_name, [])
            if not waiting:
                self._queue_waiting.pop(queue_name, None)
                return
            task_id = waiting.pop(0)
            job = self._queued_jobs.get(task_id)
            if job is None:
                return
            record = self._tasks[task_id]
            self._queue_active[queue_name] = task_id
            self._running[self._running_key(record.task_type, record.source)] = task_id
            snapshots = self._update_queue_positions_locked(queue_name)

        for snapshot in snapshots:
            self._persist_task(snapshot)
        fn, args, kwargs = job
        self._launch_task(
            record,
            fn,
            args,
            kwargs,
            on_finished=lambda: self._finish_queued_task(queue_name, task_id),
        )

    def _finish_queued_task(self, queue_name: str, task_id: str) -> None:
        with self._lock:
            if self._queue_active.get(queue_name) == task_id:
                self._queue_active.pop(queue_name, None)
            self._queued_jobs.pop(task_id, None)
            snapshots = self._update_queue_positions_locked(queue_name)
        for snapshot in snapshots:
            self._persist_task(snapshot)
        self._start_next_queued(queue_name)

    def submit(
        self,
        task_type: str,
        source: str,
        fn: Callable[..., dict],
        *args,
        message: str = "",
        **kwargs,
    ) -> TaskRecord:
        """创建任务并提交到线程池执行（旧内存语义，保持既有调用兼容）。

        新链路使用 :meth:`submit_durable` 创建可恢复的持久任务。
        fn 是实际工作函数，返回 result dict；fn 抛异常则任务标记为 failed。
        """
        record = self._create_task(task_type, source, message)
        self._launch_task(record, fn, args, kwargs)
        return record

    def submit_durable(
        self,
        job_type: str,
        payload: dict,
        *,
        resource_key: str = "",
        message: str = "",
        parent_job_id: str = "",
        priority: int = 0,
        max_attempts: int = 3,
    ) -> TaskRecord:
        """创建可恢复的持久任务（payload 必须通过白名单校验）。

        任务由 JobRunner 领取执行；重启后未完成任务可继续。
        返回 TaskRecord（job_id 即 task_id）。
        """
        try:
            from app.db.database import init_db

            init_db()
            safe_payload = validate_payload(payload)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"durable payload 不合法: {exc}") from None
        job = job_store.create_job(
            job_type=job_type,
            resource_key=resource_key,
            payload=safe_payload,
            parent_job_id=parent_job_id,
            priority=priority,
            max_attempts=max_attempts,
        )
        return self._job_to_record(job, task_type=job_type, source=str(safe_payload.get("source") or ""), message=message)

    def _make_legacy_handler(self, fn: Callable, task_type: str):
        """包装旧 fn 为 durable handler（按签名注入 progress_callback/should_cancel）。"""
        from inspect import signature

        params = set(signature(fn).parameters)

        def handler(payload: dict, progress_callback=None, should_cancel=None) -> dict:
            args = payload.get("args") or []
            call_kwargs = dict(payload.get("kwargs") or {})
            if "progress_callback" in params:
                call_kwargs["progress_callback"] = progress_callback
            if "should_cancel" in params:
                call_kwargs["should_cancel"] = should_cancel
            return fn(*args, **call_kwargs) or {}

        return handler

    def _job_to_record(self, job, *, task_type: str, source: str, message: str = "") -> TaskRecord:
        return TaskRecord(
            task_id=job.job_id,
            task_type=task_type,
            source=source,
            # 前端与旧 TaskManager 使用 pending/running；jobs 表内部为 queued/running
            status="pending" if job.status == "queued" else job.status,
            progress=job.progress,
            message=message or job.message,
            created_at=job.created_at,
            started_at=job.started_at or None,
            finished_at=job.finished_at or None,
            error=job.error or None,
            result=job.result,
        )

    def submit_queued(
        self,
        task_type: str,
        source: str,
        fn: Callable[..., dict],
        *args,
        queue_name: str,
        message: str = "",
        initial_result: dict | None = None,
        **kwargs,
    ) -> TaskRecord:
        """把任务加入命名串行队列，前一个结束后自动启动下一个。"""
        record = self._create_task(
            task_type,
            source,
            message,
            reserve_running=False,
            initial_result=initial_result,
        )
        with self._lock:
            self._queued_jobs[record.task_id] = (fn, args, dict(kwargs))
            self._queue_waiting.setdefault(queue_name, []).append(record.task_id)
            snapshots = self._update_queue_positions_locked(queue_name)
        for snapshot in snapshots:
            self._persist_task(snapshot)
        self._start_next_queued(queue_name)
        return record

    def _launch_task(
        self,
        record: TaskRecord,
        fn: Callable[..., dict],
        args: tuple,
        kwargs: dict,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        task_id = record.task_id
        rkey = self._running_key(record.task_type, record.source)
        call_kwargs = dict(kwargs)

        def _mark_cancelled_locked(record: TaskRecord) -> None:
            record.status = "cancelled"
            record.error = ""
            record.message = "已停止"
            record.finished_at = self._now()

        def _worker():
            try:
                # 更新为 running
                with self._lock:
                    t = self._tasks[task_id]
                    if task_id in self._cancelled:
                        _mark_cancelled_locked(t)
                        cancelled_before_start = True
                    else:
                        t.status = "running"
                        t.started_at = self._now()
                        t.progress = 5
                        t.message = "开始执行"
                        cancelled_before_start = False
                    snapshot = deepcopy(t)
                self._persist_task(snapshot)
                if cancelled_before_start:
                    return

                def progress_callback(progress: int, message: str = "", result_patch: dict | None = None) -> None:
                    """供长任务汇报进度。

                    result_patch 会合并进 TaskRecord.result，前端可用来展示当前处理对象。
                    """
                    with self._lock:
                        if task_id in self._cancelled:
                            raise TaskCancelledError()
                        t = self._tasks[task_id]
                        t.progress = max(0, min(99, int(progress)))
                        if message:
                            t.message = message
                        if result_patch:
                            t.result.update(result_patch)
                        snapshot = self._progress_snapshot_if_due_locked(t)
                    if snapshot is not None:
                        self._persist_task(snapshot)

                def should_cancel() -> bool:
                    """供任务函数主动检查是否已被取消。"""
                    with self._lock:
                        return task_id in self._cancelled

                # 执行实际工作。函数声明了 progress_callback 或 should_cancel 时才注入。
                try:
                    params = signature(fn).parameters
                    if "progress_callback" in params and "progress_callback" not in call_kwargs:
                        call_kwargs["progress_callback"] = progress_callback
                    if "should_cancel" in params and "should_cancel" not in call_kwargs:
                        call_kwargs["should_cancel"] = should_cancel
                except (TypeError, ValueError):
                    pass
                result = fn(*args, **call_kwargs)

                # 更新为 succeeded
                with self._lock:
                    t = self._tasks[task_id]
                    if task_id in self._cancelled:
                        _mark_cancelled_locked(t)
                    else:
                        t.status = "succeeded"
                        t.message = "完成"
                        t.progress = 100
                        t.finished_at = self._now()
                    if task_id not in self._cancelled:
                        t.result.update(result or {})
                    self._last_progress_persist_at.pop(task_id, None)
                    snapshot = deepcopy(t)
                self._persist_task(snapshot)
            except Exception as e:
                # 更新为 failed
                with self._lock:
                    t = self._tasks[task_id]
                    if task_id in self._cancelled:
                        _mark_cancelled_locked(t)
                    else:
                        t.status = "failed"
                        t.progress = 100
                        t.error = str(e)
                        t.message = f"失败: {e}"
                    t.finished_at = self._now()
                    self._last_progress_persist_at.pop(task_id, None)
                    snapshot = deepcopy(t)
                self._persist_task(snapshot)
            finally:
                with self._lock:
                    self._running.pop(rkey, None)
                    self._cancelled.discard(task_id)
                if on_finished:
                    on_finished()

        self._executor.submit(_worker)

    def cancel_task(self, task_id: str) -> bool:
        """请求停止任务（durable job 优先，未命中走旧路径）。

        Python 线程无法安全强杀；这里采用协作式停止。外部请求返回后，
        任务会在下一次取消检查处退出，并保留最后一次准确进度。
        """
        if job_store.get_job(task_id) is not None:
            return job_store.cancel_job(task_id)
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return False
            if record.status not in ("pending", "running"):
                return False
            self._cancelled.add(task_id)
            record.error = ""
            record.message = "正在停止"
            snapshot = deepcopy(record)

        self._persist_task(snapshot)
        return True

    # ---- durable jobs 辅助（任务 2） ----

    def _get_durable_record(self, task_id: str) -> TaskRecord | None:
        try:
            from app.db.database import init_db

            init_db()
            job = job_store.get_job(task_id)
        except Exception:
            return None
        if job is None:
            return None
        return self._job_to_record(
            job,
            task_type=job.job_type.removeprefix("legacy:"),
            source=str(job.payload.get("source") or ""),
        )

    def _list_durable_records(self, task_type: str | None = None, source: str | None = None) -> list:
        try:
            from app.db.database import init_db

            init_db()
            jobs = job_store.list_jobs(limit=100)
        except Exception:
            return []
        records = []
        for job in jobs:
            job_type = job.job_type.removeprefix("legacy:")
            job_source = str(job.payload.get("source") or "")
            if task_type and job_type != task_type:
                continue
            if source and job_source != source:
                continue
            records.append(self._job_to_record(job, task_type=job_type, source=job_source))
        return records

    def remove_completed_tasks(self, task_ids: set[str]) -> int:
        """同步移除指定任务历史，绝不删除或隐藏运行中的任务。"""
        if not task_ids:
            return 0
        with self._lock:
            removable = {
                task_id
                for task_id in task_ids
                if task_id not in self._tasks
                or self._tasks[task_id].status not in ("pending", "running")
            }
            for task_id in removable:
                self._tasks.pop(task_id, None)
        from app.db.database import init_db
        from app.db.tasks import delete_tasks

        init_db()
        delete_tasks(removable)
        return len(removable)

    def shutdown(self):
        """关闭线程池"""
        self._executor.shutdown(wait=False)

    def _persist_task(self, record: TaskRecord) -> None:
        """任务状态双写 SQLite；失败不影响内存任务主流程。"""
        try:
            from dataclasses import asdict

            from app.db.database import close_connection, init_db
            from app.db.tasks import save_task
            init_db()
            save_task(asdict(record))
        except Exception:
            pass
        finally:
            try:
                from app.db.database import close_connection
                close_connection()
            except Exception:
                pass

    def _progress_snapshot_if_due_locked(self, record: TaskRecord) -> TaskRecord | None:
        """锁内决定是否持久化并复制快照，实际 SQLite 写入必须在锁外。"""
        now = time.monotonic()
        last = self._last_progress_persist_at.get(record.task_id, 0.0)
        if now - last < 0.75:
            return None
        self._last_progress_persist_at[record.task_id] = now
        return deepcopy(record)
