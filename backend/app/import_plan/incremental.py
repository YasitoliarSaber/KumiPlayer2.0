"""增量导入计划生成

基于 DiffResult.added 生成增量导入计划。
复用 M03/M04 资源类型和媒体识别。
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from app.import_plan.diff import DiffResult
from app.import_plan.models import ImportPlan
from app.raw.models import RawFile, RawSnapshot
from app.recognition.plan_recognizer import recognize_import_plan_media
from app.recognition.planner import build_draft_import_plan


def build_incremental_plan(
    diff_result: DiffResult,
    source: str,
    source_root: str,
    new_snapshot: RawSnapshot | None = None,
    allow_blocked: bool = False,
) -> ImportPlan | None:
    """基于 DiffResult 的 added 文件生成增量导入计划

    只处理 change_type=added 且 resource_type=video 的文件。
    如果 diff 被 blocked，返回 None。

    参数:
        diff_result: diff 结果
        source: 来源标识
        source_root: 来源根目录

    返回:
        ImportPlan 或 None（blocked 时）
    """
    # 被 blocked 时不生成增量计划
    if diff_result.safety.blocked and not allow_blocked:
        return None

    # 筛选 added 的视频文件
    added_items = [
        item for item in diff_result.items
        if item.change_type in {"added", "replaced", "moved", "renamed"}
        and item.resource_type == "video"
    ]

    if not added_items:
        return None

    # 构造 RawSnapshot 用于复用 M03/M04 识别流程
    snapshot_by_path = {
        item.relative_path: item for item in (new_snapshot.files if new_snapshot else [])
    }
    raw_files = []
    for item in added_items:
        existing = snapshot_by_path.get(item.new_relative_path)
        rf = existing or RawFile(
            id=item.raw_file_id,
            source=source,
            source_root=source_root,
            relative_path=item.new_relative_path,
            real_path=item.new_real_path,
            name=item.new_relative_path.split("/")[-1],
            ext="." + item.new_relative_path.split(".")[-1] if "." in item.new_relative_path else "",
            resource_hint="video",
            size=item.size,
        )
        raw_files.append(rf)

    snapshot = RawSnapshot(
        snapshot_id=f"incremental_{diff_result.diff_id}",
        source=source,
        source_root=source_root,
        import_family=new_snapshot.import_family if new_snapshot else "",
        import_scope=new_snapshot.import_scope if new_snapshot else "",
        created_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
        file_count=len(raw_files),
        video_count=len(raw_files),
        files=raw_files,
    )

    # 复用 M03 生成 draft plan
    plan = build_draft_import_plan(snapshot)

    # 复用 M04 媒体识别
    plan = recognize_import_plan_media(plan)

    # 标记为增量计划
    plan.source = source
    plan.summary["plan_type"] = "incremental"
    plan.summary["base_diff_id"] = diff_result.diff_id
    plan.summary["base_snapshot_id"] = diff_result.old_snapshot_id

    return plan


def merge_incremental_plan(
    base_plan: ImportPlan,
    delta_plan: ImportPlan,
    diff_result: DiffResult,
    status: str = "confirmed",
) -> ImportPlan:
    """将增量识别结果合并回累计计划，保留旧剧集和稳定条目 ID。"""
    merged = deepcopy(base_plan)
    merged.plan_id = delta_plan.plan_id
    by_path = {item.relative_path: item for item in merged.items}
    delta_by_path = {item.relative_path: item for item in delta_plan.items}

    for change in diff_result.items:
        old_item = by_path.get(change.old_relative_path)
        new_item = delta_by_path.get(change.new_relative_path)

        if change.change_type == "missing":
            if old_item:
                old_item.availability = "missing"
            continue

        if change.change_type == "unchanged":
            if old_item:
                old_item.availability = "available"
            continue

        if change.change_type in {"replaced", "moved", "renamed"} and old_item:
            old_path = old_item.relative_path
            if new_item:
                old_item.raw_file_id = new_item.raw_file_id
                old_item.relative_path = new_item.relative_path
                old_item.real_path = new_item.real_path
                old_item.source_size = new_item.source_size
                old_item.source_mtime = new_item.source_mtime
                old_item.source_fingerprint = new_item.source_fingerprint
            elif change.new_relative_path:
                old_item.relative_path = change.new_relative_path
                old_item.real_path = change.new_real_path or old_item.real_path
            old_item.availability = "available"
            if old_item.relative_path != old_path:
                by_path.pop(old_path, None)
                by_path[old_item.relative_path] = old_item
            continue

        if change.change_type == "added" and new_item:
            restored_item = next(
                (
                    item for item in merged.items
                    if item.availability == "missing"
                    and item.resource_type == "video"
                    and item.work_id == new_item.work_id
                    and item.group_type == new_item.group_type
                    and item.season_number == new_item.season_number
                    and item.episode_number == new_item.episode_number
                ),
                None,
            )
            if restored_item is not None:
                restored_item.raw_file_id = new_item.raw_file_id
                restored_item.relative_path = new_item.relative_path
                restored_item.real_path = new_item.real_path
                restored_item.source_size = new_item.source_size
                restored_item.source_mtime = new_item.source_mtime
                restored_item.source_fingerprint = new_item.source_fingerprint
                restored_item.availability = "available"
                by_path[restored_item.relative_path] = restored_item
                continue
            new_copy = deepcopy(new_item)
            new_copy.plan_id = merged.plan_id
            new_copy.availability = "available"
            merged.items.append(new_copy)
            by_path[new_copy.relative_path] = new_copy

    merged.source_snapshot_id = diff_result.new_snapshot_id or delta_plan.source_snapshot_id
    merged.import_family = base_plan.import_family or delta_plan.import_family
    merged.import_scope = base_plan.import_scope or delta_plan.import_scope
    merged.status = status
    merged.updated_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    for item in merged.items:
        item.plan_id = merged.plan_id
    merged.summary = {
        **(base_plan.summary or {}),
        "plan_type": "cumulative_incremental",
        "base_diff_id": diff_result.diff_id,
        "video_count": sum(1 for item in merged.items if item.resource_type == "video"),
        "available_video_count": sum(
            1 for item in merged.items
            if item.resource_type == "video" and item.availability == "available"
        ),
        "missing_video_count": sum(
            1 for item in merged.items
            if item.resource_type == "video" and item.availability != "available"
        ),
    }
    return merged
