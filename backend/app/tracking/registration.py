from __future__ import annotations

import os
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.import_plan.store import save_import_plan
from app.library.index import _library_work_id
from app.raw.store import load_raw_snapshot, save_raw_snapshot
from app.raw.models import RawSnapshot
from app.tracking.models import TrackingBinding
from app.tracking.store import (
    delete_tracking_binding,
    get_tracking_binding_by_identity,
    get_tracking_binding_by_root,
    list_tracking_bindings,
    upsert_tracking_binding,
)


_SEASON_DIR = re.compile(r"^(?:season\s*\d+|s\d+(?:\.\d+)?|specials?|sp)$", re.IGNORECASE)


def reconcile_tracking_bindings_for_plan(plan: ImportPlan) -> dict:
    """让明确的导入分类覆盖同批作品遗留的追更控制状态。"""
    if plan.import_scope == "seasonal":
        return {"removed": 0}

    work_ids = {
        identity
        for item in plan.items
        if item.resource_type == "video"
        for identity in (item.work_id, item.canonical_work_id, _library_work_id(item))
        if identity
    }
    if not work_ids:
        return {"removed": 0}

    removed = 0
    for binding in list_tracking_bindings():
        if binding.logical_source != plan.source or binding.work_id not in work_ids:
            continue
        removed += int(delete_tracking_binding(binding.binding_id))
    return {"removed": removed}


def register_seasonal_plan(plan: ImportPlan) -> dict:
    """为新番目录树中的独立作品目录注册现有追更绑定。"""
    if plan.import_scope != "seasonal":
        return {"registered": 0, "skipped_loose": 0}
    snapshot = load_raw_snapshot(plan.source_snapshot_id)
    if snapshot is None or not snapshot.source_root:
        return {"registered": 0, "skipped_loose": 0}
    source_root = Path(snapshot.source_root)
    grouped: dict[str, list[ImportPlanItem]] = defaultdict(list)
    for item in plan.items:
        if item.resource_type == "video" and item.work_id and item.real_path:
            grouped[_library_work_id(item)].append(item)

    registered = 0
    skipped_loose = 0
    migrated = 0
    for work_id, items in grouped.items():
        root_groups = _root_groups(items)
        if not root_groups:
            skipped_loose += 1
            continue
        root, primary_items = min(root_groups.items(), key=lambda pair: _root_sort_key(pair[0], pair[1]))
        if root is None or _same_path(root, source_root) or not root.is_dir():
            skipped_loose += 1
            continue
        first = min(primary_items, key=_item_sort_key)
        seasons = [item.season_number for item in primary_items if item.season_number is not None and item.season_number > 0]
        season_number = min(seasons) if seasons else None
        existing_root = get_tracking_binding_by_root(plan.source, str(root), season_number)
        legacy = existing_root or get_tracking_binding_by_identity(first.work_id, str(root), season_number)
        baseline_snapshot, baseline_plan = _build_work_scoped_baseline(plan, snapshot, root, work_id)
        binding = TrackingBinding(
            work_id=work_id,
            display_title=first.work_title or first.series_group or work_id,
            logical_source=plan.source,
            root_path=str(root),
            import_family=plan.import_family or "anime",
            season_number=season_number,
            series_group=first.series_group or first.work_title,
            tracking_state="tracking",
            attention_state="ready",
            last_snapshot_id=baseline_snapshot.snapshot_id,
            baseline_plan_id=baseline_plan.plan_id,
        )
        if legacy is not None:
            binding = replace(
                binding,
                binding_id=legacy.binding_id,
                tracking_state=legacy.tracking_state,
                attention_state=legacy.attention_state,
                last_snapshot_id=(legacy.last_snapshot_id if _baseline_matches_root(legacy, root) else baseline_snapshot.snapshot_id),
                baseline_plan_id=(legacy.baseline_plan_id if _baseline_matches_root(legacy, root) else baseline_plan.plan_id),
                last_scan_at=legacy.last_scan_at,
                last_successful_scan_at=legacy.last_successful_scan_at,
                last_result=legacy.last_result,
                created_at=legacy.created_at,
            )
        saved = upsert_tracking_binding(binding)
        for root_items in root_groups.values():
            stale = _legacy_binding_for_items(root_items)
            if stale is not None and stale.work_id != saved.work_id:
                migrated += int(delete_tracking_binding(stale.binding_id))
        registered += 1
    return {"registered": registered, "skipped_loose": skipped_loose, "migrated": migrated}


