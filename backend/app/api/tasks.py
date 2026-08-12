# -*- coding: utf-8 -*-
"""Unified background task API."""

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.tasks.registry import get_task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_tasks(
    source: Optional[str] = None,
    task_type: Optional[str] = None,
    type_prefix: Optional[str] = None,
    limit: int = 20,
):
    """List recent background tasks across mirror, scrape, library, and sources."""
    manager = get_task_manager()
    tasks = manager.list_tasks(task_type=task_type, source=source)
    if type_prefix:
        tasks = [task for task in tasks if task.task_type.startswith(type_prefix)]
    safe_limit = max(1, min(int(limit or 20), 100))
    return {"tasks": [_record_to_dict(task) for task in tasks[:safe_limit]]}


@router.get("/{task_id}")
def get_task(task_id: str):
    manager = get_task_manager()
    record = manager.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _record_to_dict(record)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    manager = get_task_manager()
    record = manager.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    manager.cancel_task(task_id)
    updated = manager.get_task(task_id) or record
    return _record_to_dict(updated)


def _record_to_dict(record):
    data = asdict(record)
    data["started_at"] = data.get("started_at") or ""
    data["finished_at"] = data.get("finished_at") or ""
    data["error"] = data.get("error") or ""
    return data
