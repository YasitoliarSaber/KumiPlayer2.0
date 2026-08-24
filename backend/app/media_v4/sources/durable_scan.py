"""P-004：OpenList durable SourceScan 应用服务。

大库 OpenList 完整/增量扫描改为可离开、可恢复、可取消的持久扫描：
- 创建时先写入 source_scans(status=running) 并返回 scan/task 身份；
- 后台线程按同一 adapter（scan_openlist_directory / scan_openlist_incremental）
  扫描，成功时原子持久化 evidence 并置 completed + stage 检查点；
- 失败写明确终态；取消标记丢弃结果，不污染 confirmed revision。
SourceScan 只发生在 revision 之前，不进入 confirmed revision jobs；
confirmed revision 仍是唯一执行输入。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.core.paths import get_data_dir
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.sources.incremental import stage_scan_state

_cancel_flags: dict[str, threading.Event] = {}
_threads: dict[str, threading.Thread] = {}

SCAN_KINDS = {"full", "incremental"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _scan_root(database: V4Database, scan_id: str) -> str | None:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT root_id FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
    return str(row["root_id"]) if row else None


def create_durable_scan(
    database: V4Database,
    *,
    scan_id: str,
    root_id: str,
    kind: str,
    scan_fn: Callable[..., tuple],
    state_fn: Callable[[], dict] | None = None,
) -> str:
    """登记 durable scan 并启动后台线程执行；立即返回 scan_id。"""

    if kind not in SCAN_KINDS:
        raise ValueError(f"不支持的扫描类型: {kind}")
    now = _now()
    with database.connect() as conn:
        existing = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if existing is not None:
            if existing["status"] in {"running", "queued"}:
                raise ValueError("同一扫描任务已在执行中")
            conn.execute(
                "UPDATE source_scans SET status = 'running', started_at = ?, error = '' WHERE scan_id = ?",
                (now, scan_id),
            )
        else:
            conn.execute(
                "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
                "VALUES (?, ?, 1, 'running', ?)",
                (scan_id, root_id, now),
            )
    _cancel_flags[scan_id] = threading.Event()

    def _run() -> None:
        cancel = _cancel_flags[scan_id]
        try:
            raw = scan_fn()
            _scan_id, evidence = raw[0], raw[1]
            if cancel.is_set():
                raise _CancelledScan()
            # 证据必须挂在 durable scan_id 下，预览/恢复才能按 scan_id 读取。
            if _scan_id != scan_id:
                evidence = [replace(item, scan_id=scan_id) for item in evidence]
            if state_fn is not None:
                stage_scan_state(scan_id, state_fn())
            V4Repository(database).save_scan_evidence_bulk(evidence)
            if cancel.is_set():
                raise _CancelledScan()
            with database.connect() as conn:
                conn.execute(
                    "UPDATE source_scans SET status = 'completed', finished_at = ?, error = '' WHERE scan_id = ?",
                    (_now(), scan_id),
                )
        except _CancelledScan:
            with database.connect() as conn:
                conn.execute(
                    "UPDATE source_scans SET status = 'cancelled', finished_at = ?, error = '用户已取消扫描' WHERE scan_id = ?",
                    (_now(), scan_id),
                )
        except Exception as exc:  # noqa: BLE001 - 后台线程必须写明确终态
            with database.connect() as conn:
                conn.execute(
                    "UPDATE source_scans SET status = 'failed', finished_at = ?, error = ? WHERE scan_id = ?",
                    (_now(), str(exc)[:400], scan_id),
                )
        finally:
            _cancel_flags.pop(scan_id, None)
            _threads.pop(scan_id, None)

    thread = threading.Thread(target=_run, name=f"durable-scan-{scan_id}", daemon=True)
    _threads[scan_id] = thread
    thread.start()
    return scan_id


class _CancelledScan(Exception):
    pass


def get_durable_scan(database: V4Database, scan_id: str) -> dict:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT scan_id, root_id, status, started_at, finished_at, error "
            "FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(scan_id)
        status = str(row["status"])
        entries = []
        if status == "completed":
            entries = [
                _evidence_dict(item)
                for item in V4Repository(database).list_scan_evidence(scan_id)
            ]
    return {
        "scan_id": scan_id,
        "root_id": str(row["root_id"]),
        "status": status,
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "error": str(row["error"] or ""),
        "entries": entries,
    }


def cancel_durable_scan(scan_id: str) -> bool:
    """请求取消；后台线程在安全点丢弃结果。返回是否已登记。"""

    cancel = _cancel_flags.get(scan_id)
    if cancel is None:
        return False
    cancel.set()
    return True


def _evidence_dict(item) -> dict:
    from dataclasses import asdict

    return asdict(item)


def scans_data_root() -> str:
    return str(get_data_dir() / "openlist_incremental")
