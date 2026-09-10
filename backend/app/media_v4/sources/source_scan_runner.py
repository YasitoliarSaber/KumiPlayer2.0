"""SourceScanRunner：以 SQLite 为执行真相的可恢复来源扫描执行器（C 段）。

职责边界：
- API 只登记任务（``register_source_scan``），不创建任何执行线程；
- worker（``SourceScanRunner``）原子领取 queued 扫描、刷新心跳、把进度
  以节流节奏写回 SQLite，并在安全点响应取消；
- 每轮消费前执行 stale recovery：旧心跳的 running/cancelling 行按
  取消/完整证据续跑/归档续跑/显式失败四个分支收口，绝不允许僵尸行
  永久占用；
- handler registry 按 ``scan_kind`` 分派到显式 handler；重建 adapter 所需
  的非敏感参数保存在 ``source_scan_requests.request_json``，Token/密码
  等凭据绝不入库。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.sources.durable_scan import (
    _CancelledScan,
    _scan_evidence_count,
    _update_scan_progress,
)
from app.media_v4.sources.scan_finalize import draft_finalizer
from app.media_v4.sources.scan_state import (
    SCAN_PROGRESS_MIN_INTERVAL_SECONDS,
    SCAN_PROGRESS_MIN_ITEMS,
    STALE_SCAN_AFTER_SECONDS,
    scan_is_stale,
)
from app.media_v4.sources.scanner import SourceScanCancelled

_LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ScanTask:
    scan_id: str
    root_id: str
    scan_kind: str
    source_mode: str
    request: dict
    attempts: int
    archive_path: str = ""
    archive_sha256: str = ""


@dataclass(frozen=True, slots=True)
class InputArchive:
    archive_path: str
    sha256: str
    original_filename: str


def register_source_scan(
    database: V4Database,
    *,
    scan_id: str,
    root_id: str,
    scan_kind: str,
    source_mode: str,
    request: dict | None = None,
    archive: InputArchive | None = None,
    conn=None,
) -> str:
    """登记 queued 扫描与可序列化请求；立即返回，不启动任何线程。

    同一 root 的代次在此处单调递增；重复 scan_id 视为编程错误。
    """

    now = _now()
    payload = json.dumps(request or {}, ensure_ascii=False)
    archive_path = archive.archive_path if archive else ""
    archive_sha = archive.sha256 if archive else ""
    original_name = archive.original_filename if archive else ""
    def write(connection) -> str:
        existing = connection.execute(
            "SELECT 1 FROM source_scans WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        if existing is not None:
            raise ValueError("扫描任务已存在: " + str(scan_id))
        generation = connection.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 FROM source_scans WHERE root_id = ?",
            (root_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at)
            VALUES (?, ?, ?, 'queued', 'queued', ?, ?)
            """,
            (scan_id, root_id, generation, now, now),
        )
        connection.execute(
            """
            INSERT INTO source_scan_requests(
                scan_id, scan_kind, source_mode, request_json,
                input_archive_path, input_sha256, original_filename,
                attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (scan_id, scan_kind, source_mode, payload, archive_path, archive_sha, original_name, now, now),
        )
        return scan_id

    if conn is not None:
        return write(conn)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = write(connection)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def _load_task(database: V4Database, scan_id: str) -> ScanTask | None:
    """读取扫描与请求两行；不使用 JOIN 别名，保持语句最简。"""

    with database.connect() as conn:
        scan_row = conn.execute(
            "SELECT root_id, status FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if scan_row is None:
            return None
        request_row = conn.execute(
            "SELECT scan_kind, source_mode, request_json, attempts, "
            "input_archive_path, input_sha256 FROM source_scan_requests WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
    try:
        request = json.loads(str(request_row["request_json"])) if request_row else {}
    except (TypeError, ValueError):
        request = {}
    return ScanTask(
        scan_id=scan_id,
        root_id=str(scan_row["root_id"]),
        scan_kind=str(request_row["scan_kind"] or "") if request_row else "",
        source_mode=str(request_row["source_mode"] or "") if request_row else "",
        request=request if isinstance(request, dict) else {},
        attempts=int(request_row["attempts"] or 0) if request_row else 0,
        archive_path=str(request_row["input_archive_path"] or "") if request_row else "",
        archive_sha256=str(request_row["input_sha256"] or "") if request_row else "",
    )


def _finish_scan(
    database: V4Database,
    scan_id: str,
    *,
    status: str,
    error: str = "",
) -> None:
    """以条件更新写终态；只有仍处于执行态的行会被收口。"""

    stamp = _now()
    cancel_flag = 1 if status == "cancelled" else 0
    with database.connect() as conn:
        conn.execute(
            """
            UPDATE source_scans
            SET status = ?, stage = ?, cancel_requested = ?,
                finished_at = ?, heartbeat_at = ?, error = ?
            WHERE scan_id = ? AND status IN ('queued', 'running', 'cancelling')
            """,
            (status, status, cancel_flag, stamp, stamp, error, scan_id),
        )


def _complete_scan(database: V4Database, scan_id: str) -> bool:
    """把已领取的扫描落成 completed 终态；processed 以参数绑定写入。

    只允许覆盖运行态：与取消接口的竞态不会把 cancelling/cancelled 改写成
    completed。返回值用于让调用方在取消刚好并发到达时补做终态收口。
    """

    stamp = _now()
    with database.connect() as conn:
        row = conn.execute(
            "SELECT total_count FROM source_scans WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        total = int(row["total_count"] or 0) if row else 0
        updated = conn.execute(
            """
            UPDATE source_scans
            SET status = 'completed', stage = 'ready', processed_count = ?,
                finished_at = ?, heartbeat_at = ?, cancel_requested = 0, error = ''
            WHERE scan_id = ? AND status = 'running'
            """,
            (total, stamp, stamp, scan_id),
        )
    return updated.rowcount == 1


def get_handler(scan_kind: str):
    """按 scan_kind 分派到显式 handler；未知类型返回 None。"""

    from app.media_v4.sources import scan_handlers

    return scan_handlers.HANDLERS.get(scan_kind)


class _ExecutionRuntime:
    """handler 的执行上下文：取消、节流进度与流式证据持久化。"""

    def __init__(self, database: V4Database, scan_id: str, cancel_event: threading.Event | None):
        self.database = database
        self.scan_id = scan_id
        self._cancel_event = cancel_event or threading.Event()
        self._last_progress_ts = 0.0
        self._items_since_write = 0

    def cancellation_requested(self) -> bool:
        """内存 Event 是本进程的快速路径，持久化标记是取消的权威。"""

        if self._cancel_event.is_set():
            return True
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM source_scans WHERE scan_id = ?",
                (self.scan_id,),
            ).fetchone()
        if row is not None and bool(row["cancel_requested"]):
            self._cancel_event.set()
        return self._cancel_event.is_set()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def report_progress(
        self,
        *,
        stage: str = "reading_source",
        processed_count: int = 0,
        total_count: int = 0,
    ) -> None:
        """阶段切换立即写；同阶段按 250ms/128 条节流，心跳始终兜底。"""

        if self.cancellation_requested():
            raise _CancelledScan()
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT stage, processed_count, total_count FROM source_scans WHERE scan_id = ?",
                (self.scan_id,),
            ).fetchone()
        current_stage = str(row["stage"] or "queued") if row else "queued"
        current_processed = int(row["processed_count"] or 0) if row else 0
        stage_changed = stage != current_stage
        self._items_since_write += max(0, int(processed_count) - current_processed)
        now_ts = time.monotonic()
        within_window = (now_ts - self._last_progress_ts) < SCAN_PROGRESS_MIN_INTERVAL_SECONDS
        enough_items = self._items_since_write >= SCAN_PROGRESS_MIN_ITEMS
        if not stage_changed and within_window and not enough_items:
            self._touch_heartbeat()
            return
        self._last_progress_ts = now_ts
        self._items_since_write = 0
        _update_scan_progress(
            self.database,
            self.scan_id,
            stage=stage,
            processed_count=processed_count,
            total_count=total_count,
        )

    def _touch_heartbeat(self) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE source_scans SET heartbeat_at = ? WHERE scan_id = ? AND status = 'running'",
                (_now(), self.scan_id),
            )

    def heartbeat(self) -> None:
        self._touch_heartbeat()

    def close(self) -> None:
        """安全边界收口标记；SQLite 连接为短生命周期，无需额外清理。"""

    def persist_evidence_batch(self, batch) -> None:
        """流式证据持久化：批次内去重交给 INSERT OR IGNORE。"""

        if self.cancellation_requested():
            raise _CancelledScan()
        if not batch:
            return
        if any(item.scan_id != self.scan_id for item in batch):
            raise ValueError("扫描适配器返回的证据未使用登记的 scan_id，拒绝写入")
        V4Repository(self.database).save_scan_evidence_bulk(batch)
        _update_scan_progress(
            self.database,
            self.scan_id,
            stage="reading_source",
            processed_count=_scan_evidence_count(self.database, self.scan_id),
        )


# ── SourceScanRunner（worker 循环，方法体在后续小节逐段实现）─────────────────


class SourceScanRunner:
    """轮询领取 queued 扫描并在工作线程内串行执行。"""

    def __init__(
        self,
        database: V4Database,
        *,
        poll_interval: float = 0.5,
        stale_after: int = STALE_SCAN_AFTER_SECONDS,
    ):
        self.database = database
        self.poll_interval = poll_interval
        self.stale_after = stale_after
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._active_scan_id: str | None = None

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="source-scan-runner",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                # 超时意味着进程即将退出但 handler 仍未返回。保留 running
                # 及其全部检查点，只把心跳推进到 stale 窗口之外，让下一
                # 个进程可以立即按既有恢复分支接管，而不伪造用户取消。
                self._mark_active_scan_interrupted()
        if thread is None or not thread.is_alive():
            self._thread = None

    def wake(self) -> None:
        """API 登记新任务后唤醒 worker；worker 未启动时惰性自启（幂等）。"""

        self.start()
        self._wake_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.recover_stale_scans()
                task = self.claim_next_scan()
                if task is not None:
                    self.run_scan(task)
                    continue
            except Exception:  # noqa: BLE001 - worker 绝不允许死亡
                _LOGGER.exception("source scan worker loop failed")
                self._wake_event.wait(timeout=max(self.poll_interval, 1.0))
            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()

    def _mark_active_scan_interrupted(self) -> None:
        """停机超时后让当前扫描可被下一进程立即识别为失联。"""

        scan_id = self._active_scan_id
        if not scan_id:
            return
        stale_stamp = (
            datetime.now(UTC) - timedelta(seconds=max(1, self.stale_after) + 1)
        ).isoformat()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE source_scans SET heartbeat_at = ? "
                "WHERE scan_id = ? AND status IN ('running', 'cancelling')",
                (stale_stamp, scan_id),
            )

    def claim_next_scan(self) -> ScanTask | None:
        """在写事务内 SELECT + 条件 UPDATE 原子领取；失败表示已有执行者。"""

        stamp = _now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT scan_id FROM source_scans
                    WHERE status = ?
                    ORDER BY started_at, scan_id
                    LIMIT 1
                    """,
                    ("queued",),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                scan_id = str(row["scan_id"])
                claimed = conn.execute(
                    """
                    UPDATE source_scans
                    SET status = ?, heartbeat_at = ?
                    WHERE scan_id = ? AND status = ?
                    """,
                    ("running", stamp, scan_id, "queued"),
                ).rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if claimed != 1:
            return None
        task = _load_task(self.database, scan_id)
        if task is None:
            _finish_scan(self.database, scan_id, status="failed", error="扫描请求记录缺失")
            return None
        return task

    def run_scan(self, task: ScanTask) -> None:
        """执行一个已领取的扫描；终态写入与异常收口都集中在这里。"""

        # runner.stop() 只表示应用生命周期结束，不是用户取消。真正的取消
        # 信号来自 source_scans.cancel_requested，避免正常关窗伪造 cancelled。
        self._active_scan_id = task.scan_id
        runtime = _ExecutionRuntime(self.database, task.scan_id, None)
        try:
            _update_scan_progress(self.database, task.scan_id, stage="reading_source")
            handler = get_handler(task.scan_kind)
            if handler is None:
                raise ValueError("不支持的扫描类型: " + str(task.scan_kind))
            returned = handler(self.database, task, runtime)
            if runtime.cancellation_requested():
                raise _CancelledScan()
            evidence = self._settled_evidence(task, returned)
            self._settle_total(task.scan_id, len(evidence))
            if runtime.cancellation_requested():
                raise _CancelledScan()
            self._run_finalizer(task, runtime, evidence)
            if not _complete_scan(self.database, task.scan_id):
                _finish_scan(self.database, task.scan_id, status="cancelled", error="用户已取消扫描")
        except (SourceScanCancelled, _CancelledScan):
            _finish_scan(self.database, task.scan_id, status="cancelled", error="用户已取消扫描")
        except Exception as exc:  # noqa: BLE001 - 执行器必须写明确终态
            _finish_scan(self.database, task.scan_id, status="failed", error=str(exc)[:400])
        finally:
            runtime.close()
            if self._active_scan_id == task.scan_id:
                self._active_scan_id = None

    def _settled_evidence(self, task: ScanTask, returned) -> list:
        """扫描阶段结束后的证据全集：优先读数据库，必要时兜底保存。

        流式 adapter 可能已经落库部分证据；这里用返回的完整结果补齐未落库项。
        """

        repository = V4Repository(self.database)
        if returned:
            if any(item.scan_id != task.scan_id for item in returned):
                raise ValueError("扫描适配器返回的证据未使用登记的 scan_id，拒绝写入")
            repository.save_scan_evidence_bulk(returned)
        return repository.list_scan_evidence(task.scan_id)

    def _settle_total(self, scan_id: str, count: int) -> None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT total_count FROM source_scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        total = int(row["total_count"] or 0) if row else 0
        if count > total:
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE source_scans SET total_count = ? WHERE scan_id = ?",
                    (count, scan_id),
                )

    def _run_finalizer(self, task: ScanTask, runtime: _ExecutionRuntime, evidence: list) -> None:
        finalize = draft_finalizer(
            self.database,
            revision_id=str(task.request.get("revision_id") or ""),
            source_display_name=str(task.request.get("source_display_name") or ""),
            root_id=task.root_id,
            scan_id=task.scan_id,
        )
        if finalize is None:
            return
        finalize(
            evidence,
            should_cancel=runtime.cancellation_requested,
            on_progress=runtime.report_progress,
        )

    def recover_stale_scans(self, *, max_age_seconds: int | None = None) -> int:
        """按四个分支收口/恢复失联扫描；返回处理的行数。

        - stale + cancel_requested（或 cancelling）→ cancelled；
        - stale + 证据完整（evidence_count == total_count > 0，阶段已过读取）
          → 仅用数据库证据离线续跑 finalizer；
        - stale + 归档存在且哈希一致 → 同一 scan_id 重新排队，attempts+1；
        - 其余 stale → 明确 failed（上次扫描意外中断）。
        新鲜心跳（本进程在跑或其他执行者存活）一律不动。
        """

        threshold = max_age_seconds or self.stale_after
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT scan_id, status, stage, total_count, heartbeat_at, cancel_requested
                FROM source_scans
                WHERE status IN (?, ?, ?)
                """,
                ("queued", "running", "cancelling"),
            ).fetchall()
        handled = 0
        for row in rows:
            scan_id = str(row["scan_id"])
            status = str(row["status"])
            if not scan_is_stale(row["heartbeat_at"], max_age_seconds=threshold):
                continue
            if status == "queued":
                # queued 行只会被 claim；只有取消语义需要在这里收口。
                if bool(row["cancel_requested"]):
                    _finish_scan(self.database, scan_id, status="cancelled", error="用户已取消扫描")
                    handled += 1
                continue
            cancel_requested = bool(row["cancel_requested"]) or status == "cancelling"
            if cancel_requested:
                _finish_scan(self.database, scan_id, status="cancelled", error="用户已取消扫描")
                handled += 1
                continue
            total_count = int(row["total_count"] or 0)
            evidence_count = _scan_evidence_count(self.database, scan_id)
            stage = str(row["stage"] or "")
            evidence_complete = (
                total_count > 0
                and evidence_count >= total_count
                and stage in {"parsing", "normalizing", "preparing_preview", "recognizing"}
            )
            if self._resume_from_evidence(scan_id, evidence_complete):
                handled += 1
                continue
            if self._requeue_from_archive(scan_id):
                handled += 1
                continue
            _finish_scan(
                self.database,
                scan_id,
                status="failed",
                error="上次扫描意外中断，请重新扫描",
            )
            handled += 1
        return handled

    def _resume_from_evidence(self, scan_id: str, evidence_complete: bool) -> bool:
        """分支 B：证据完整时只用数据库证据离线续跑 finalizer。"""

        if not evidence_complete:
            return False
        task = _load_task(self.database, scan_id)
        if task is None:
            return False
        self._bump_attempts(scan_id)
        stamp = _now()
        with self.database.connect() as conn:
            conn.execute(
                """
                UPDATE source_scans SET status = ?, heartbeat_at = ?
                WHERE scan_id = ? AND status IN (?, ?)
                """,
                ("running", stamp, scan_id, "running", "cancelling"),
            )
        self._active_scan_id = scan_id
        runtime = _ExecutionRuntime(self.database, scan_id, None)
        try:
            evidence = V4Repository(self.database).list_scan_evidence(scan_id)
            self._run_finalizer(task, runtime, evidence)
            if not _complete_scan(self.database, scan_id):
                _finish_scan(self.database, scan_id, status="cancelled", error="用户已取消扫描")
            return True
        except (SourceScanCancelled, _CancelledScan):
            _finish_scan(self.database, scan_id, status="cancelled", error="用户已取消扫描")
            return True
        except Exception as exc:  # noqa: BLE001 - 恢复失败也要落到明确终态
            _finish_scan(self.database, scan_id, status="failed", error=str(exc)[:400])
            return True
        finally:
            runtime.close()
            if self._active_scan_id == scan_id:
                self._active_scan_id = None

    def _requeue_from_archive(self, scan_id: str) -> bool:
        """分支 C：归档存在且哈希一致时，把同一 scan_id 重新排队。"""

        task = _load_task(self.database, scan_id)
        if task is None:
            return False
        from app.media_v4.sources.input_archive import archive_is_intact

        if not archive_is_intact(task.archive_path, task.archive_sha256):
            return False
        self._bump_attempts(scan_id)
        stamp = _now()
        with self.database.connect() as conn:
            conn.execute(
                """
                UPDATE source_scans
                SET status = ?, stage = ?, heartbeat_at = ?, cancel_requested = 0, error = ''
                WHERE scan_id = ? AND status IN (?, ?)
                """,
                ("queued", "queued", stamp, scan_id, "running", "cancelling"),
            )
        return True

    def _bump_attempts(self, scan_id: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE source_scan_requests SET attempts = attempts + 1, updated_at = ? WHERE scan_id = ?",
                (_now(), scan_id),
            )


_runner: SourceScanRunner | None = None
_runner_lock = threading.Lock()


def get_source_scan_runner(database: V4Database) -> SourceScanRunner:
    """进程级单例：lifespan 与 API 共用同一个 worker，任务注册后唤醒它。

    数据库句柄变化（测试隔离或运行时重置）时重建 worker，避免旧 worker
    轮询已废弃的数据库。
    """

    global _runner
    with _runner_lock:
        if _runner is not None and _runner.database is not database:
            _runner.stop()
            _runner = None
        if _runner is None:
            _runner = SourceScanRunner(database)
        return _runner


def reset_source_scan_runner() -> None:
    """测试隔离：丢弃进程级单例。"""

    global _runner
    with _runner_lock:
        if _runner is not None:
            _runner.stop()
        _runner = None
