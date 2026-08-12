from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from collections.abc import Iterable
from pathlib import Path

from app.core.paths import configured_mount_source
from app.import_plan.diff import DiffItem, DiffResult
from app.import_plan.incremental import merge_incremental_plan
from app.import_plan.store import load_import_plan, save_import_plan
from app.raw.models import RawFile, RawSnapshot
from app.recognition.planner import build_draft_import_plan
from app.tracking.store import get_tracking_binding

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".rmvb", ".mov"}


def preview_manual_episodes(work_id: str, paths: Iterable[str], season_number: int | None = None) -> dict:
    binding = get_tracking_binding(work_id)
    if binding is None or not binding.baseline_plan_id:
        raise LookupError("当前作品没有可追加的基准导入计划")
    base = load_import_plan(plan_id=binding.baseline_plan_id)
    if base is None:
        raise LookupError("基准导入计划不存在")
    selected = _expand_video_paths(paths)
    if not selected:
        raise ValueError("至少选择一个视频文件")

    representative = next((item for item in base.items if item.resource_type == "video"), None)
    if representative is None:
        raise LookupError("基准导入计划没有可继承的剧集信息")

    snapshot = _snapshot_for_selected_files(
        selected,
        base.source,
        binding.display_title or binding.series_group or representative.work_title,
    )
    snapshot.import_family = representative.import_family or base.import_family or "anime"
    plan = build_draft_import_plan(snapshot)
    existing_keys = {
        (item.season_number, item.episode_number): item
        for item in base.items
        if item.resource_type == "video" and item.group_type == "season" and item.episode_number is not None
    }

    rows = []
    statuses = {}
    for item in plan.items:
        if item.resource_type != "video":
            continue
        episode_number = _extract_manual_episode_number(Path(item.real_path).name)
        item.canonical_work_id = work_id
        item.work_id = work_id
        item.work_title = binding.display_title or representative.work_title
        item.original_title = representative.original_title
        item.year = representative.year
        item.series_group = binding.series_group or representative.series_group or item.work_title
        item.media_type = "tv"
        item.show_type = representative.show_type or "anime_series"
        item.tmdb_hint_id = representative.tmdb_hint_id
        item.tmdb_hint_type = representative.tmdb_hint_type
        item.import_family = representative.import_family or base.import_family or "anime"
        item.card_type = representative.card_type or "main_series"
        item.belongs_to_series = representative.belongs_to_series
        item.relation_type = representative.relation_type or "main"
        item.group_type = "season"
        item.episode_number = episode_number
        item.special_number = None
        item.title = ""
        item.action = "generate_strm"
        if season_number is not None:
            item.season_number = season_number
        elif binding.season_number is not None:
            item.season_number = binding.season_number
        else:
            item.season_number = representative.season_number or 1
        item.confidence = "high" if episode_number is not None else "low"
        item.needs_review = episode_number is None
        item.reasons.append(
            f"仅按文件名识别为第 {episode_number} 集"
            if episode_number is not None
            else "文件名中没有明确集号"
        )

        key = (item.season_number, item.episode_number)
        if item.episode_number is None:
            status = "unrecognized"
        elif key in existing_keys:
            existing = existing_keys[key]
            if Path(existing.real_path) != Path(item.real_path):
                status = "conflict"
            elif _source_changed(existing, item):
                status = "replaced"
            else:
                status = "existing"
        else:
            status = "added"
        statuses[item.id] = status
        rows.append({
            "item_id": item.id, "path": item.real_path, "season_number": item.season_number,
            "episode_number": item.episode_number, "title": item.title, "status": status,
        })

    plan.summary.update({
        "plan_type": "manual_episode_preview", "target_work_id": work_id,
        "base_plan_id": base.plan_id, "manual_statuses": statuses,
    })
    save_import_plan(plan, update_latest=False)
    can_commit = (
        any(row["status"] in {"added", "replaced"} for row in rows)
        and all(row["status"] in {"added", "existing", "replaced"} for row in rows)
    )
    return {"plan_id": plan.plan_id, "work_id": work_id, "items": rows, "can_commit": can_commit}


