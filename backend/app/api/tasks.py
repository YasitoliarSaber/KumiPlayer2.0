# -*- coding: utf-8 -*-
"""Unified background task API.

任务门面：legacy TaskManager（内存 + SQLite 历史）与 durable job（jobs 表）
双轨合并。durable job 统一以 ``Job.to_record_dict()``（TaskRecord 兼容形状）
暴露，source 取 payload 实际字段，不凭空猜测；jobs 表内部 queued 状态在
门面出口统一适配为前端契约的 pending（内部状态机不变）。legacy 任务保持原
TaskRecord 形状。查询按 legacy-first 语义：legacy 任务走原有路径，未命中才
回退 durable job。legacy id（``task_xxx``）与 durable id（32 位 uuid hex）
空间不重叠，任一 id 至多命中一条链路，durable 预检不会遮蔽 legacy 命中。
"""

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.jobs import store as job_store
from app.tasks.registry import get_task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _get_durable_job(task_id: str):
    """读取 durable job；DB 不可用按未命中处理（不遮蔽 legacy 查询）。"""
    try:
        from app.db.database import init_db

        init_db()
        return job_store.get_job(task_id)
    except Exception:
        return None


def _list_durable_jobs() -> list:
    """列出 durable jobs（created_at 倒序，与 legacy 展示一致）。"""
    try:
        from app.db.database import init_db

        init_db()
        return job_store.list_jobs(limit=100)
    except Exception:
        return []


def _record_to_dict(record):
    """legacy TaskRecord 或 durable to_record_dict() 统一转 JSON 形状。"""
    if isinstance(record, dict):
        data = dict(record)
    else:
        data = asdict(record)
    data["started_at"] = data.get("started_at") or ""
    data["finished_at"] = data.get("finished_at") or ""
    data["error"] = data.get("error") or ""
    return data


def _durable_record_dict(job):
    """durable Job → TaskRecord JSON 形状；内部 queued 适配为前端 pending。

    jobs 表内部状态机保留 queued（内部事实），只在任务门面出口做适配，
    与 TaskManager._job_to_record 的 queued→pending 语义一致（manager.py），
    保证 GET/列表/cancel 响应统一符合前端 TaskRecord 契约。
    """
    record = job.to_record_dict()
    if record.get("status") == "queued":
        record["status"] = "pending"
    return _record_to_dict(record)


def _record_task_type(record) -> str:
    return record["task_type"] if isinstance(record, dict) else record.task_type


@router.get("")
def list_tasks(
    source: Optional[str] = None,
    task_type: Optional[str] = None,
    type_prefix: Optional[str] = None,
    limit: int = 20,
):
    """List recent background tasks across legacy TaskManager and durable jobs."""
    manager = get_task_manager()
    durable_by_id = {job.job_id: job for job in _list_durable_jobs()}
    # manager.list_tasks 已合并 legacy（内存 + SQLite）与 durable（TaskRecord
    # 形状）；这里把 durable 项替换为 Job.to_record_dict() 形状，保持统一门面。
    # source/task_type 过滤由 manager 按两种链路分别应用，接口语义不变。
    records = [
        _durable_record_dict(durable_by_id[item.task_id])
        if item.task_id in durable_by_id
        else item
        for item in manager.list_tasks(task_type=task_type, source=source)
    ]
    if type_prefix:
        records = [
            record
            for record in records
            if _record_task_type(record).startswith(type_prefix)
        ]
    safe_limit = max(1, min(int(limit or 20), 100))
    return {"tasks": [_record_to_dict(record) for record in records[:safe_limit]]}


@router.get("/{task_id}")
def get_task(task_id: str):
    """查询任务：legacy TaskManager 优先，未命中回退 durable job。

    durable 预检仅为拿到 Job 原始形状（to_record_dict）；legacy id 与
    durable id 空间不重叠，此检查不会遮蔽 legacy 命中。
    """
    manager = get_task_manager()
    job = _get_durable_job(task_id)
    if job is not None:
        return _durable_record_dict(job)
    record = manager.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _record_to_dict(record)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    """请求取消任务：legacy 走原有协作取消；durable 走 job_store 请求取消。

    durable 终态（succeeded/failed/cancelled/waiting_review）不因 cancel
    请求被篡改：job_store.cancel_job 对非 queued/running 返回 False，
    无副作用，响应返回 reload 后的历史记录。
    """
    manager = get_task_manager()
    job = _get_durable_job(task_id)
    if job is not None:
        job_store.cancel_job(task_id)
        updated = _get_durable_job(task_id) or job
        return _durable_record_dict(updated)
    record = manager.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    manager.cancel_task(task_id)
    updated = manager.get_task(task_id) or record
    return _record_to_dict(updated)
