# -*- coding: utf-8 -*-
"""镜像生成 API

POST /api/mirror/{source}/generate  -> task_id
GET  /api/mirror/tasks/{task_id}    -> 任务状态
"""

from inspect import signature
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.import_plan.store import load_import_plan, load_latest_confirmed_import_plan
from app.mirror.generator import generate_mirror
from app.tasks.logs import append_task_log
from app.tasks.registry import get_task_manager

router = APIRouter(prefix="/api/mirror", tags=["mirror"])


# ============================================================
# 请求模型
# ============================================================

class GenerateRequest(BaseModel):
    """镜像生成请求"""
    plan_id: Optional[str] = None


# ============================================================
# 端点
# ============================================================

@router.post("/{source}/generate")
def generate_mirror_task(source: str, req: GenerateRequest = GenerateRequest()):
    """创建镜像生成异步任务

    返回 task_id，前端轮询 GET /api/mirror/tasks/{task_id} 查询进度。
    """
    # 加载 plan
    if req.plan_id:
        plan = load_import_plan(plan_id=req.plan_id)
    else:
        plan = load_latest_confirmed_import_plan(source)
    if plan is None:
        raise HTTPException(status_code=404, detail="ImportPlan 不存在")

    # 校验 plan.source 与 URL source 一致
    if plan.source != source:
        raise HTTPException(
            status_code=400,
            detail=f"plan.source={plan.source} 与 URL source={source} 不匹配",
        )

    from app.api.imports import _legacy_plan_uses_durable_authority

    if _legacy_plan_uses_durable_authority(plan.plan_id, source):
        raise HTTPException(
            status_code=409,
            detail="该 TXT 预设已进入 durable_root 执行权威，禁止 legacy mirror",
        )

    # 校验 plan.status
    if plan.status != "confirmed":
        raise HTTPException(
            status_code=400,
            detail=f"plan.status 必须是 confirmed，当前为 {plan.status}",
        )

    # 提交异步任务
    manager = get_task_manager()
    try:
        record = manager.submit(
            task_type="mirror_generate",
            source=source,
            fn=_run_mirror_generate,
            plan=plan,
            message=f"镜像生成: {source}",
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "task_id": record.task_id,
        "status": record.status,
    }


@router.get("/tasks/{task_id}")
def get_task_status(task_id: str):
    """查询任务状态

    任务失败时仍返回 200，body.error 包含错误信息。
    """
    manager = get_task_manager()
    record = manager.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    return _record_to_dict(record)


# ============================================================
# 内部函数
# ============================================================

def _mirror_result_payload(result) -> dict:
    return {
        "plan_id": result.plan_id,
        "generated_count": result.generated_count,
        "skipped_count": result.skipped_count,
        "failed_count": result.failed_count,
        "items_count": len(result.items),
        "failed_items": [
            {"item_id": item.item_id, "message": item.message}
            for item in result.items
            if item.status == "failed"
        ],
        "mirror_root": result.mirror_root,
    }


def _run_mirror_generate(
    plan,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
) -> dict:
    """实际执行镜像生成（在线程池中运行）

    这里汇总镜像生成、追更登记、媒体库索引刷新整个任务链的日志；
    最终 result 始终保留累计日志，任务完成、刷新页面或 SQLite 恢复后不丢。
    """
    logs: list[dict] = []

    def emit(progress: int, message: str, patch: Optional[dict] = None) -> None:
        current = dict(patch or {})
        kind = str(current.pop("log_kind", "info"))
        append_task_log(logs, message, kind)
        current["logs"] = list(logs)
        if progress_callback:
            progress_callback(progress, message, current)

    if progress_callback and "progress_callback" in signature(generate_mirror).parameters:
        result = generate_mirror(plan, progress_callback=emit)
    else:
        result = generate_mirror(plan)
    payload = _mirror_result_payload(result)
    if result.status != "success":
        details = "；".join(result.errors[:3]) or "镜像生成器未返回具体错误"
        emit(99, "镜像生成未通过完整性检查", {**payload, "log_kind": "error"})
        raise RuntimeError(f"镜像生成失败：{details}")
    emit(94, "正在更新追更状态", {**payload, "log_kind": "info"})
    from app.tracking.registration import (
        reconcile_tracking_bindings_for_plan,
        register_seasonal_plan,
    )
    tracking = (
        register_seasonal_plan(plan)
        if plan.import_scope == "seasonal"
        else reconcile_tracking_bindings_for_plan(plan)
    )
    emit(97, "正在刷新媒体库索引", {**payload, "tracking": tracking})
    from app.library.service import publish_import_plan_to_library
    library_refresh = publish_import_plan_to_library(plan)
    from app.media_presets.service import mark_preset_lifecycle
    mark_preset_lifecycle(plan.plan_id, "mirrored")
    payload["tracking"] = tracking
    payload["library_refresh"] = library_refresh
    emit(99, "媒体库索引已刷新", {**payload, "log_kind": "done"})
    payload["logs"] = list(logs)
    return payload


def _record_to_dict(record) -> dict:
    """将 TaskRecord 转为 dict"""
    return {
        "task_id": record.task_id,
        "task_type": record.task_type,
        "source": record.source,
        "status": record.status,
        "progress": record.progress,
        "message": record.message,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "error": record.error,
        "result": record.result,
    }
