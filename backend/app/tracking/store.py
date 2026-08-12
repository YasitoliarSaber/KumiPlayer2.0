from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.database import close_connection, get_connection, init_db
from app.tracking.models import (
    ATTENTION_STATES,
    LOGICAL_SOURCES,
    TRACKING_STATES,
    TrackingBinding,
    tracking_attention_from_scrape_result,
)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def upsert_tracking_binding(binding: TrackingBinding) -> TrackingBinding:
    if not binding.work_id or not binding.root_path:
        raise ValueError("work_id 和 root_path 不能为空")
    if binding.tracking_state not in TRACKING_STATES:
        raise ValueError("未知追更状态")
    if binding.attention_state not in ATTENTION_STATES:
        raise ValueError("未知关注状态")
    if binding.logical_source not in LOGICAL_SOURCES:
        raise ValueError("追更来源只能是 local、pan115 或 baidu")
    existing = (
        get_tracking_binding_by_id(binding.binding_id)
        if binding.binding_id
        else get_tracking_binding_by_identity(
            binding.work_id,
            binding.root_path,
            binding.season_number,
        )
    )
    now = _now()
    if existing is not None and not binding.binding_id:
        # 自动登记只刷新来源与显示信息；用户状态和扫描基线必须保留。
        value = replace(
            binding,
            binding_id=existing.binding_id,
            tracking_state=existing.tracking_state,
            attention_state=existing.attention_state,
            last_snapshot_id=existing.last_snapshot_id,
            baseline_plan_id=existing.baseline_plan_id,
            last_scan_at=existing.last_scan_at,
            last_successful_scan_at=existing.last_successful_scan_at,
            last_result=existing.last_result,
            created_at=existing.created_at,
            updated_at=now,
        )
    else:
        value = replace(
            binding,
            binding_id=binding.binding_id or (existing.binding_id if existing else uuid4().hex),
            created_at=binding.created_at or (existing.created_at if existing else now),
            updated_at=now,
        )
    conn = get_connection()
    conn.execute("""
        INSERT INTO tracking_bindings (
            binding_id, work_id, display_title, logical_source, root_path, import_family,
            season_number, series_group, tracking_state, attention_state,
            last_snapshot_id, baseline_plan_id, last_scan_at,
            last_successful_scan_at, last_result, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(binding_id) DO UPDATE SET
            work_id=excluded.work_id, display_title=excluded.display_title,
            logical_source=excluded.logical_source, root_path=excluded.root_path,
            import_family=excluded.import_family, season_number=excluded.season_number,
            series_group=excluded.series_group, tracking_state=excluded.tracking_state,
            attention_state=excluded.attention_state,
            last_snapshot_id=excluded.last_snapshot_id,
            baseline_plan_id=excluded.baseline_plan_id, last_scan_at=excluded.last_scan_at,
            last_successful_scan_at=excluded.last_successful_scan_at,
            last_result=excluded.last_result, updated_at=excluded.updated_at
    """, _binding_values(value))
    conn.commit()
    close_connection()
    return value


def get_tracking_binding(work_id: str) -> TrackingBinding | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM tracking_bindings WHERE work_id = ?", (work_id,)).fetchone()
    close_connection()
    return _row_to_binding(row) if row else None


def get_tracking_binding_by_id(binding_id: str) -> TrackingBinding | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM tracking_bindings WHERE binding_id = ?", (binding_id,)).fetchone()
    close_connection()
    return _row_to_binding(row) if row else None


