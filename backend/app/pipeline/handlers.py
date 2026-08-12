"""流水线 handler（durable job 可恢复执行）。

- mirror_revision：装载不可变 revision → 现有 generate_mirror；成功后登记 artifact_records；
- scrape_revision：现有 run_auto_scrape；仅单元 closure 后允许创建（由 orchestrator 控制）。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.database import get_connection
from app.import_plan import revision_store
from app.jobs.registry import register


def _register_artifacts(revision_id: str, plan: Any, succeeded_item_ids: set[str]) -> int:
    """只登记生成器已确认写入的 .strm 产物。

    revision 的 target 路径只是计划/执行投影，不能证明文件写入成功；必须以
    ``MirrorItemResult.status == generated`` 为唯一事实来源。
    """
    conn = get_connection()
    registered = 0
    for item in plan.items:
        if (
            item.action != "generate_strm"
            or not item.target_strm_path
            or item.id not in succeeded_item_ids
        ):
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO artifact_records (artifact_id, kind, path, revision_id, work_id, created_at)
            VALUES (?, 'strm', ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, item.target_strm_path, revision_id, item.work_id or "", revision_store.now_iso()),
        )
        registered += max(0, cursor.rowcount)
    conn.commit()
    return registered


def handle_mirror_revision(payload: dict, progress_callback=None, should_cancel=None) -> dict:
    """生成镜像：只消费 confirmed revision，不重新识别、不调用刮削。"""
    from app.mirror.generator import generate_mirror

    revision_id = str(payload.get("revision_id") or "")
    if not revision_id:
        raise ValueError("mirror payload 缺少 revision_id")
    plan = revision_store.load_plan(revision_id)
    if plan is None:
        raise ValueError(f"revision 不存在: {revision_id}")
    if plan.status not in ("confirmed", "executed"):
        raise ValueError(f"revision 未确认，不能生成镜像: {plan.status}")
    result = generate_mirror(plan, progress_callback=progress_callback)
    # 镜像结果状态检查：failed / partial_failed 不得登记 artifact、
    # 不得触发刮削（部分失败只登记成功项，单元不完整不进入刮削）。
    result_status = getattr(result, "status", "") or "failed"
    if result_status == "failed":
        raise ValueError(
            "镜像生成失败: " + "；".join(getattr(result, "errors", []) or ["未知原因"])
        )
    revision_store.persist_execution_fields(plan)
    generated = int(getattr(result, "generated_count", 0) or 0)
    succeeded_item_ids = {
        str(item.item_id) for item in (getattr(result, "items", []) or [])
        if getattr(item, "status", "") == "generated"
    }
    if succeeded_item_ids:
        _register_artifacts(revision_id, plan, succeeded_item_ids)
    if result_status == "success":
        # mirror 成功后、单元 closure 时进入刮削（scrape 全局单通道由 resource_key 保证）
        unit_id = str(payload.get("unit_id") or "")
        from app.pipeline import orchestrator

        if unit_id and orchestrator.unit_is_closed(unit_id):
            orchestrator.enqueue_scrape(
                revision_id,
                str(payload.get("source") or plan.source or "openlist"),
                unit_id=unit_id,
            )
    return {
        "status": "succeeded",
        "revision_id": revision_id,
        "mirror_status": result_status,
        "strm_count": generated,
        "failed_count": int(getattr(result, "failed_count", 0) or 0),
        "skipped_count": int(getattr(result, "skipped_count", 0) or 0),
    }


def handle_scrape_revision(payload: dict, progress_callback=None, should_cancel=None) -> dict:
    """自动刮削（run_auto_scrape）；scrape 全局单通道由 resource_key 保证。

    错误语义：run_auto_scrape 的 error 信号（status=failed 或 result.error 非空）
    通过抛错传播给 JobRunner，不得被标 succeeded。
    """
    from app.scrape.auto import run_auto_scrape

    revision_id = str(payload.get("revision_id") or "")
    source = str(payload.get("source") or "openlist")
    if not revision_id:
        raise ValueError("scrape payload 缺少 revision_id")
    result = run_auto_scrape(
        source,
        plan_id=revision_id,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
    )
    result_status = str(result.get("status") or "succeeded")
    error = result.get("error") or ""
    if result_status == "failed" or error:
        from app.pipeline.library_handler import record_scrape_outcome

        record_scrape_outcome(
            revision_id=revision_id, work_id="", source=source,
            succeeded=False, error=str(error or result_status),
        )
        raise RuntimeError(
            f"自动刮削失败: {error or result_status}"
        )
    # SQLite 单写：刮削成功登记 scrape_bindings（替代 JSON ScrapeMap 新链路路径）
    from app.import_plan import revision_store as _rev_store
    from app.pipeline import orchestrator as _orch
    from app.pipeline.library_handler import record_scrape_outcome

    plan = _rev_store.load_plan(revision_id)
    work_id = ""
    work_title = ""
    provider_id = ""
    if plan is not None and plan.items:
        work_id = str(plan.items[0].work_id or "")
        work_title = str(plan.items[0].work_title or "")
        provider_id = str(plan.items[0].provider_id or "")
    record_scrape_outcome(
        revision_id=revision_id, work_id=work_id, work_title=work_title,
        source=source, provider_id=provider_id, succeeded=True,
    )
    # 刮削完成 → 媒体库索引重建（单通道）
    unit_id = str(payload.get("unit_id") or "")
    job = _orch.enqueue_library_rebuild(unit_id=unit_id)
    return {"status": result_status, "revision_id": revision_id, "scrape": result, "library_rebuild_job": job}


def register_pipeline_handlers() -> None:
    register("mirror_revision", handle_mirror_revision)
    register("scrape_revision", handle_scrape_revision)
    from app.pipeline.discovery_handler import register_discovery_handler
    from app.pipeline.library_handler import register_library_handler

    register_discovery_handler()
    register_library_handler()
