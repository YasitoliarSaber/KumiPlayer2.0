# -*- coding: utf-8 -*-
"""删除预览和删除日志持久化"""

import json
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK

from app.library.delete import DeleteFile, DeletePreview, DeleteResult


def _get_delete_log_path() -> Path:
    from app.core.paths import get_data_dir
    lib_dir = get_data_dir() / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    return lib_dir / "delete_log.json"


def _get_delete_preview_dir() -> Path:
    from app.core.paths import get_data_dir
    preview_dir = get_data_dir() / "library" / "delete_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir


def save_delete_preview(preview: DeletePreview) -> str:
    """保存删除预览，供 confirm 按 preview_id 执行"""
    path = _get_delete_preview_dir() / f"{preview.preview_id}.json"
    write_json_atomic(path, asdict(preview))
    return str(path)


def load_delete_preview(preview_id: str) -> Optional[DeletePreview]:
    """加载删除预览"""
    path = _get_delete_preview_dir() / f"{preview_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = [DeleteFile(**item) for item in data.get("files", [])]
        return DeletePreview(
            preview_id=data.get("preview_id", ""),
            source=data.get("source", ""),
            scope=data.get("scope", ""),
            work_id=data.get("work_id", ""),
            files=files,
            empty_dirs=data.get("empty_dirs", []),
            warnings=data.get("warnings", []),
            blocked=data.get("blocked", False),
            retained_work_ids=data.get("retained_work_ids", []),
            library_work_count=int(data.get("library_work_count", 0) or 0),
            media_preset_count=int(data.get("media_preset_count", 0) or 0),
            tracking_binding_count=int(data.get("tracking_binding_count", 0) or 0),
            tracking_scan_run_count=int(data.get("tracking_scan_run_count", 0) or 0),
            history_count=int(data.get("history_count", 0) or 0),
            progress_count=int(data.get("progress_count", 0) or 0),
            related_reference_count=int(data.get("related_reference_count", 0) or 0),
        )
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def load_delete_log() -> List[dict]:
    """加载删除日志"""
    path = _get_delete_log_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return []


def save_delete_log(result: DeleteResult, source: str = "", scope: str = "") -> None:
    """追加一条删除日志"""
    with DATA_WRITE_LOCK:
        logs = load_delete_log()
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        entry = {
            "delete_id": f"del_{len(logs) + 1:04d}",
            "preview_id": result.preview_id,
            "requested_at": now,
            "source": source,
            "scope": scope,
            "status": result.status,
            "deleted": result.deleted,
            "failed": [{"path": f.path, "reason": f.reason} for f in result.failed],
            "skipped": [{"path": s.path, "reason": s.reason} for s in result.skipped],
            "empty_dirs_removed": result.empty_dirs_removed,
            "deleted_library_work_count": result.deleted_library_work_count,
            "deleted_preset_ids": result.deleted_preset_ids,
            "deleted_tracking_binding_count": result.deleted_tracking_binding_count,
            "deleted_tracking_scan_run_count": result.deleted_tracking_scan_run_count,
            "cancelled_tracking_task_count": result.cancelled_tracking_task_count,
        }
        logs.append(entry)
        write_json_atomic(_get_delete_log_path(), logs)
