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

import inspect
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.core.paths import get_data_dir
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.sources.incremental import stage_scan_state
from app.media_v4.sources.scanner import SourceScanCancelled

_cancel_flags: dict[str, threading.Event] = {}
_threads: dict[str, threading.Thread] = {}

SCAN_KINDS = {"full", "incremental"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _accepts_should_cancel(fn: Callable) -> bool:
    return _accepts_parameter(fn, "should_cancel")


def _accepts_parameter(fn: Callable, name: str) -> bool:
    try:
        parameters = inspect.signature(fn).parameters
        return name in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    except (TypeError, ValueError):
        return False


_STAGE_ORDER = {
    "queued": 0,
    "reading_source": 1,
    # recognizing 是 v14 及更早扫描行的兼容读数；新任务使用更细的
    # parsing/normalizing 阶段，但旧任务不能因为新增列而倒退或改名。
    "recognizing": 2,
    "parsing": 3,
    "normalizing": 4,
    "preparing_preview": 5,
    "ready": 6,
    "failed": 6,
    "cancelled": 6,
}

_STAGE_LABELS = {
    "queued": "准备读取媒体来源",
    "reading_source": "读取媒体来源",
    "recognizing": "离线识别与整理",
    "parsing": "解析媒体条目",
    "normalizing": "整理识别结果",
    "preparing_preview": "生成识别预览",
    "ready": "识别结果已就绪",
    "paused": "本次巡检已达请求预算，可继续扫描",
    "failed": "扫描失败",
    "cancelled": "扫描已取消",
}


def _scan_row(database: V4Database, scan_id: str):
    with database.connect() as conn:
        return conn.execute(
            "SELECT stage, processed_count, total_count, cancel_requested "
            "FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()


def _persisted_cancel_requested(database: V4Database, scan_id: str) -> bool:
    row = _scan_row(database, scan_id)
    return bool(row and (int(row["cancel_requested"] or 0) or row["stage"] == "cancelled"))


def _update_scan_progress(
    database: V4Database,
    scan_id: str,
    *,
    stage: str | None = None,
    processed_count: int | None = None,
    total_count: int | None = None,
) -> None:
    """按阶段语义写入扫描进度；数据库是刷新/离开页面后的唯一事实。

    计数规则（11.19.5）：阶段前进时重置为新阶段传入值（未知总量用 0，
    不能沿用上一阶段 total）；相同阶段才使用 max 保持单调；旧阶段回调
    整条忽略，不只保留 stage 却采纳旧计数；终态（ready/failed/cancelled）
    忽略运行阶段回调。
    """

    if stage is not None and stage not in _STAGE_ORDER:
        raise ValueError(f"不支持的扫描阶段: {stage}")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT stage, processed_count, total_count, status FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if row is None:
            return
        current_stage = str(row["stage"] or "queued")
        current_processed = int(row["processed_count"] or 0)
        current_total = int(row["total_count"] or 0)
        if current_stage in {"ready", "failed", "cancelled"}:
            return
        incoming_stage = stage or current_stage
        if _STAGE_ORDER.get(incoming_stage, 0) < _STAGE_ORDER.get(current_stage, 0):
            return
        if incoming_stage != current_stage:
            next_processed = max(0, int(processed_count or 0))
            next_total = max(next_processed, int(total_count or 0))
        else:
            next_processed = max(current_processed, int(processed_count or 0))
            next_total = max(current_total, int(total_count or 0), next_processed)
        conn.execute(
            "UPDATE source_scans SET stage = ?, processed_count = ?, total_count = ?, "
            "heartbeat_at = ? WHERE scan_id = ?",
            (incoming_stage, next_processed, next_total, _now(), scan_id),
        )


def _scan_evidence_count(database: V4Database, scan_id: str) -> int:
    with database.connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM source_evidence WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()[0]
        )


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
    finalize_fn: Callable[..., None] | None = None,
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
                "UPDATE source_scans SET status = 'running', stage = 'queued', "
                "processed_count = 0, total_count = 0, heartbeat_at = ?, "
                "cancel_requested = 0, started_at = ?, finished_at = '', error = '' WHERE scan_id = ?",
                (now, now, scan_id),
            )
        else:
            generation = conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM source_scans WHERE root_id = ?",
                (root_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
                "VALUES (?, ?, ?, 'running', 'queued', ?, ?)",
                (scan_id, root_id, generation, now, now),
            )
    _cancel_flags[scan_id] = threading.Event()
    # 新适配器显式接收 durable scan_id；只有未升级的外部回调保留兼容归一化。
    enforce_scan_identity = _accepts_parameter(scan_fn, "scan_id")

    def _run() -> None:
        cancel = _cancel_flags[scan_id]

        def cancellation_requested() -> bool:
            # Event 是当前进程的快速路径；取消接口还会写入数据库，批次
            # 回调会重新读取持久化标记，避免把取消合同建立在内存状态上。
            return cancel.is_set()

        def report_progress(*, stage: str = "reading_source", processed_count: int = 0, total_count: int = 0) -> None:
            if cancel.is_set() or _persisted_cancel_requested(database, scan_id):
                cancel.set()
                raise _CancelledScan()
            _update_scan_progress(
                database,
                scan_id,
                stage=stage,
                processed_count=processed_count,
                total_count=total_count,
            )

        def persist_evidence_batch(batch) -> None:
            if cancel.is_set() or _persisted_cancel_requested(database, scan_id):
                cancel.set()
                raise _CancelledScan()
            if not batch:
                return
            normalized = batch
            if any(item.scan_id != scan_id for item in batch):
                if enforce_scan_identity:
                    raise ValueError("扫描适配器返回的证据未使用登记的 scan_id，拒绝写入")
                normalized = [replace(item, scan_id=scan_id) for item in batch]
            V4Repository(database).save_scan_evidence_bulk(normalized)
            _update_scan_progress(
                database,
                scan_id,
                stage="reading_source",
                processed_count=_scan_evidence_count(database, scan_id),
            )

        try:
            _update_scan_progress(database, scan_id, stage="reading_source")
            scan_kwargs = {}
            if _accepts_parameter(scan_fn, "should_cancel"):
                scan_kwargs["should_cancel"] = cancellation_requested
            if _accepts_parameter(scan_fn, "on_evidence_batch"):
                scan_kwargs["on_evidence_batch"] = persist_evidence_batch
            if _accepts_parameter(scan_fn, "on_progress"):
                scan_kwargs["on_progress"] = report_progress
            if _accepts_parameter(scan_fn, "scan_id"):
                scan_kwargs["scan_id"] = scan_id
            raw = scan_fn(**scan_kwargs) if scan_kwargs else scan_fn()
            _scan_id, evidence = raw[0], raw[1]
            if cancellation_requested() or _persisted_cancel_requested(database, scan_id):
                raise _CancelledScan()
            # 证据必须挂在 durable scan_id 下，预览/恢复才能按 scan_id 读取。
            if enforce_scan_identity and (
                _scan_id != scan_id or any(item.scan_id != scan_id for item in evidence)
            ):
                raise ValueError("扫描适配器返回的证据未使用登记的 scan_id，拒绝写入")
            if _scan_id != scan_id:
                evidence = [replace(item, scan_id=scan_id) for item in evidence]
            if state_fn is not None:
                stage_scan_state(scan_id, state_fn())
            # 流式 adapter 只会提前写入已观察到的子集；结算时仍须补齐
            # 返回的完整结果，才能保留未抽查的基线证据。
            persist_evidence_batch(evidence)
            evidence = V4Repository(database).list_scan_evidence(scan_id)
            report_progress(
                stage="parsing" if finalize_fn is not None else "preparing_preview",
                processed_count=0 if finalize_fn is not None else len(evidence),
                total_count=len(evidence),
            )
            if cancellation_requested() or _persisted_cancel_requested(database, scan_id):
                raise _CancelledScan()
            # 识别与草稿持久化属于来源任务，但必须保持纯离线；在线作品匹配
            # 只能由确认后的 metadata job 执行。取消合同 B：取消只保留
            # 不可变审计事实；finalizer 在 create_draft 前有最后一道取消
            # 检查，草稿一旦开始创建就让扫描自然完成，避免出现
            # "cancelled 状态却挂着 draft" 的矛盾态。
            if finalize_fn is not None:
                finalize_kwargs = {}
                if _accepts_parameter(finalize_fn, "should_cancel"):
                    finalize_kwargs["should_cancel"] = cancellation_requested
                if _accepts_parameter(finalize_fn, "on_progress"):
                    finalize_kwargs["on_progress"] = report_progress
                finalize_fn(evidence, **finalize_kwargs)
            with database.connect() as conn:
                conn.execute(
                    "UPDATE source_scans SET status = 'completed', stage = 'ready', "
                    "processed_count = total_count, finished_at = ?, heartbeat_at = ?, "
                    "cancel_requested = 0, error = '' WHERE scan_id = ?",
                    (_now(), _now(), scan_id),
                )
        except (SourceScanCancelled, _CancelledScan):
            with database.connect() as conn:
                conn.execute(
                    "UPDATE source_scans SET status = 'cancelled', stage = 'cancelled', "
                    "cancel_requested = 1, finished_at = ?, heartbeat_at = ?, "
                    "error = '用户已取消扫描' WHERE scan_id = ?",
                    (_now(), _now(), scan_id),
                )
        except Exception as exc:  # noqa: BLE001 - 后台线程必须写明确终态
            with database.connect() as conn:
                conn.execute(
                    "UPDATE source_scans SET status = 'failed', stage = 'failed', "
                    "finished_at = ?, heartbeat_at = ?, error = ? WHERE scan_id = ?",
                    (_now(), _now(), str(exc)[:400], scan_id),
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


def get_durable_scan(
    database: V4Database,
    scan_id: str,
    *,
    include_entries: bool = True,
) -> dict:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT scan_id, root_id, status, stage, processed_count, total_count, "
            "heartbeat_at, cancel_requested, started_at, finished_at, error "
            "FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(scan_id)
        status = str(row["status"])
        entries = []
        evidence_count = int(conn.execute(
            "SELECT COUNT(*) FROM source_evidence WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()[0])
        parsed_count = int(conn.execute(
            """
            SELECT COUNT(*)
            FROM parsed_facts pf
            JOIN source_evidence se ON se.evidence_id = pf.evidence_id
            WHERE se.scan_id = ?
            """,
            (scan_id,),
        ).fetchone()[0])
        if status == "completed" and include_entries:
            evidence_rows = conn.execute(
                "SELECT * FROM source_evidence WHERE scan_id = ? ORDER BY source_key",
                (scan_id,),
            ).fetchall()
            entries = [
                _evidence_dict(V4Repository._row_to_source_evidence(item))
                for item in evidence_rows
            ]

    stored_stage = str(row["stage"] or "queued")
    stored_processed = int(row["processed_count"] or 0)
    stored_total = int(row["total_count"] or 0)
    if status == "completed":
        stage = "ready"
        processed_count = evidence_count
        total_count = evidence_count
    elif status == "failed":
        stage = "failed"
        processed_count = max(stored_processed, parsed_count)
        total_count = max(stored_total, evidence_count)
    elif status == "cancelled":
        stage = "cancelled"
        processed_count = max(stored_processed, parsed_count)
        total_count = max(stored_total, evidence_count)
    elif status == "paused":
        # 达到请求预算：进度保留，可继续扫描；不得当作完整成功。
        stage = "paused"
        processed_count = max(stored_processed, parsed_count)
        total_count = max(stored_total, evidence_count)
    elif stored_stage not in {"", "queued"}:
        stage = stored_stage
        processed_count = stored_processed
        total_count = stored_total
    elif evidence_count == 0:
        stage, processed_count, total_count = "reading_source", 0, 0
    elif parsed_count < evidence_count:
        # 只有没有 v15 阶段事实的旧扫描才会走这里。保留旧 API 的
        # recognizing 语义，避免刷新旧任务时把“已有证据、尚未解析”误报
        # 成新任务的 parsing 阶段。
        stage, processed_count, total_count = "recognizing", parsed_count, evidence_count
    else:
        stage, processed_count, total_count = "preparing_preview", evidence_count, evidence_count
    stage_label = _STAGE_LABELS.get(stage, "处理中")
    if stage == "reading_source" and total_count == 0 and processed_count > 0:
        # 全量/增量读取阶段无法预知总量（预先递归统计会让请求量翻倍）。给出实时
        # 计数，让用户看到扫描仍在推进，而不是一个不动的百分比。
        stage_label = f"正在读取媒体来源（已发现 {processed_count} 个媒体文件）"
    if status == "cancelling":
        stage_label = "正在取消扫描"
    from app.media_v4.sources.scan_state import INTERRUPTED_STAGE_LABEL, scan_is_stale

    heartbeat_text = str(row["heartbeat_at"] or "")
    # 空心跳是 v15 旧行的历史产物，不据此判中断；有旧心跳才说明执行者失联。
    interrupted = status in {"running", "cancelling"} and bool(heartbeat_text) and scan_is_stale(
        heartbeat_text
    )
    if interrupted:
        stage_label = INTERRUPTED_STAGE_LABEL
    progress = None
    if total_count > 0:
        progress = min(1.0, max(0.0, processed_count / total_count))
    return {
        "scan_id": scan_id,
        "root_id": str(row["root_id"]),
        "status": status,
        "interrupted": interrupted,
        # 只有 paused 的扫描可以带着 frontier 断点继续；它是"部分进度"，不是成功。
        "resumable": status == "paused",
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "heartbeat_at": str(row["heartbeat_at"] or ""),
        "cancel_requested": bool(row["cancel_requested"] or 0),
        "error": str(row["error"] or ""),
        "evidence_count": evidence_count,
        "parsed_count": parsed_count,
        "stage": stage,
        "stage_label": stage_label,
        "processed_count": processed_count,
        "total_count": total_count,
        # 读取阶段的“已发现媒体文件数”，前端在总量未知时用它替代假百分比。
        "discovered_count": processed_count,
        "progress": progress,
        "entries": entries,
    }


def cancel_durable_scan(scan_id: str, *, database: V4Database | None = None) -> bool:
    """请求取消；后台线程在安全点丢弃结果。返回是否已登记。

    queued 从未被领取，直接收口为 cancelled；running/cancelling 若心跳
    已失联（进程退出的僵尸行），同样立即收口——失联线程不可能再执行
    finally，继续等只会从「永久运行中」变成「永久取消中」。新鲜心跳的
    running 才写 cancel_requested 交给执行者在安全点收口。
    """

    cancel = _cancel_flags.get(scan_id)
    if cancel is not None:
        cancel.set()
    if database is not None:
        with database.connect() as conn:
            row = conn.execute(
                "SELECT status, heartbeat_at, started_at FROM source_scans WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()
            if row is None or row["status"] not in {"running", "queued", "cancelling"}:
                return cancel is not None
            from app.media_v4.sources.scan_state import scan_is_stale

            now_stamp = _now()
            if row["status"] == "queued" or scan_is_stale(row["heartbeat_at"] or row["started_at"]):
                conn.execute(
                    "UPDATE source_scans SET status = 'cancelled', stage = 'cancelled', "
                    "cancel_requested = 1, finished_at = ?, heartbeat_at = ?, "
                    "error = '用户已取消扫描' WHERE scan_id = ?",
                    (now_stamp, now_stamp, scan_id),
                )
            else:
                conn.execute(
                    "UPDATE source_scans SET status = 'cancelling', cancel_requested = 1, "
                    "heartbeat_at = ? WHERE scan_id = ?",
                    (now_stamp, scan_id),
                )
            return True
    return cancel is not None


def _evidence_dict(item) -> dict:
    from dataclasses import asdict

    return asdict(item)


def scans_data_root() -> str:
    return str(get_data_dir() / "openlist_incremental")
