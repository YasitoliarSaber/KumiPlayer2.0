"""流水线 handler（durable job 可恢复执行）。

- mirror_revision：装载不可变 revision → 现有 generate_mirror；成功后登记 artifact_records；
- scrape_revision：现有 run_auto_scrape；仅单元 closure 后允许创建（由 orchestrator 控制）。
"""

from __future__ import annotations

from typing import Any

from app.import_plan import revision_store
from app.jobs.registry import register


def _register_artifacts(revision_id: str, plan: Any, result_items: list) -> int:
    """登记真实物化的 .strm：以 ``MirrorItemResult`` 为唯一证据。

    - status == generated：strm_path 必须真实 is_file()；
    - status == skipped + strm_path 非空 + 文件存在（generator「内容相同，跳过」
      语义，且回填了 target 路径）→ 同样登记——crash recovery：首次写盘成功
      但 artifact 登记前进程崩溃，重跑时 generator 判定内容相同走 skipped，
      这里把 artifact 补回来，Library projection 不再误判不可播放；
    - ignored/failed 或空路径 → 不登记。

    artifact path 以 ``MirrorItemResult.strm_path`` 为准，不从 plan 猜。
    """
    from pathlib import Path

    from app.pipeline.artifacts import upsert_artifact

    work_by_item = {str(item.id): (item.work_id or "") for item in plan.items}
    registered = 0
    for result_item in result_items:
        status = str(getattr(result_item, "status", "") or "")
        path_value = str(getattr(result_item, "strm_path", "") or "")
        if status not in ("generated", "skipped") or not path_value:
            continue
        if not Path(path_value).is_file():
            continue
        upsert_artifact(
            kind="strm",
            path=path_value,
            revision_id=revision_id,
            work_id=work_by_item.get(str(getattr(result_item, "item_id", "") or ""), ""),
        )
        registered += 1
    return registered


def handle_mirror_revision(payload: dict, progress_callback=None, should_cancel=None) -> dict:
    """生成镜像：只消费 confirmed revision，不重新识别、不调用刮削。"""
    from app.mirror.generator import generate_mirror

    revision_id = str(payload.get("revision_id") or "")
    if not revision_id:
        raise ValueError("mirror payload 缺少 revision_id")
    # Module 5 Review Fix：stale/superseded job 在任何副作用前 no-op 正常结束
    # （不访问网络、不写文件、不写 binding/artifact、不 enqueue 下游、不重试）。
    if not revision_store.is_current_revision(revision_id):
        return {"status": "obsolete", "revision_id": revision_id, "mirror_status": "skipped"}
    plan = revision_store.load_plan(revision_id)
    if plan is None:
        raise ValueError(f"revision 不存在: {revision_id}")
    if plan.status not in ("confirmed", "executed"):
        raise ValueError(f"revision 未确认，不能生成镜像: {plan.status}")
    result = generate_mirror(plan, progress_callback=progress_callback, persist_plan=False)
    # 镜像结果状态检查：failed / partial_failed 不得登记 artifact、
    # 不得触发刮削（部分失败只登记成功项，单元不完整不进入刮削）。
    result_status = getattr(result, "status", "") or "failed"
    if result_status == "failed":
        raise ValueError(
            "镜像生成失败: " + "；".join(getattr(result, "errors", []) or ["未知原因"])
        )
    revision_store.persist_execution_fields(plan)
    generated = int(getattr(result, "generated_count", 0) or 0)
    result_items = list(getattr(result, "items", []) or [])
    if result_items:
        _register_artifacts(revision_id, plan, result_items)
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
    # Module 5 Review Fix：stale/superseded job 在任何副作用前 no-op 正常结束，
    # 绝不调用 run_auto_scrape（0 网络、0 文件、0 binding、0 下游）。
    if not revision_store.is_current_revision(revision_id):
        return {"status": "obsolete", "revision_id": revision_id, "scrape": {"status": "skipped"}}
    result = run_auto_scrape(
        source,
        plan_id=revision_id,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
        publish_library=False,
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
    # Module 5：成功事实只来自真实 scrape target 的 effective upsert
    # （execute_scrape → scrape_bindings 稳定行），不再插入「整 revision
    # 粗粒度 binding」（record_scrape_outcome 只保留失败路径）。
    # 刮削完成 → 媒体库索引重建（单通道）
    from app.pipeline import orchestrator as _orch

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