def load_manual_episode_preview(plan_id: str):
    preview = load_import_plan(plan_id=plan_id)
    if preview is None or preview.summary.get("plan_type") != "manual_episode_preview":
        raise LookupError("追加剧集预览不存在")
    return preview


def validate_manual_episode_preview(plan_id: str):
    preview = load_manual_episode_preview(plan_id)
    statuses = preview.summary.get("manual_statuses") or {}
    unresolved = [item_id for item_id, status in statuses.items() if status in {"conflict", "unrecognized"}]
    if unresolved:
        raise ValueError("存在未解决的重复集或无法识别文件")
    if not any(status in {"added", "replaced"} for status in statuses.values()):
        raise ValueError("没有需要追加的新剧集")
    return preview


def build_manual_episode_commit(plan_id: str):
    preview = validate_manual_episode_preview(plan_id)
    statuses = preview.summary.get("manual_statuses") or {}
    base = load_import_plan(plan_id=preview.summary.get("base_plan_id"))
    if base is None:
        raise LookupError("基准导入计划不存在")
    delta = deepcopy(preview)
    delta.plan_id = hashlib.md5(f"manual-commit:{preview.plan_id}".encode("utf-8")).hexdigest()
    delta.items = [item for item in delta.items if statuses.get(item.id) in {"added", "replaced"}]
    for item in delta.items:
        item.plan_id = delta.plan_id
    diff = DiffResult(
        source=base.source,
        old_snapshot_id=base.source_snapshot_id,
        new_snapshot_id=delta.source_snapshot_id,
        items=[DiffItem(
            change_type=statuses.get(item.id, "added"), source=base.source, raw_file_id=item.raw_file_id,
            old_relative_path=item.relative_path if statuses.get(item.id) == "replaced" else "",
            new_relative_path=item.relative_path, new_real_path=item.real_path,
            resource_type="video",
        ) for item in delta.items],
        added_count=sum(1 for item in delta.items if statuses.get(item.id) == "added"),
        replaced_count=sum(1 for item in delta.items if statuses.get(item.id) == "replaced"),
    )
    cumulative = merge_incremental_plan(base, delta, diff, status="draft")
    save_import_plan(cumulative, update_latest=False)
    return cumulative


def commit_manual_episode_plan(
    work_id: str,
    plan_id: str,
    include_scrape: bool = True,
    progress_callback=None,
) -> dict:
    from dataclasses import replace

    from app.import_pipeline.service import run_auto_import_pipeline
    from app.library.service import refresh_tracking_library_work
    from app.raw.store import save_raw_snapshot
    from app.sources.local import LocalScanner
    from app.tracking.store import get_tracking_binding, upsert_tracking_binding

    plan = build_manual_episode_commit(plan_id)
    result = run_auto_import_pipeline(
        plan.source, plan.plan_id, include_scrape=include_scrape,
        progress_callback=progress_callback,
    )
    mirror = result.get("mirror") or {}
    mirror_succeeded = mirror.get("status") == "success"
    if result.get("status") != "succeeded" and not (
        result.get("stage") == "scrape" and mirror_succeeded
    ):
        return result
    binding = get_tracking_binding(work_id)
    if binding:
        snapshot_id = binding.last_snapshot_id
        if Path(binding.root_path).is_dir():
            snapshot = LocalScanner().scan(
                binding.root_path, source_root=binding.root_path,
                logical_source=binding.logical_source, include_root=True,
                metadata_only=bool(configured_mount_source(binding.root_path)),
            )
            snapshot.import_family = binding.import_family
            # 单部作品的人工追加只更新该追更绑定，不能覆盖整个来源的全量基线。
            save_raw_snapshot(snapshot, update_latest=False)
            snapshot_id = snapshot.snapshot_id
        upsert_tracking_binding(replace(
            binding, baseline_plan_id=plan.plan_id, last_snapshot_id=snapshot_id,
            attention_state="ready" if include_scrape else binding.attention_state,
        ))
    # 追加的首要结果是剧集与真实播放路径落库；元数据服务不可用时也必须
    # 立即刷新作品，让缺失缩略图走作品背景图兜底，不能把已写入的剧集判失败。
    refresh_tracking_library_work(plan, work_id)
    scrape = result.get("scrape") or {}
    metadata_degraded = (
        result.get("status") != "succeeded"
        or int(scrape.get("review_queued") or 0) > 0
    )
    if metadata_degraded:
        warning = str(result.get("error") or "新剧集资料暂时未补齐")
        result = {
            **result,
            "status": "succeeded",
            "stage": "done",
            "metadata_status": "degraded",
            "metadata_warning": warning,
        }
    return result


