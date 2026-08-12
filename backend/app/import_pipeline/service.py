# -*- coding: utf-8 -*-
"""Automated import pipeline.

The default happy path should be hands-off:
draft plan -> auto confirm -> mirror generation -> auto scrape.
Manual work is reserved for blocking import issues or scrape review items.
"""

from __future__ import annotations

from typing import Callable, Optional

from app.import_plan.service import build_preview, confirm_plan
from app.import_plan.store import load_import_plan
from app.mirror.generator import generate_mirror
from app.scrape.auto import run_auto_scrape
from app.tasks.logs import append_task_log


def run_auto_import_pipeline(
    source: str,
    plan_id: str,
    include_scrape: bool = True,
    include_episode: bool = True,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
) -> dict:
    """Confirm an import plan, generate mirror, then auto scrape if possible."""
    logs: list[dict] = []

    def push(progress: int, message: str, patch: Optional[dict] = None, *, kind: str = "info") -> None:
        append_task_log(logs, message, kind)
        payload = {"logs": list(logs), "plan_id": plan_id, "source": source}
        if patch:
            payload.update(patch)
        if progress_callback:
            progress_callback(progress, message, payload)

    push(8, "读取导入计划")
    plan = load_import_plan(plan_id=plan_id)
    if plan is None:
        raise ValueError(f"ImportPlan 不存在: {plan_id}")
    if plan.source != source:
        raise ValueError(f"plan.source={plan.source} 与 source={source} 不匹配")

    preview = build_preview(plan)
    deferred_issues = [
        issue for issue in preview.issues
        if issue.code == "needs_review"
    ]
    blocking_issues = [
        issue for issue in preview.issues
        if issue.level == "error"
    ]
    if deferred_issues:
        push(15, f"发现 {sum(len(issue.item_ids) for issue in deferred_issues)} 项低置信内容，先继续导入，稍后可处理", {
            "preview_summary": preview.summary,
            "deferred_issues": [_issue_to_dict(issue) for issue in deferred_issues],
        }, kind="warn")
    if blocking_issues:
        push(15, f"导入计划有 {len(blocking_issues)} 个阻塞问题，需要处理", {
            "preview_summary": preview.summary,
            "issues": [_issue_to_dict(issue) for issue in blocking_issues],
        }, kind="warn")
        from app.core.error_log import log_error
        for issue in blocking_issues:
            log_error(
                stage="import_plan",
                category=issue.code or issue.level,
                message=f"自动导入管线因阻塞问题暂停: {issue.message}",
                level=issue.level,
                source=source,
                context={
                    "plan_id": plan_id,
                    "issue_code": issue.code,
                    "item_ids": issue.item_ids,
                },
            )

    if plan.status == "draft":
        push(18, "自动确认导入计划")
        confirmed_plan, error = confirm_plan(plan, force=False)
        if error:
            push(100, f"自动确认失败：{error}", {
                "blocked": True,
                "stage": "confirm",
                "current_target": "导入确认失败",
            }, kind="error")
            return {
                "source": source,
                "plan_id": plan_id,
                "status": "blocked",
                "stage": "confirm",
                "error": error,
                "logs": logs,
            }
        plan = confirmed_plan
    elif plan.status == "confirmed":
        push(18, "导入计划已确认", kind="done")
    elif plan.status == "executed":
        push(18, "导入计划已执行，跳过镜像生成")
    else:
        raise ValueError(f"不支持的 plan.status: {plan.status}")

    mirror_payload = None
    if plan.status == "confirmed":
        push(32, "自动生成镜像", {"current_target": "生成 .strm 镜像"})
        mirror_result = generate_mirror(plan)
        mirror_payload = {
            "status": mirror_result.status,
            "generated_count": mirror_result.generated_count,
            "skipped_count": mirror_result.skipped_count,
            "failed_count": mirror_result.failed_count,
            "mirror_root": mirror_result.mirror_root,
            "errors": mirror_result.errors,
        }
        if mirror_result.status != "success":
            push(100, "镜像生成失败，需要人工处理", {
                "blocked": True,
                "stage": "mirror",
                "mirror": mirror_payload,
                "current_target": "镜像生成失败",
            }, kind="error")
            return {
                "source": source,
                "plan_id": plan_id,
                "status": "blocked",
                "stage": "mirror",
                "mirror": mirror_payload,
                "logs": logs,
            }
        from app.tracking.registration import (
            reconcile_tracking_bindings_for_plan,
            register_seasonal_plan,
        )
        mirror_payload["tracking"] = (
            register_seasonal_plan(plan)
            if plan.import_scope == "seasonal"
            else reconcile_tracking_bindings_for_plan(plan)
        )
        push(48, f"镜像完成：生成 {mirror_result.generated_count}，跳过 {mirror_result.skipped_count}", {
            "mirror": mirror_payload,
            "current_target": "镜像已生成",
        }, kind="done")

    scrape_payload = None
    if include_scrape:
        push(52, "自动刮削匹配", {"current_target": "自动刮削"})

        def scrape_progress(progress: int, message: str = "", result_patch: Optional[dict] = None) -> None:
            mapped = 52 + int(max(0, min(100, progress)) * 0.45)
            patch = dict(result_patch or {})
            patch["stage"] = "scrape"
            if mirror_payload:
                patch["mirror"] = mirror_payload
            push(mapped, message or "自动刮削", patch)

        scrape_payload = run_auto_scrape(
            source=source,
            plan_id=plan_id,
            include_episode=include_episode,
            progress_callback=scrape_progress,
        )
        scrape_failed = int(scrape_payload.get("failed", 0)) > 0 or bool(scrape_payload.get("error"))
        if scrape_failed:
            failed_messages = [
                str(item.get("error", "")).strip()
                for item in scrape_payload.get("results", [])
                if item.get("status") == "failed" and item.get("error")
            ]
            error = str(scrape_payload.get("error") or (failed_messages[0] if failed_messages else "刮削或媒体库索引刷新失败"))
            push(100, f"自动导入在刮削阶段停止：{error}", {
                "blocked": True,
                "stage": "scrape",
                "mirror": mirror_payload,
                "scrape": scrape_payload,
                "current_target": "刮削或索引刷新失败",
            }, kind="error")
            return {
                "source": source,
                "plan_id": plan_id,
                "status": "blocked",
                "stage": "scrape",
                "error": error,
                "mirror": mirror_payload,
                "scrape": scrape_payload,
                "logs": logs,
            }

    push(100, "自动导入流水线完成", {
        "stage": "done",
        "mirror": mirror_payload,
        "scrape": scrape_payload,
        "current_target": "完成",
    }, kind="done")
    return {
        "source": source,
        "plan_id": plan_id,
        "status": "succeeded",
        "stage": "done",
        "mirror": mirror_payload,
        "scrape": scrape_payload,
        "logs": logs,
    }


def _issue_to_dict(issue) -> dict:
    return {
        "code": issue.code,
        "level": issue.level,
        "message": issue.message,
        "item_ids": issue.item_ids,
    }
