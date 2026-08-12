from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Optional

from app.core.paths import configured_mount_source
from app.import_plan.diff import compute_diff
from app.import_plan.incremental import build_incremental_plan, merge_incremental_plan
from app.import_plan.service import build_preview, confirm_plan
from app.import_plan.store import load_import_plan, save_import_plan
from app.library.service import rebuild_tracking_library_from_bindings, refresh_tracking_library_work
from app.mirror.generator import generate_mirror
from app.raw.store import load_raw_snapshot, save_raw_snapshot
from app.recognition.plan_recognizer import recognize_import_plan_media
from app.recognition.planner import build_draft_import_plan
from app.sources.local import LocalScanner
from app.tracking.models import (
    TrackingBinding,
    tracking_attention_from_scrape_result as _tracking_attention_from_scrape_result,
)
from app.tracking.store import (
    delete_tracking_binding,
    get_tracking_binding_by_id,
    list_tracking_bindings,
    save_tracking_scan_result,
)


def import_seasonal_root(
    root_path: str,
    logical_source: str = "local",
    include_scrape: bool = True,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """扫描一个包含多部作品的根目录，并复用标准新番导入流水线。"""
    root = Path(root_path).expanduser()
    if not root.is_dir():
        raise ValueError("所选新番根目录不可访问")
    if logical_source not in {"local", "pan115", "baidu"}:
        raise ValueError("未知的新番来源")

    logs: list[dict] = []

    def report(progress: int, message: str, patch: Optional[dict] = None) -> None:
        if should_cancel and should_cancel():
            raise RuntimeError("任务已停止")
        incoming_logs = (patch or {}).get("logs") or []
        incoming_kind = (
            str(incoming_logs[-1].get("kind") or "info")
            if incoming_logs and isinstance(incoming_logs[-1], dict)
            else "info"
        )
        logs.append({"kind": incoming_kind, "message": message})
        del logs[:-160]
        if progress_callback:
            payload = dict(patch or {})
            payload["logs"] = list(logs)
            progress_callback(progress, message, payload)

    report(5, "扫描新番根目录", {"root_path": str(root)})
    mounted_source = configured_mount_source(root)
    source = mounted_source or logical_source
    snapshot = LocalScanner().scan(
        str(root),
        source_root=str(root),
        logical_source=source,
        metadata_only=bool(mounted_source) or source in {"pan115", "baidu"},
        should_cancel=should_cancel,
    )
    if snapshot.video_count == 0:
        raise ValueError("所选目录中没有可识别的视频文件")
    snapshot.import_family = "anime"
    snapshot.import_scope = "seasonal"
    save_raw_snapshot(snapshot)

    report(24, f"识别 {snapshot.video_count} 个视频", {
        "root_path": str(root),
        "video_count": snapshot.video_count,
    })
    plan = recognize_import_plan_media(build_draft_import_plan(snapshot))
    plan.import_scope = "seasonal"
    plan.summary["import_scope"] = "seasonal"
    save_import_plan(plan)

    work_ids = {
        item.canonical_work_id or item.work_id
        for item in plan.items
        if item.resource_type == "video" and (item.canonical_work_id or item.work_id)
    }
    if not work_ids:
        raise ValueError("没有从根目录识别到独立作品")

    progress_context = {
        "root_path": str(root),
        "video_count": snapshot.video_count,
        "detected_work_count": len(work_ids),
    }
    report(32, f"识别到 {len(work_ids)} 部新番", progress_context)
    from app.import_pipeline.service import run_auto_import_pipeline

    def pipeline_progress(progress: int, message: str, patch: Optional[dict] = None) -> None:
        mapped = 32 + int(max(0, min(100, progress)) * 0.67)
        report(mapped, message, {**progress_context, **(patch or {})})

    result = run_auto_import_pipeline(
        source,
        plan.plan_id,
        include_scrape=include_scrape,
        progress_callback=pipeline_progress,
    )
    return {
        **result,
        "root_path": str(root),
        "detected_work_count": len(work_ids),
        "video_count": snapshot.video_count,
        "logs": list(logs),
    }


def scan_tracking_binding(
    binding_id: str,
    include_scrape: bool = True,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    binding = get_tracking_binding_by_id(binding_id)
    if binding is None:
        raise LookupError("追更绑定不存在")
    if binding.tracking_state != "tracking":
        raise ValueError("只有追更中的作品可以扫盘")

    logs: list[dict] = []

    def report(progress: int, message: str, patch: Optional[dict] = None) -> None:
        if should_cancel and should_cancel():
            raise RuntimeError("任务已停止")
        logs.append({"kind": "info", "message": message})
        del logs[:-120]
        if progress_callback:
            payload = {"logs": list(logs), "work_id": binding.work_id, "binding_id": binding.binding_id}
            if patch:
                payload.update(patch)
            progress_callback(progress, message, payload)

    root = Path(binding.root_path)
    report(5, f"检查来源：{root}")
    if not root.exists() or not root.is_dir():
        result = {
            "status": "source_unavailable", "work_id": binding.work_id,
            "binding_id": binding.binding_id, "deleted_count": 0,
            "message": "来源目录暂时不可用", "logs": logs,
        }
        _save_binding_result(binding, result, attention_state="source_unavailable", successful=False)
        return result

    report(15, "扫描当前作品目录")
    snapshot = LocalScanner().scan(
        str(root), source_root=str(root), logical_source=binding.logical_source, include_root=True,
        metadata_only=bool(configured_mount_source(root)),
        should_cancel=should_cancel,
    )
    snapshot.import_family = binding.import_family or "anime"
    save_raw_snapshot(snapshot, update_latest=False)

    if not binding.last_snapshot_id or not binding.baseline_plan_id:
        return _run_first_scan(binding, snapshot, include_scrape, logs, report, should_cancel)

    old_snapshot = load_raw_snapshot(binding.last_snapshot_id)
    base_plan = load_import_plan(plan_id=binding.baseline_plan_id)
    if old_snapshot is None or base_plan is None:
        return _run_first_scan(binding, snapshot, include_scrape, logs, report, should_cancel)
    if not _baseline_matches_binding(old_snapshot, base_plan, binding):
        # 旧目录树导入曾把整个“新番”根作为单作品基线，必须无损重建当前作品基线。
        report(28, "迁移旧新番基线")
        return _run_first_scan(binding, snapshot, include_scrape, logs, report, should_cancel)

    report(30, "计算目录变化")
    comparison_snapshot = _baseline_snapshot_for_plan(old_snapshot, base_plan)
    # 目录树首次导入的 relative_path 可能带“新番/作品”两层，而作品级真实
    # 目录扫描只会产生“作品/Season ...”。两者 real_path 相同，比较前必须
    # 统一到作品根，否则原有剧集会全部被误判为 moved。
    comparison_snapshot = _rebase_snapshot_to_tracking_root(comparison_snapshot, root)
    snapshot = _rebase_snapshot_to_tracking_root(snapshot, root)
    diff = compute_diff(comparison_snapshot, snapshot)
    counts = _diff_counts(diff)
    safe_append = _is_safe_seasonal_append(diff)
    if diff.safety.blocked and not safe_append:
        result = {
            "status": "waiting_review", "work_id": binding.work_id,
            "binding_id": binding.binding_id, "deleted_count": 0,
            **counts, "warnings": diff.safety.reasons, "logs": logs,
        }
        _save_binding_result(binding, result, attention_state="waiting_review", successful=False)
        return result

    changed_count = diff.added_count + diff.replaced_count + diff.moved_count + diff.renamed_count
    if diff.missing_count and changed_count == 0:
        result = {
            "status": "waiting_review", "work_id": binding.work_id,
            "binding_id": binding.binding_id, "deleted_count": 0,
            **counts,
            "issues": [{
                "code": "missing_files",
                "level": "warning",
                "message": f"有 {diff.missing_count} 个文件暂时不可用；未删除镜像、元数据或观看记录",
                "item_ids": [],
            }],
            "logs": logs,
        }
        _save_binding_result(binding, result, "waiting_review", False)
        return result
    if changed_count == 0:
        result = {
            "status": "succeeded", "work_id": binding.work_id,
            "binding_id": binding.binding_id, "deleted_count": 0,
            **counts, "message": "没有发现更新", "logs": logs,
        }
        _save_binding_result(
            replace(binding, last_snapshot_id=snapshot.snapshot_id),
            result, attention_state="ready", successful=True,
        )
        return result

    report(45, "识别新增和替换剧集")
    delta = build_incremental_plan(
        diff, binding.logical_source, str(root), new_snapshot=snapshot,
        allow_blocked=safe_append,
    )
    if delta is None:
        video_changes = [
            item for item in diff.items
            if item.resource_type == "video"
            and item.change_type in {"added", "replaced", "moved", "renamed"}
        ]
        if video_changes:
            result = {
                "status": "waiting_review", "work_id": binding.work_id,
                "binding_id": binding.binding_id, "deleted_count": 0, **counts,
                "issues": [{
                    "code": "incremental_plan_empty",
                    "level": "error",
                    "message": f"发现 {len(video_changes)} 个视频变化，但未生成增量计划；扫描基线未推进",
                    "item_ids": [item.raw_file_id for item in video_changes if item.raw_file_id],
                }],
                "logs": logs,
            }
            _save_binding_result(binding, result, "waiting_review", False)
            return result
        result = {"status": "succeeded", "deleted_count": 0, **counts, "logs": logs}
        _save_binding_result(replace(binding, last_snapshot_id=snapshot.snapshot_id), result, "ready", True)
        return result
    _apply_binding_identity(delta, binding)
    merged = merge_incremental_plan(base_plan, delta, diff)
    _apply_binding_identity(merged, binding)
    preview = build_preview(merged)
    blockers = _blocking_preview_issues(preview)
    if blockers:
        result = {
            "status": "waiting_review", "work_id": binding.work_id,
            "binding_id": binding.binding_id, "deleted_count": 0,
            **counts, "issues": blockers, "logs": logs,
        }
        _save_binding_result(binding, result, "waiting_review", False)
        return result

    return _execute_plan(binding, merged, snapshot.snapshot_id, include_scrape, counts, logs, report, should_cancel)


def scan_all_tracking(
    include_scrape: bool = True,
    logical_source: Optional[str] = None,
    work_ids: Optional[list[str]] = None,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    all_bindings, stale_binding_ids = _deduplicate_tracking_bindings(list_tracking_bindings("tracking"))
    for binding_id in stale_binding_ids:
        delete_tracking_binding(binding_id)
    selected_work_ids = set(work_ids) if work_ids is not None else None
    bindings = [
        binding for binding in all_bindings
        if (logical_source is None or binding.logical_source == logical_source)
        and (selected_work_ids is None or binding.work_id in selected_work_ids)
    ]
    results = []
    total = max(1, len(bindings))
    for index, binding in enumerate(bindings, start=1):
        if should_cancel and should_cancel():
            raise RuntimeError("任务已停止")
        if progress_callback:
            progress_callback(int((index - 1) / total * 95), f"扫描 {binding.display_title or binding.work_id}", None)
        try:
            def binding_progress(progress: int, message: str, patch: Optional[dict] = None) -> None:
                if progress_callback:
                    overall = int(((index - 1) + max(0, min(100, progress)) / 100) / total * 95)
                    progress_callback(overall, message, patch)

            binding_result = scan_tracking_binding(
                binding.binding_id,
                include_scrape=include_scrape,
                progress_callback=binding_progress,
                should_cancel=should_cancel,
            )
            binding_result.setdefault(
                "display_title",
                binding.display_title or binding.series_group or binding.work_id,
            )
            binding_result.setdefault(
                "added_episode_count",
                int(binding_result.get("added_count") or 0),
            )
            results.append(binding_result)
        except Exception as exc:
            if should_cancel and should_cancel():
                raise RuntimeError("任务已停止") from exc
            results.append({
                "status": "failed",
                "work_id": binding.work_id,
                "display_title": binding.display_title or binding.series_group or binding.work_id,
                "added_episode_count": 0,
                "error": str(exc),
                "deleted_count": 0,
            })
    if should_cancel and should_cancel():
        raise RuntimeError("任务已停止")
    # 扫描按钮承诺只处理当前可见范围；列表外的历史绑定不能借由修复步骤
    # 重新生成额外卡片。
    library_repair = rebuild_tracking_library_from_bindings(bindings)
    return {
        "status": "succeeded", "total": len(bindings), "results": results,
        "updated": sum(1 for item in results if item.get("added_count") or item.get("replaced_count")),
        "added_count": sum(int(item.get("added_count") or 0) for item in results),
        "added_episode_count": sum(int(item.get("added_episode_count") or 0) for item in results),
        "replaced_count": sum(int(item.get("replaced_count") or 0) for item in results),
        "unchanged": sum(1 for item in results if item.get("status") == "succeeded" and not item.get("added_count") and not item.get("replaced_count")),
        "waiting_review": sum(1 for item in results if item.get("status") == "waiting_review"),
        "failed": sum(1 for item in results if item.get("status") in {"failed", "source_unavailable"}),
        "library_repair": library_repair,
        "deduplicated_bindings": len(stale_binding_ids),
    }


def _deduplicate_tracking_bindings(bindings: list[TrackingBinding]) -> tuple[list[TrackingBinding], list[str]]:
    """同一来源、目录、季度只保留一条绑定，优先保留当前媒体库卡片的 ID。"""
    from app.library.store import load_library_index

    index = load_library_index()
    seasonal_work_ids = {
        work.work_id
        for work in (index.works if index else [])
        if work.import_scope == "seasonal"
    }
    grouped: dict[tuple[str, str, int | None], list[TrackingBinding]] = {}
    for binding in bindings:
        key = (
            binding.logical_source,
            os.path.normcase(os.path.normpath(binding.root_path)),
            binding.season_number,
        )
        grouped.setdefault(key, []).append(binding)

    selected: list[TrackingBinding] = []
    stale_ids: list[str] = []
    for group in grouped.values():
        preferred = next((item for item in group if item.work_id in seasonal_work_ids), group[0])
        selected.append(preferred)
        stale_ids.extend(item.binding_id for item in group if item.binding_id != preferred.binding_id)
    return selected, stale_ids


def _run_first_scan(binding, snapshot, include_scrape, logs, report, should_cancel=None):
    report(35, "识别作品与剧集")
    plan = recognize_import_plan_media(build_draft_import_plan(snapshot))
    _apply_binding_identity(plan, binding)
    preview = build_preview(plan)
    blockers = _blocking_preview_issues(preview)
    if blockers:
        save_import_plan(plan, update_latest=False)
        result = {
            "status": "waiting_review", "work_id": binding.work_id,
            "binding_id": binding.binding_id, "added_count": snapshot.video_count,
            "replaced_count": 0, "missing_count": 0, "deleted_count": 0,
            "issues": blockers, "plan_id": plan.plan_id, "logs": logs,
        }
        _save_binding_result(binding, result, "waiting_review", False)
        return result
    confirmed, error = confirm_plan(plan, force=False, update_latest=False)
    if error or confirmed is None:
        raise ValueError(error or "导入计划确认失败")
    counts = {"added_count": snapshot.video_count, "replaced_count": 0, "missing_count": 0,
              "moved_count": 0, "renamed_count": 0, "unchanged_count": 0}
    return _execute_plan(binding, confirmed, snapshot.snapshot_id, include_scrape, counts, logs, report, should_cancel)


def _execute_plan(binding, plan, snapshot_id, include_scrape, counts, logs, report, should_cancel=None):
    _restrict_plan_to_binding(plan, binding)
    if not plan.items:
        result = {
            "status": "failed", "work_id": binding.work_id, "binding_id": binding.binding_id,
            "deleted_count": 0, **counts, "errors": ["追更计划中没有当前作品的可更新剧集"], "logs": logs,
        }
        _save_binding_result(binding, result, "waiting_review", False)
        return result
    report(60, "生成新增剧集镜像")
    plan.status = "confirmed"
    mirror = generate_mirror(plan, update_latest=False)
    if mirror.status not in {"success"}:
        result = {
            "status": "failed", "work_id": binding.work_id, "binding_id": binding.binding_id,
            "deleted_count": 0, **counts, "errors": mirror.errors, "logs": logs,
        }
        _save_binding_result(binding, result, "waiting_review", False)
        return result

    scrape_result = None
    attention = "waiting_metadata" if not include_scrape else "ready"
    if include_scrape:
        report(75, "补充在线元数据")
        from app.scrape.auto import run_auto_scrape
        scrape_result = run_auto_scrape(
            source=plan.source, plan_id=plan.plan_id, include_episode=True,
            should_cancel=should_cancel,
        )
        attention = _tracking_attention_from_scrape_result(scrape_result)

    report(92, "刷新媒体库索引")
    refresh_tracking_library_work(plan, binding.work_id)
    result = {
        "status": "succeeded", "work_id": binding.work_id, "binding_id": binding.binding_id,
        "plan_id": plan.plan_id, "snapshot_id": snapshot_id, "deleted_count": 0,
        **counts,
        "mirror": {
            "generated_count": mirror.generated_count,
            "skipped_count": mirror.skipped_count,
            "failed_count": mirror.failed_count,
        },
        "scrape": scrape_result, "logs": logs,
    }
    _save_binding_result(
        replace(binding, last_snapshot_id=snapshot_id, baseline_plan_id=plan.plan_id),
        result, attention, True,
    )
    return result


def _apply_binding_identity(plan, binding: TrackingBinding) -> None:
    title = binding.display_title or binding.series_group
    series_group = binding.series_group or title
    for item in plan.items:
        if item.resource_type != "video":
            continue
        item.canonical_work_id = binding.work_id
        item.work_id = binding.work_id
        if title:
            item.work_title = title
        if series_group:
            item.series_group = series_group
        item.import_family = "anime"
        if item.media_type != "movie":
            item.media_type = "tv"
            item.show_type = "anime_series"
            item.card_type = "main_series"
            if binding.season_number is not None and item.group_type == "season":
                item.season_number = binding.season_number
    plan.import_scope = "seasonal"
    plan.summary["tracking_binding_id"] = binding.binding_id


def _restrict_plan_to_binding(plan, binding: TrackingBinding) -> None:
    """追更计划只能包含当前绑定根目录的条目，绝不复用同来源其他作品。"""
    root = Path(binding.root_path)
    selected = []
    for item in plan.items:
        if item.resource_type != "video" or not item.real_path:
            continue
        if not _path_is_within(item.real_path, root):
            continue
        selected.append(item)
    plan.items = selected


def _path_is_within(path: str, root: Path) -> bool:
    """检查路径归属；挂载网盘可能不支持 Path.resolve，不能因此丢弃剧集。"""
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        try:
            normalized_path = os.path.normcase(os.path.normpath(str(path)))
            normalized_root = os.path.normcase(os.path.normpath(str(root)))
            return os.path.commonpath([normalized_path, normalized_root]) == normalized_root
        except ValueError:
            return False


def _baseline_matches_binding(snapshot, plan, binding: TrackingBinding) -> bool:
    """确认基线是当前作品目录的快照与计划，而不是整个新番根目录。"""
    if not _path_is_within(str(snapshot.source_root or ""), Path(binding.root_path)):
        return False
    videos = [item for item in plan.items if item.resource_type == "video"]
    return bool(videos) and all(_path_is_within(item.real_path, Path(binding.root_path)) for item in videos)


def _blocking_preview_issues(preview) -> list[dict]:
    """只返回真正阻塞追更执行的结构性错误。

    低置信度识别属于可延后的 warning；镜像和后续刮削先继续，
    由 review queue 保留人工处理入口。缺少必要归位字段仍会以 error 阻塞。
    """
    return [
        {"code": issue.code, "level": issue.level, "message": issue.message, "item_ids": issue.item_ids}
        for issue in preview.issues
        if issue.level == "error"
    ]


def _diff_counts(diff) -> dict:
    return {
        "added_count": diff.added_count, "replaced_count": diff.replaced_count,
        "added_episode_count": sum(
            1 for item in diff.items
            if item.change_type == "added" and item.resource_type == "video"
        ),
        "missing_count": diff.missing_count, "moved_count": diff.moved_count,
        "renamed_count": diff.renamed_count, "unchanged_count": diff.unchanged_count,
    }


def _baseline_snapshot_for_plan(snapshot, plan):
    """以累计计划校准比较基线，恢复曾被错误推进快照吞掉的视频。"""
    planned_video_paths = {
        _relative_path_key(item.relative_path)
        for item in plan.items
        if item.resource_type == "video" and item.relative_path
    }
    if not planned_video_paths:
        return snapshot
    files = [
        item for item in snapshot.files
        if item.resource_hint != "video"
        or _relative_path_key(item.relative_path) in planned_video_paths
    ]
    if len(files) == len(snapshot.files):
        return snapshot
    return replace(
        snapshot,
        file_count=len(files),
        video_count=sum(1 for item in files if item.resource_hint == "video"),
        files=files,
    )


def _rebase_snapshot_to_tracking_root(snapshot, root: Path):
    """把作品级快照路径统一为 ``作品目录名/子路径``。

    只改可重建的 relative_path，不接触来源文件。挂载盘上的 resolve 可能
    不可用，因此使用 normpath/commonpath 做纯字符串路径归属校验。
    """
    parent = root.parent
    rebased_files = []
    for item in snapshot.files:
        relative_path = item.relative_path
        if item.real_path and _path_is_within(item.real_path, root):
            try:
                relative_path = os.path.relpath(
                    os.path.normpath(item.real_path),
                    os.path.normpath(str(parent)),
                ).replace("\\", "/")
            except (OSError, ValueError):
                pass
        rebased_files.append(replace(item, relative_path=relative_path))
    return replace(snapshot, source_root=str(root), files=rebased_files)


def _relative_path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path).replace("/", os.sep)))


def _is_safe_seasonal_append(diff) -> bool:
    """新番追更的纯新增不受总量变化阈值阻断。

    周更作品在只有 1-2 集的早期阶段，一次补入多集会自然超过通用
    总量变化阈值。只要旧文件没有缺失、替换或路径异常，这属于安全的
    正常追加，仍保留其他风险信号的人工确认机制。
    """
    added_videos = [
        item for item in diff.items
        if item.change_type == "added" and item.resource_type == "video"
    ]
    replaced_videos = [
        item for item in diff.items
        if item.change_type == "replaced" and item.resource_type == "video"
    ]
    return bool(
        added_videos
        and diff.missing_count == 0
        and not replaced_videos
        and diff.moved_count == 0
        and diff.renamed_count == 0
        and diff.uncertain_count == 0
    )


def _save_binding_result(binding, result: dict, attention_state: str, successful: bool) -> None:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    save_tracking_scan_result(replace(
        binding,
        attention_state=attention_state,
        last_scan_at=now,
        last_successful_scan_at=now if successful else binding.last_successful_scan_at,
        last_result=result,
    ), result)