def get_tracking_binding_by_identity(
    work_id: str,
    root_path: str,
    season_number: int | None,
) -> TrackingBinding | None:
    """按数据库唯一键查找，供自动追更登记安全重试。"""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT * FROM tracking_bindings
        WHERE work_id = ? AND root_path = ? AND season_number IS ?
        """,
        (work_id, root_path, season_number),
    ).fetchone()
    close_connection()
    return _row_to_binding(row) if row else None


def get_tracking_binding_by_root(
    logical_source: str,
    root_path: str,
    season_number: int | None,
) -> TrackingBinding | None:
    """按实际追更目录定位绑定，避免目录树导入和手动添加产生重复绑定。"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM tracking_bindings
        WHERE logical_source = ? AND season_number IS ?
        ORDER BY updated_at DESC
        """,
        (logical_source, season_number),
    ).fetchall()
    close_connection()
    target = os.path.normcase(os.path.normpath(root_path))
    for row in rows:
        if os.path.normcase(os.path.normpath(str(row["root_path"] or ""))) == target:
            return _row_to_binding(row)
    return None


def delete_tracking_binding(binding_id: str) -> bool:
    """删除已迁移且不再可达的追更绑定，不触碰来源文件或镜像。"""
    if not binding_id:
        return False
    conn = get_connection()
    cursor = conn.execute("DELETE FROM tracking_bindings WHERE binding_id = ?", (binding_id,))
    conn.commit()
    close_connection()
    return cursor.rowcount > 0


def count_tracking_state_for_clear(logical_source: str | None = None) -> dict[str, int]:
    """统计媒体库清理将移除的追更控制记录。"""
    source = _normalize_clear_source(logical_source)
    init_db()
    conn = get_connection()
    try:
        if source is None:
            binding_count = conn.execute("SELECT COUNT(*) FROM tracking_bindings").fetchone()[0]
            scan_run_count = conn.execute("SELECT COUNT(*) FROM tracking_scan_runs").fetchone()[0]
        else:
            binding_count = conn.execute(
                "SELECT COUNT(*) FROM tracking_bindings WHERE logical_source = ?",
                (source,),
            ).fetchone()[0]
            scan_run_count = conn.execute(
                """
                SELECT COUNT(*) FROM tracking_scan_runs
                WHERE binding_id IN (
                    SELECT binding_id FROM tracking_bindings WHERE logical_source = ?
                )
                """,
                (source,),
            ).fetchone()[0]
        return {
            "binding_count": int(binding_count),
            "scan_run_count": int(scan_run_count),
        }
    finally:
        close_connection()


def delete_tracking_state_for_clear(logical_source: str | None = None) -> dict[str, int]:
    """原子删除指定来源的追更绑定及其扫描历史。"""
    source = _normalize_clear_source(logical_source)
    init_db()
    conn = get_connection()
    try:
        if source is None:
            scan_cursor = conn.execute("DELETE FROM tracking_scan_runs")
            binding_cursor = conn.execute("DELETE FROM tracking_bindings")
        else:
            scan_cursor = conn.execute(
                """
                DELETE FROM tracking_scan_runs
                WHERE binding_id IN (
                    SELECT binding_id FROM tracking_bindings WHERE logical_source = ?
                )
                """,
                (source,),
            )
            binding_cursor = conn.execute(
                "DELETE FROM tracking_bindings WHERE logical_source = ?",
                (source,),
            )
        conn.commit()
        return {
            "binding_count": max(0, int(binding_cursor.rowcount)),
            "scan_run_count": max(0, int(scan_cursor.rowcount)),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        close_connection()


def list_tracking_bindings(tracking_state: str | None = None) -> list[TrackingBinding]:
    conn = get_connection()
    if tracking_state:
        rows = conn.execute(
            "SELECT * FROM tracking_bindings WHERE tracking_state = ? ORDER BY updated_at DESC",
            (tracking_state,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tracking_bindings ORDER BY updated_at DESC").fetchall()
    close_connection()
    return [_row_to_binding(row) for row in rows]


def record_tracking_scan_run(binding: TrackingBinding, result: dict) -> dict:
    """Persist one completed scan without duplicating media structure in SQLite."""
    now = _now()
    value = {
        "scan_id": uuid4().hex,
        "binding_id": binding.binding_id,
        "work_id": binding.work_id,
        "status": str(result.get("status") or "failed"),
        "started_at": now,
        "finished_at": now,
        "result": result,
        "error": str(result.get("error") or ""),
    }
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO tracking_scan_runs (
            scan_id, binding_id, work_id, status, started_at, finished_at, result, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            value["scan_id"], value["binding_id"], value["work_id"], value["status"],
            value["started_at"], value["finished_at"],
            json.dumps(result, ensure_ascii=False), value["error"],
        ),
    )
    conn.commit()
    close_connection()
    return value


def save_tracking_scan_result(binding: TrackingBinding, result: dict) -> TrackingBinding | None:
    """原子保存扫描结果；绑定已被清理时绝不重新创建或留下孤立历史。"""
    if not binding.binding_id:
        return None
    if binding.tracking_state not in TRACKING_STATES:
        raise ValueError("未知追更状态")
    if binding.attention_state not in ATTENTION_STATES:
        raise ValueError("未知关注状态")
    if binding.logical_source not in LOGICAL_SOURCES:
        raise ValueError("追更来源只能是 local、pan115 或 baidu")

    value = replace(binding, updated_at=_now())
    scan = _new_scan_run(value, result)
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE tracking_bindings SET
                work_id=?, display_title=?, logical_source=?, root_path=?, import_family=?,
                season_number=?, series_group=?, tracking_state=?, attention_state=?,
                last_snapshot_id=?, baseline_plan_id=?, last_scan_at=?,
                last_successful_scan_at=?, last_result=?, updated_at=?
            WHERE binding_id=?
            """,
            (
                value.work_id, value.display_title, value.logical_source, value.root_path,
                value.import_family, value.season_number, value.series_group,
                value.tracking_state, value.attention_state, value.last_snapshot_id,
                value.baseline_plan_id, value.last_scan_at, value.last_successful_scan_at,
                json.dumps(value.last_result, ensure_ascii=False), value.updated_at,
                value.binding_id,
            ),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return None
        conn.execute(
            """
            INSERT INTO tracking_scan_runs (
                scan_id, binding_id, work_id, status, started_at, finished_at, result, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _scan_run_values(scan),
        )
        conn.commit()
        return value
    except Exception:
        conn.rollback()
        raise
    finally:
        close_connection()


def list_tracking_scan_runs(work_id: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_connection()
    safe_limit = max(1, min(int(limit), 500))
    if work_id:
        rows = conn.execute(
            "SELECT * FROM tracking_scan_runs WHERE work_id = ? ORDER BY started_at DESC LIMIT ?",
            (work_id, safe_limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tracking_scan_runs ORDER BY started_at DESC LIMIT ?", (safe_limit,)
        ).fetchall()
    close_connection()
    values = []
    for row in rows:
        value = dict(row)
        try:
            value["result"] = json.loads(value.get("result") or "{}")
        except json.JSONDecodeError:
            value["result"] = {}
        values.append(value)
    return values


def _binding_values(value: TrackingBinding) -> tuple:
    return (
        value.binding_id, value.work_id, value.display_title, value.logical_source, value.root_path,
        value.import_family, value.season_number, value.series_group,
        value.tracking_state, value.attention_state, value.last_snapshot_id,
        value.baseline_plan_id, value.last_scan_at, value.last_successful_scan_at,
        json.dumps(value.last_result, ensure_ascii=False), value.created_at, value.updated_at,
    )


def _normalize_clear_source(logical_source: str | None) -> str | None:
    value = (logical_source or "all").strip().lower()
    if value in {"", "all"}:
        return None
    # 清理路径必须容忍尚未支持追更的来源（如 openlist）：这些来源没有
    # 追更记录，按零处理；创建绑定仍在更严格的位置校验 LOGICAL_SOURCES。
    if value not in LOGICAL_SOURCES:
        return value
    return value


def _new_scan_run(binding: TrackingBinding, result: dict) -> dict:
    now = _now()
    return {
        "scan_id": uuid4().hex,
        "binding_id": binding.binding_id,
        "work_id": binding.work_id,
        "status": str(result.get("status") or "failed"),
        "started_at": now,
        "finished_at": now,
        "result": result,
        "error": str(result.get("error") or ""),
    }


def _scan_run_values(value: dict) -> tuple:
    return (
        value["scan_id"], value["binding_id"], value["work_id"], value["status"],
        value["started_at"], value["finished_at"],
        json.dumps(value["result"], ensure_ascii=False), value["error"],
    )


def _row_to_binding(row) -> TrackingBinding:
    data = dict(row)
    try:
        data["last_result"] = json.loads(data.get("last_result") or "{}")
    except json.JSONDecodeError:
        data["last_result"] = {}
    scrape_result = data["last_result"].get("scrape") or {}
    if (
        data.get("attention_state") == "waiting_metadata"
        and data["last_result"].get("status") == "succeeded"
        and tracking_attention_from_scrape_result(scrape_result) == "ready"
    ):
        # 兼容旧版本把 already_scraped 误判为元数据缺失的历史记录。
        data["attention_state"] = "ready"
    return TrackingBinding(**data)