def _validate_video_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.exists() or not path.is_file():
        raise ValueError(f"视频文件不存在: {value}")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"不支持的视频格式: {path.suffix}")
    return path.resolve()


def _expand_video_paths(values: Iterable[str]) -> list[Path]:
    selected: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.exists():
            raise ValueError(f"文件或目录不存在: {value}")
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            resolved = candidate.resolve()
            key = str(resolved).casefold()
            if key not in seen:
                seen.add(key)
                selected.append(resolved)
    return sorted(selected, key=lambda item: str(item).casefold())


def _source_changed(existing, candidate) -> bool:
    if existing.source_size and candidate.source_size and existing.source_size != candidate.source_size:
        return True
    if existing.source_mtime and candidate.source_mtime:
        return abs(existing.source_mtime - candidate.source_mtime) > 0.001
    if existing.source_fingerprint and candidate.source_fingerprint:
        return existing.source_fingerprint != candidate.source_fingerprint
    return False


def _extract_manual_episode_number(filename: str) -> int | None:
    """只从文件名提取手动追加的正片集号，不读取任何父目录信息。"""
    stem = Path(filename).stem.strip()
    patterns = (
        r"(?i)(?:^|[^0-9A-Z])S\d{1,3}\s*E\s*0*(\d{1,4})(?=$|[^0-9])",
        r"第\s*0*(\d{1,4})\s*[集话話]",
        r"(?i)(?:^|[^0-9A-Z])EP\s*0*(\d{1,4})(?=$|[^0-9])",
        r"(?i)(?:^|[^0-9A-Z])E\s*0*(\d{1,4})(?=$|[^0-9])",
        r"[\[【(（]\s*0*(\d{1,4})\s*[\]】)）]",
    )
    for pattern in patterns:
        match = re.search(pattern, stem)
        if match:
            number = int(match.group(1))
            return number if number > 0 else None
    if re.fullmatch(r"0*\d{1,4}", stem):
        number = int(stem)
        return number if number > 0 else None
    return None


def _snapshot_for_selected_files(paths: list[Path], source: str, title: str) -> RawSnapshot:
    token = "|".join(str(path) for path in paths)
    snapshot_id = "manual_" + hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]
    files = []
    for path in paths:
        stat = path.stat()
        detected_source = configured_mount_source(path)
        file_source = detected_source or ("local" if source in {"pan115", "baidu"} else source)
        relative = f"{title or path.parent.name}/{path.name}"
        files.append(RawFile(
            id=hashlib.md5(f"{file_source}:{path}".encode("utf-8")).hexdigest(),
            snapshot_id=snapshot_id, source=file_source, source_root=str(path.parent),
            virtual_root=title or path.parent.name, source_path_parts=relative.split("/"),
            relative_path=relative, real_path=str(path), name=path.name, stem=path.stem,
            ext=path.suffix, depth=2, parent_path=title or path.parent.name,
            is_file=True, resource_hint="video", size=stat.st_size, mtime=stat.st_mtime,
        ))
    return RawSnapshot(
        snapshot_id=snapshot_id, source=source, source_root=str(paths[0].parent),
        file_count=len(files), video_count=len(files), files=files,
    )