def _build_work_scoped_baseline(
    plan: ImportPlan,
    snapshot: RawSnapshot,
    root: Path,
    work_id: str,
) -> tuple[RawSnapshot, ImportPlan]:
    """为单一追更目录保存独立的快照与计划，供之后安全增量扫描。"""
    token = re.sub(r"[^a-zA-Z0-9]", "", work_id)[-12:] or "work"
    snapshot_id = f"{snapshot.snapshot_id}_tracking_{token}"
    plan_id = f"{plan.plan_id}_tracking_{token}"
    scoped_files = [
        replace(
            file,
            snapshot_id=snapshot_id,
            source_root=str(root),
            relative_path=_tracking_relative_path(file.real_path, root, file.relative_path),
        )
        for file in snapshot.files
        if _path_is_within(file.real_path, root)
    ]
    scoped_snapshot = replace(
        snapshot,
        snapshot_id=snapshot_id,
        source_root=str(root),
        input_file=str(root),
        file_count=len(scoped_files),
        video_count=sum(1 for file in scoped_files if file.resource_hint == "video"),
        files=scoped_files,
    )
    scoped_plan = deepcopy(plan)
    scoped_plan.plan_id = plan_id
    scoped_plan.source_snapshot_id = snapshot_id
    scoped_plan.items = [
        replace(item, plan_id=plan_id)
        for item in plan.items
        if item.resource_type == "video" and _path_is_within(item.real_path, root)
    ]
    scoped_plan.summary = dict(scoped_plan.summary)
    scoped_plan.summary["tracking_root"] = str(root)
    scoped_plan.summary["tracking_work_id"] = work_id
    save_raw_snapshot(scoped_snapshot, update_latest=False)
    save_import_plan(scoped_plan, update_latest=False)
    return scoped_snapshot, scoped_plan


def _baseline_matches_root(binding: TrackingBinding, root: Path) -> bool:
    """仅复用已经按当前作品目录切分的历史基线。"""
    if not binding.last_snapshot_id or not binding.baseline_plan_id:
        return False
    snapshot = load_raw_snapshot(binding.last_snapshot_id)
    if snapshot is None or not _same_path(Path(snapshot.source_root), root):
        return False
    from app.import_plan.store import load_import_plan
    baseline = load_import_plan(plan_id=binding.baseline_plan_id)
    if baseline is None:
        return False
    videos = [item for item in baseline.items if item.resource_type == "video"]
    return bool(videos) and all(_path_is_within(item.real_path, root) for item in videos)


def _path_is_within(path: str, root: Path) -> bool:
    try:
        return os.path.commonpath([
            os.path.normcase(os.path.normpath(str(path))),
            os.path.normcase(os.path.normpath(str(root))),
        ]) == os.path.normcase(os.path.normpath(str(root)))
    except ValueError:
        return False


def _work_root(items: list[ImportPlanItem]) -> Path | None:
    parents = [str(Path(item.real_path).parent) for item in items if item.real_path]
    if not parents:
        return None
    try:
        root = Path(os.path.commonpath(parents))
    except ValueError:
        return None
    if _SEASON_DIR.fullmatch(root.name.strip()):
        root = root.parent
    return root


def _root_groups(items: list[ImportPlanItem]) -> dict[Path, list[ImportPlanItem]]:
    groups: dict[Path, list[ImportPlanItem]] = defaultdict(list)
    for item in items:
        root = _work_root([item])
        if root is not None:
            groups[root].append(item)
    return groups


def _item_sort_key(item: ImportPlanItem) -> tuple:
    label = " ".join((item.work_title or item.series_group or "").split())
    is_copy = bool(re.search(r"\s*[（(]\d+[)）]\s*$", label))
    return (1 if is_copy else 0, os.path.normcase(item.real_path))


def _root_sort_key(root: Path, items: list[ImportPlanItem]) -> tuple:
    return (_item_sort_key(min(items, key=_item_sort_key)), os.path.normcase(str(root)))


def _legacy_binding_for_items(items: list[ImportPlanItem]) -> TrackingBinding | None:
    root = _work_root(items)
    if root is None:
        return None
    first = min(items, key=_item_sort_key)
    seasons = [item.season_number for item in items if item.season_number is not None and item.season_number > 0]
    return get_tracking_binding_by_identity(
        first.work_id,
        str(root),
        min(seasons) if seasons else None,
    )


def _same_path(left: Path, right: Path) -> bool:
    left_key = os.path.normcase(os.path.abspath(os.path.normpath(str(left))))
    right_key = os.path.normcase(os.path.abspath(os.path.normpath(str(right))))
    return left_key == right_key


def _tracking_relative_path(real_path: str, root: Path, fallback: str) -> str:
    """保存作品级基线时去掉目录树额外的上层分类前缀。"""
    try:
        return os.path.relpath(
            os.path.normpath(real_path),
            os.path.normpath(str(root.parent)),
        ).replace("\\", "/")
    except (OSError, ValueError):
        return fallback
