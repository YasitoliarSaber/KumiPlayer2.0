"""Source Catalog 导入批次的只读状态投影。

批次、revision 与 durable jobs 仍各自保有事实；本模块只把它们关联为可恢复的
前端观察模型，绝不推进任一状态机。
"""

from __future__ import annotations

from typing import Any

from app.catalog import store as catalog_store
from app.import_plan import revision_store
from app.jobs import store as job_store


def _job_summary(job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "result": job.result,
    }


def _revision_jobs() -> dict[str, dict[str, Any]]:
    """按 revision 聚合下游任务；jobs 表是恢复时唯一可信任务来源。"""
    grouped: dict[str, dict[str, Any]] = {}
    for job in job_store.list_jobs(limit=1000):
        revision_id = str(job.payload.get("revision_id") or "")
        if not revision_id:
            continue
        bucket = grouped.setdefault(revision_id, {})
        if job.job_type == "mirror_revision":
            bucket["mirror"] = _job_summary(job)
        elif job.job_type == "scrape_revision":
            bucket["scrape"] = _job_summary(job)
        elif job.job_type == "library_rebuild":
            # library rebuild 以 unit_id 合并，不能反向唯一归属 revision；由 scrape
            # 终态 result 中的 library_rebuild_job 再精确关联。
            bucket.setdefault("library_candidates", []).append(_job_summary(job))
    return grouped


def _pipeline_state(revision: dict[str, Any], jobs: dict[str, Any]) -> str:
    mirror = jobs.get("mirror")
    scrape = jobs.get("scrape")
    library = jobs.get("library")
    if revision.get("status") == "draft":
        return "needs_review"
    if not mirror:
        return "queued"
    if mirror["status"] in {"failed", "cancelled"}:
        return mirror["status"]
    if mirror["status"] in {"queued", "running"}:
        return "mirroring"
    if mirror["status"] != "succeeded":
        return mirror["status"]
    if not scrape:
        return "mirrored"
    if scrape["status"] in {"failed", "cancelled"}:
        return scrape["status"]
    if scrape["status"] in {"queued", "running"}:
        return "scraping"
    if scrape["status"] == "succeeded":
        # 刮削成功不代表可观察流程已经结束：资料写入后仍可能在重建媒体库。
        # 仅当 scrape result 中确实回传了 library job 时才等待它，兼容旧任务记录。
        if library:
            if library["status"] in {"failed", "cancelled"}:
                return library["status"]
            if library["status"] in {"queued", "running"}:
                return "updating_library"
            if library["status"] != "succeeded":
                return library["status"]
        return "completed"
    return scrape["status"]


def _unit_payload(raw: dict[str, Any], jobs_by_revision: dict[str, dict[str, Any]]) -> dict[str, Any]:
    revision_id = str(raw.get("revision_id") or "")
    result = {
        "unit_id": str(raw.get("unit_id") or ""),
        "revision_id": revision_id,
        "work_title": str(raw.get("work_title") or raw.get("boundary") or "未命名作品"),
        "boundary": str(raw.get("boundary") or ""),
        "video_count": int(raw.get("video_count") or 0),
        "discovery_status": str(raw.get("status") or ""),
        "state": "needs_review" if raw.get("status") == "needs_review" else "discovering",
    }
    if not revision_id:
        return result
    revision = revision_store.load_revision(revision_id)
    if revision is None:
        result["state"] = "failed"
        result["error"] = "识别版本已不可用"
        return result
    jobs = dict(jobs_by_revision.get(revision_id, {}))
    scrape_result = (jobs.get("scrape") or {}).get("result") or {}
    library_job_id = str(scrape_result.get("library_rebuild_job") or "")
    if library_job_id:
        library_job = job_store.get_job(library_job_id)
        if library_job is not None:
            jobs["library"] = _job_summary(library_job)
    result.update({
        "revision_status": revision.get("status") or "",
        "state": _pipeline_state(revision, jobs),
        "mirror_job": jobs.get("mirror"),
        "scrape_job": jobs.get("scrape"),
    })
    if jobs.get("library"):
        result["library_rebuild_job"] = jobs["library"]
    return result


def refresh_batch_status(batch: dict, *, persist: bool = True) -> dict:
    """投影批次与下游作业状态，供 OpenList 和本地入口共同读取。"""
    discovery_jobs: dict[str, Any] = {}
    for root in batch.get("roots", []):
        jobs = job_store.list_discovery_jobs_for_root(root["root_id"])
        generation = root.get("generation")
        discovery_jobs[root["root_id"]] = next(
            (job for job in jobs if generation is not None and int(job.payload.get("generation") or 0) == int(generation)),
            jobs[0] if jobs else None,
        )

    jobs_by_revision = _revision_jobs()
    root_payloads: list[dict[str, Any]] = []
    statuses: list[str] = []
    for root in batch.get("roots", []):
        item = dict(root)
        job = discovery_jobs.get(root["root_id"])
        if job is None:
            status = str(root.get("status") or "pending")
            item.update({"job_id": "", "job_status": status, "progress": 0, "units": []})
        else:
            status = job.status
            if persist and status in {"succeeded", "failed", "cancelled"}:
                catalog_store.update_import_batch_root(
                    batch["batch_id"], root["root_id"], status=status, error_kind=job.error or "",
                )
            raw_units = (job.result or {}).get("units") or [] if status == "succeeded" else []
            units = [_unit_payload(unit, jobs_by_revision) for unit in raw_units if isinstance(unit, dict)]
            item.update({
                "job_id": job.job_id,
                "job_status": status,
                "progress": job.progress,
                "message": job.message,
                "error": job.error,
                "units": units,
                "plan_ids": [unit["revision_id"] for unit in units if unit.get("revision_id")],
            })
        statuses.append(status)
        root_payloads.append(item)

    if statuses and all(status == "succeeded" for status in statuses):
        batch_status = "succeeded"
    elif any(status == "running" for status in statuses):
        batch_status = "running"
    elif any(status == "queued" for status in statuses):
        batch_status = "pending"
    elif any(status == "failed" for status in statuses):
        batch_status = "partial_failed" if any(status == "succeeded" for status in statuses) else "failed"
    elif any(status == "cancelled" for status in statuses):
        batch_status = "cancelled"
    else:
        batch_status = str(batch.get("status") or "pending")
    if persist and batch_status != batch.get("status"):
        catalog_store.update_import_batch(batch["batch_id"], status=batch_status)
    result = dict(batch)
    result.update({
        "status": batch_status,
        "roots": root_payloads,
        "job_ids": [item["job_id"] for item in root_payloads if item.get("job_id")],
    })
    return result
