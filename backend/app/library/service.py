# -*- coding: utf-8 -*-
"""媒体库服务函数"""

import os
import re
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import List, Optional

from app.import_plan.store import load_import_plan, load_latest_confirmed_import_plan
from app.library.index import (
    _library_work_id,
    _plan_library_work_id_resolver,
    build_library_index,
    rebuild_related_works_for_plan,
)
from app.library.models import LibraryIndex, WorkIndex
from app.library.scanner import MirrorAsset, MirrorFile, MirrorScanResult, scan_mirror
from app.library.store import load_library_index, save_library_index
from app.scrape.store import load_scrape_map
from app.tracking.store import list_tracking_bindings

_LIBRARY_INDEX_LOCK = RLock()


def _serialized_library_index_operation(fn):
    """Serialize read-modify-write operations for the shared LibraryIndex."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _LIBRARY_INDEX_LOCK:
            return fn(*args, **kwargs)
    return wrapped


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


@_serialized_library_index_operation
def rescan_library(source: Optional[str] = None) -> dict:
    """重建 LibraryIndex

    返回 result dict。
    """
    warnings = []
    existing_index = load_library_index()

    # 加载 ImportPlan
    if source:
        plans = [load_latest_confirmed_import_plan(source)]
        plans = [p for p in plans if p is not None]
    else:
        # 扫描所有已知来源，后续新增来源时只需要扩展这里或改成读取 source registry。
        plans = [
            p for p in (
                load_latest_confirmed_import_plan("pan115"),
                load_latest_confirmed_import_plan("baidu"),
                load_latest_confirmed_import_plan("local"),
                load_latest_confirmed_import_plan("openlist"),
            )
            if p is not None
        ]

    if not plans:
        return {
            "index_path": "",
            "work_count": 0,
            "episode_count": 0,
            "missing_strm_count": 0,
            "warnings": ["未找到 confirmed ImportPlan"],
        }

    # 当前计划明确标记为已完结时，清理同批作品早期误建的追更绑定。
    # import_scope 是分类主真相，历史 tracking 控制记录不能反向覆盖它。
    from app.tracking.registration import reconcile_tracking_bindings_for_plan
    for plan in plans:
        reconcile_tracking_bindings_for_plan(plan)

    # 加载 ScrapeMap
    scrape_map = load_scrape_map()

    # 扫描 mirror
    scan_result = scan_mirror(source=source)

    # 构建索引
    indexes = [
        build_library_index(plan, scrape_map, scan_result)
        for plan in plans
    ]
    index = _merge_library_indexes(indexes)
    if source:
        index = _replace_source_in_existing_index(source, index)
    elif existing_index is not None:
        index = _preserve_existing_seasonal_works(index, existing_index)

    # 保存
    index_path = save_library_index(index)

    # 普通重扫不扫描新番更新，只从已保存的追更基线恢复作品卡片数量。
    tracking_bindings = [
        binding for binding in list_tracking_bindings("tracking")
        if source is None or binding.logical_source == source
    ]
    tracking_repair = rebuild_tracking_library_from_bindings(tracking_bindings) if tracking_bindings else {
        "restored": 0,
        "warnings": [],
    }
    warnings.extend(tracking_repair.get("warnings", []))
    if tracking_repair.get("restored"):
        index = load_library_index() or index

    # 统计
    episode_count = sum(len(w.episodes) for w in index.works)
    missing = sum(
        summary.get("missing_strm_count", 0)
        for summary in index.source_summary.values()
    )

    return {
        "index_path": index_path,
        "work_count": len(index.works),
        "episode_count": episode_count,
        "seasonal_work_count": sum(work.import_scope == "seasonal" for work in index.works),
        "missing_strm_count": missing,
        "warnings": warnings,
    }


def _merge_library_indexes(indexes: List[LibraryIndex]) -> LibraryIndex:
    """合并多个来源，并把同一剧集作品的零散集数收进同一张卡。"""
    works = []
    source_summary = {}
    for index in indexes:
        works.extend(index.works)
        source_summary.update(index.source_summary)

    return LibraryIndex(
        version=min((index.version for index in indexes), default=2),
        works=_deduplicate_library_works(works),
        source_summary=source_summary,
        generated_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
    )


def _replace_source_in_existing_index(source: str, source_index: LibraryIndex) -> LibraryIndex:
    """Replace one source in the persisted global LibraryIndex.

    Source-scoped rescans happen after deleting one work or scraping one source.
    They must not wipe other sources from the global library cache.
    """
    existing = load_library_index()
    if existing is None:
        return source_index

    source_index = _preserve_existing_seasonal_works(source_index, existing, source)

    works = []
    for work in existing.works:
        replacement = _without_source_contribution(work, source)
        if replacement is not None:
            works.append(replacement)
    works.extend(source_index.works)

    source_summary = dict(existing.source_summary)
    source_summary.pop(source, None)
    source_summary.update(source_index.source_summary)

    return LibraryIndex(
        version=min(existing.version, source_index.version),
        works=_deduplicate_library_works(works),
        source_summary=source_summary,
        generated_at=source_index.generated_at,
    )


def _without_source_contribution(work: WorkIndex, source: str) -> Optional[WorkIndex]:
    """Remove one source from a previously cross-source merged cache card."""
    work_sources = set(work.sources or [work.source])
    episode_sources = {episode.source or work.source for episode in work.episodes}
    if source not in work_sources and source not in episode_sources:
        return work

    kept_episodes = [
        deepcopy(episode)
        for episode in work.episodes
        if (episode.source or work.source) != source
    ]
    remaining_locations = {
        key: deepcopy(value)
        for key, value in work.source_locations.items()
        if key != source
    }
    remaining_sources = _ordered_sources(
        list((work_sources | {episode.source for episode in kept_episodes if episode.source}) - {source})
        + list(remaining_locations)
    )
    if not kept_episodes and not remaining_locations:
        return None

    replacement = deepcopy(work)
    replacement.episodes = kept_episodes
    replacement.sources = remaining_sources
    replacement.source_locations = remaining_locations
    replacement.source_episode_counts = {
        key: value
        for key, value in _work_source_episode_counts(work).items()
        if key != source
    }
    if replacement.source == source and remaining_sources:
        replacement.source = remaining_sources[0]

    retained_seasons = []
    for season in replacement.seasons:
        expected_group = "special" if season.group_type in {"special", "sps"} else "season"
        episode_count = sum(
            episode.season_number == season.season_number
            and episode.group_type == expected_group
            for episode in kept_episodes
        )
        if episode_count:
            season.episode_count = episode_count
            retained_seasons.append(season)
    replacement.seasons = retained_seasons
    return replacement


def _preserve_existing_seasonal_works(
    rebuilt: LibraryIndex,
    existing: LibraryIndex,
    source: Optional[str] = None,
) -> LibraryIndex:
    """普通来源重扫只核对新番作品数，不覆盖新番的剧集更新状态。"""
    rebuilt_keys = {(work.source, work.work_id) for work in rebuilt.works}
    preserved = [
        work for work in existing.works
        if work.import_scope == "seasonal" and (source is None or work.source == source)
        and (work.source, work.work_id) not in rebuilt_keys
    ]
    if not preserved:
        return rebuilt

    rebuilt.works.extend(preserved)
    rebuilt.works = _deduplicate_library_works(rebuilt.works)
    for preserved_source in {
        source_name
        for work in preserved
        for source_name in _work_sources(work)
    }:
        rebuilt.source_summary = _refresh_source_summary_from_works(
            rebuilt.source_summary,
            preserved_source,
            rebuilt.works,
        )
    return rebuilt


@_serialized_library_index_operation
def refresh_tracking_library_work(plan, work_id: str) -> dict:
    """以当前追更计划局部替换一个作品，避免 source 级重扫覆盖同来源其他作品。"""
    current = load_library_index()
    if current is None:
        return {"mode": "skipped", "warnings": ["媒体库索引不存在，未执行追更局部替换"]}

    scoped_plan = deepcopy(plan)
    scoped_plan.import_scope = "seasonal"
    source_index = build_library_index(scoped_plan, load_scrape_map(), scan_mirror(source=scoped_plan.source))
    replacements = [work for work in source_index.works if work.work_id == work_id]
    if not replacements:
        return {"mode": "skipped", "warnings": [f"{work_id}: 未构建到可替换的追更作品，保留原索引"]}
    _replace_library_works(current, scoped_plan.source, {work_id}, replacements)
    save_library_index(current)
    return {"mode": "tracking_work", "work_count": len(replacements), "warnings": []}


@_serialized_library_index_operation
def rebuild_tracking_library_from_bindings(bindings: list) -> dict:
    """从所有追更基线计划恢复新番卡片；只替换成功构建出的绑定作品。"""
    current = load_library_index()
    if current is None:
        return {"mode": "skipped", "restored": 0, "warnings": ["媒体库索引不存在"]}

    grouped: dict[str, dict] = {}
    warnings: list[str] = []
    for binding in bindings:
        plan = load_import_plan(plan_id=getattr(binding, "baseline_plan_id", ""))
        if plan is None:
            warnings.append(f"{getattr(binding, 'work_id', '')}: 缺少追更基线计划")
            continue
        work_id = getattr(binding, "work_id", "")
        items = [
            deepcopy(item) for item in plan.items
            if item.resource_type == "video" and (item.work_id == work_id or item.canonical_work_id == work_id)
        ]
        if not items:
            warnings.append(f"{work_id}: 基线计划未包含该作品，已保留现有索引")
            continue
        group = grouped.setdefault(
            plan.source,
            {"template": deepcopy(plan), "items": [], "work_ids": set(), "keys": set()},
        )
        group["work_ids"].add(work_id)
        for item in items:
            key = (work_id, item.group_type, item.season_number, item.episode_number, item.relative_path)
            if key not in group["keys"]:
                group["keys"].add(key)
                group["items"].append(item)

    restored = 0
    for source, group in grouped.items():
        plan = group["template"]
        plan.items = group["items"]
        plan.import_scope = "seasonal"
        source_index = build_library_index(plan, load_scrape_map(), scan_mirror(source=source))
        replacements = [work for work in source_index.works if work.work_id in group["work_ids"]]
        if not replacements:
            warnings.append(f"{source}: 未构建到追更作品，未覆盖原索引")
            continue
        _replace_library_works(current, source, {work.work_id for work in replacements}, replacements)
        restored += len(replacements)

    if restored:
        save_library_index(current)
    return {"mode": "tracking_rebuild", "restored": restored, "warnings": warnings}


def _replace_library_works(
    current: LibraryIndex,
    source: str,
    work_ids: set[str],
    replacements: list[WorkIndex],
) -> None:
    old_work_map = {
        work.work_id: work
        for work in current.works
        if work.source == source and work.work_id in work_ids
    }
    for work in replacements:
        old = old_work_map.get(work.work_id)
        if old:
            work.last_played = old.last_played
            if old.related_works and not work.related_works:
                work.related_works = old.related_works
    replacement_identities = {
        identity for work in replacements
        if (identity := _seasonal_work_identity(work)) is not None
    }
    retained_works = []
    for work in current.works:
        if work.source == source and work.work_id in work_ids:
            continue
        if _seasonal_work_identity(work) in replacement_identities:
            retained = _without_source_contribution(work, source)
            if retained is not None:
                retained_works.append(retained)
            continue
        retained_works.append(work)
    current.works = retained_works
    current.works.extend(replacements)
    current.works = _deduplicate_library_works(current.works)
    current.source_summary = _refresh_source_summary_from_works(
        current.source_summary,
        source,
        current.works,
    )
    current.generated_at = _now_iso()


@_serialized_library_index_operation
def publish_import_plan_to_library(plan) -> dict:
    """镜像完成后立即局部发布当前计划，不依赖后续刮削或来源级 latest 计划。"""
    scan_result = _scan_plan_items_directly(plan, [])
    source_index = build_library_index(plan, load_scrape_map(), scan_result)
    if not source_index.works:
        return {
            "mode": "skipped_no_displayable_work",
            "index_path": "",
            "work_count": 0,
            "warnings": ["镜像计划缺少可发布的作品身份，已保留现有媒体库索引"],
        }

    current = load_library_index() or LibraryIndex()
    raw_work_ids = {
        _library_work_id(item)
        for item in plan.items
        if item.resource_type == "video" and item.action == "generate_strm"
    }
    replacement_ids = raw_work_ids | {work.work_id for work in source_index.works}
    target_paths = {
        _normalize_path(item.target_strm_path)
        for item in plan.items
        if item.target_strm_path
    }
    current.works = [
        work
        for work in current.works
        if not (
            work.source == plan.source
            and (
                work.work_id in replacement_ids
                or any(_normalize_path(episode.strm_path) in target_paths for episode in work.episodes)
            )
        )
    ]
    current.works.extend(source_index.works)
    current.works = _deduplicate_library_works(current.works)
    rebuild_related_works_for_plan(
        [work for work in current.works if work.source == plan.source],
        plan,
    )
    current.source_summary = _refresh_source_summary_from_works(
        current.source_summary,
        plan.source,
        current.works,
    )
    current.generated_at = _now_iso()
    index_path = save_library_index(current)
    return {
        "mode": "plan_publish",
        "index_path": index_path,
        "work_count": len(source_index.works),
        "warnings": list(source_index.source_summary.get(plan.source, {}).get("warnings", [])),
    }


@_serialized_library_index_operation
def refresh_library_for_scrape_targets(targets: list, library_work_id: str = "") -> dict:
    """After scraping, refresh only affected LibraryIndex works.

    Scraping changes NFO files, posters, fanart, logos, and scrape_map entries.
    It does not change the full mirror tree, so a full scan is unnecessary for
    normal scrape completion.  This path never falls back to a source/full
    rescan: callers must request those expensive operations explicitly.
    """
    clean_targets = [t for t in targets if t is not None]
    if not clean_targets:
        return {"mode": "noop", "work_count": 0, "warnings": ["未提供刮削目标"]}
    warnings = []
    current = load_library_index()
    if current is None:
        if library_work_id:
            raise RuntimeError("当前作品卡片索引不存在，拒绝生成新卡片")
        raise RuntimeError("媒体库索引不存在，拒绝在刮削流程中回退全量重扫")

    scrape_map = load_scrape_map()
    refreshed_total = 0
    anchored_work = None
    if library_work_id:
        anchored_work = next((work for work in current.works if work.work_id == library_work_id), None)
        if anchored_work is None:
            raise RuntimeError("当前作品卡片已不存在，拒绝生成新卡片")

    targets_by_plan: dict[tuple[str, str], list] = {}
    for target in clean_targets:
        source = getattr(target, "source", "")
        if source:
            plan_id = getattr(target, "import_plan_id", "") or ""
            targets_by_plan.setdefault((source, plan_id), []).append(target)

    for (source, plan_id), source_targets in targets_by_plan.items():
        plan = load_import_plan(plan_id=plan_id) if plan_id else None
        if plan is not None and (plan.source != source or plan.status not in {"confirmed", "executed"}):
            warnings.append(f"{source}: 目标计划 {plan_id} 状态或来源不匹配，回退来源基线")
            plan = None
        if plan is None:
            plan = load_latest_confirmed_import_plan(source)
        if plan is None:
            operation = "原位手动刮削" if library_work_id else "刮削流程"
            raise RuntimeError(
                f"{source}: {operation}找不到可用 ImportPlan，拒绝全量重扫"
            )

        affected_work_ids = _affected_library_work_ids(plan, source_targets)
        if not affected_work_ids:
            raise RuntimeError(
                f"{source}: 无法定位受影响作品，拒绝在刮削流程中回退全量重扫"
            )

        library_work_id_resolver = _plan_library_work_id_resolver(plan)
        subset_items = [
            item for item in plan.items
            if library_work_id_resolver(item) in affected_work_ids
        ]
        if not subset_items:
            raise RuntimeError(
                f"{source}: 受影响作品没有计划条目，拒绝在刮削流程中回退全量重扫"
            )

        subset_plan = replace(plan, items=subset_items)
        scan_result = _scan_plan_items_directly(subset_plan, source_targets)
        source_index = build_library_index(subset_plan, scrape_map, scan_result)
        if not source_index.works:
            raise RuntimeError(
                f"{source}: 媒体库索引刷新失败，没有生成任何可展示作品。"
                "请检查镜像 .strm 是否存在，以及其中的真实视频路径是否可达"
            )
        if library_work_id:
            if len(source_index.works) != 1:
                raise RuntimeError(
                    f"{source}: 当前季度重建得到 {len(source_index.works)} 张卡片，"
                    "无法安全原位替换，已拒绝写入"
                )
            _rekey_library_work(source_index.works[0], library_work_id)
        legacy_ids_by_library: dict[str, set[str]] = {}
        for item in subset_items:
            library_id = library_work_id_resolver(item)
            aliases = legacy_ids_by_library.setdefault(library_id, set())
            if item.work_id:
                aliases.add(item.work_id)
            if item.canonical_work_id:
                aliases.add(item.canonical_work_id)
        replacement_ids = {work.work_id for work in source_index.works}
        legacy_work_ids = {
            work_id
            for aliases in legacy_ids_by_library.values()
            for work_id in aliases
        }
        replacement_work_ids = affected_work_ids | replacement_ids | legacy_work_ids
        if library_work_id:
            replacement_work_ids.add(library_work_id)
        old_work_map = {
            work.work_id: work
            for work in current.works
            if work.work_id in replacement_work_ids and (work.source == source or work.work_id == library_work_id)
        }

        refreshed_works = []
        for work in source_index.works:
            old = old_work_map.get(work.work_id)
            if old is None:
                old = next(
                    (
                        old_work_map[legacy_id]
                        for legacy_id in legacy_ids_by_library.get(work.work_id, set())
                        if legacy_id in old_work_map
                    ),
                    None,
                )
            if old:
                work.last_played = old.last_played
                if old.related_works and not work.related_works:
                    work.related_works = old.related_works
            refreshed_works.append(work)

        if library_work_id and anchored_work is not None:
            retained = _without_source_contribution(anchored_work, source)
            if retained is not None:
                refreshed_works[0] = _merge_manual_replacement_work(refreshed_works[0], retained)
                _rekey_library_work(refreshed_works[0], library_work_id)

        current.works = [
            work for work in current.works
            if not (
                work.work_id in replacement_work_ids
                and (work.source == source or work.work_id == library_work_id)
            )
        ]
        current.works.extend(refreshed_works)
        current.works = _deduplicate_library_works(current.works)
        # 局部构建只包含受影响作品，无法单独看见同系列的其他卡片；
        # 用完整来源计划恢复文件夹/显式系列关系，避免刮削后关联作品消失。
        rebuild_related_works_for_plan(
            [work for work in current.works if work.source == source],
            plan,
        )
        from app.library.deleted_works import refresh_source_summary

        refresh_source_summary(current)
        current.generated_at = _now_iso()
        refreshed_total += len(refreshed_works)

    index_path = save_library_index(current)
    return {
        "mode": "partial",
        "index_path": index_path,
        "work_count": refreshed_total,
        "warnings": warnings,
    }


def refresh_library_for_scrape_target(target, library_work_id: str = "") -> dict:
    """Refresh LibraryIndex for one scrape target."""
    return refresh_library_for_scrape_targets([target], library_work_id=library_work_id)


def _rekey_library_work(work: WorkIndex, work_id: str) -> None:
    """把重建结果绑定回详情页原卡片，连同内部季/集引用一起更新。"""
    work.work_id = work_id
    for season in work.seasons:
        season.work_id = work_id
    for episode in work.episodes:
        episode.work_id = work_id


def _affected_library_work_ids(plan, targets: list) -> set[str]:
    ids: set[str] = set()
    library_work_id = _plan_library_work_id_resolver(plan)
    item_ids = {
        item_id
        for target in targets
        for item_id in (getattr(target, "item_ids", None) or [])
    }
    target_work_ids = {getattr(target, "work_id", "") for target in targets if getattr(target, "work_id", "")}
    target_dirs = {
        _normalize_path(getattr(target, "target_dir", ""))
        for target in targets
        if getattr(target, "target_dir", "")
    }

    for item in plan.items:
        if item_ids and item.id in item_ids:
            ids.add(library_work_id(item))
            continue
        if item.work_id and item.work_id in target_work_ids:
            ids.add(library_work_id(item))
            continue
        if target_dirs and item.target_dir:
            item_dir = _normalize_path(item.target_dir)
            if item_dir in target_dirs:
                ids.add(library_work_id(item))

    if ids:
        return ids

    for target in targets:
        series_group = getattr(target, "series_group", "")
        card_type = getattr(target, "card_type", "")
        if card_type != "main_series" or not series_group:
            continue
        for item in plan.items:
            if item.card_type == "main_series" and item.series_group == series_group:
                ids.add(library_work_id(item))
    return ids


_DIRECT_ASSET_KINDS = {
    "tvshow.nfo": "tvshow_nfo",
    "movie.nfo": "movie_nfo",
    "poster.jpg": "poster",
    "poster.png": "poster",
    "poster.webp": "poster",
    "fanart.jpg": "fanart",
    "fanart.png": "fanart",
    "fanart.webp": "fanart",
    "clearlogo.png": "clearlogo",
    "clearlogo.svg": "clearlogo",
    "clearlogo.webp": "clearlogo",
}


def _scan_plan_items_directly(plan, targets: list) -> MirrorScanResult:
    from app.core.paths import get_mirror_root

    mirror_root = get_mirror_root()
    result = MirrorScanResult(
        source=plan.source,
        mirror_root=str(mirror_root),
        scanned_at=_now_iso(),
    )
    asset_dirs: set[Path] = set()

    for item in plan.items:
        if item.target_strm_path:
            path = Path(item.target_strm_path)
            if path.exists() and path.is_file():
                try:
                    relative = str(path.relative_to(mirror_root))
                except ValueError:
                    relative = str(path)
                namespace = relative.replace("\\", "/").split("/", 1)[0] if relative else ""
                try:
                    real_path = path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeDecodeError):
                    real_path = ""
                    result.warnings.append(f"无法读取 .strm: {path}")
                stat = path.stat()
                result.strm_files.append(MirrorFile(
                    source=plan.source,
                    namespace=namespace,
                    strm_path=str(path),
                    relative_strm_path=relative,
                    real_path=real_path,
                    exists=True,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                ))
            asset_dirs.add(path.parent)
        if item.target_dir:
            directory = Path(item.target_dir)
            asset_dirs.add(directory)
            asset_dirs.add(directory.parent)

    for target in targets:
        target_dir = getattr(target, "target_dir", "")
        if target_dir:
            directory = Path(target_dir)
            asset_dirs.add(directory)
            asset_dirs.add(directory.parent)

    for directory in asset_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for name, kind in _DIRECT_ASSET_KINDS.items():
            path = directory / name
            if path.exists() and path.is_file():
                result.assets.append(MirrorAsset(path=str(path), kind=kind, exists=True))

    return result


def _refresh_source_summary_from_works(source_summary: dict, source: str, works: list[WorkIndex]) -> dict:
    updated = dict(source_summary or {})
    previous = dict(updated.get(source, {}))
    source_works = [work for work in works if source in _work_sources(work)]
    previous.update({
        "work_count": len(source_works),
        "episode_count": sum(_work_source_episode_counts(work).get(source, 0) for work in source_works),
        "poster_count": sum(1 for work in source_works if work.poster_path),
        "fanart_count": sum(1 for work in source_works if work.fanart_path),
        "clearlogo_count": sum(1 for work in source_works if work.clearlogo_path),
        "scraped_work_count": sum(
            1 for work in source_works if work.metadata_state == "ready"
        ),
    })
    previous.setdefault("warnings", [])
    updated[source] = previous
    return updated


def _deduplicate_library_works(works: list[WorkIndex]) -> list[WorkIndex]:
    """Deduplicate directory cards and merge only strongly matched works."""
    selected: dict[tuple[str, ...], WorkIndex] = {}
    order: list[tuple[str, ...]] = []
    for work in works:
        mirror_series_key = _mirror_series_work_identity(work)
        key = _seasonal_work_identity(work) or mirror_series_key or ("directory", work.source, work.work_id)
        previous = selected.get(key)
        if previous is None:
            selected[key] = deepcopy(work)
            order.append(key)
            continue
        if work.import_scope == "seasonal" and previous.import_scope == "seasonal":
            selected[key] = _merge_seasonal_works(previous, work)
        elif mirror_series_key is not None:
            selected[key] = _merge_mirror_series_works(previous, work)
        elif _library_work_completeness(work) >= _library_work_completeness(previous):
            if previous.last_played and not work.last_played:
                work.last_played = previous.last_played
            selected[key] = deepcopy(work)
    normalized = [selected[key] for key in order]
    for work in normalized:
        _normalize_work_sources(work)
    return normalized


_SOURCE_PRIORITY = {"pan115": 0, "baidu": 1, "local": 2, "openlist": 3}


def _mirror_series_work_identity(work: WorkIndex) -> tuple[str, ...] | None:
    if (
        work.source == "local"
        or work.card_type != "main_series"
        or work.media_type not in {"", "tv"}
        or not work.dir_path
    ):
        return None
    directory = Path(work.dir_path)
    if not re.fullmatch(r"(?:Season\s*\d+|S\d+|SPs)", directory.name, flags=re.IGNORECASE):
        return None
    return ("mirror-series", work.source, str(directory.parent).casefold())


def _seasonal_work_identity(work: WorkIndex) -> Optional[tuple[str, ...]]:
    if work.import_scope != "seasonal":
        return None
    tmdb_keys = sorted({
        (season.tmdb_type or work.media_type or "tv", str(season.tmdb_id))
        for season in work.seasons
        if season.tmdb_id
    })
    if tmdb_keys:
        return ("seasonal", "tmdb", *tmdb_keys[0])
    if work.work_id:
        return ("seasonal", "canonical", work.work_id)
    return None


def _merge_seasonal_works(left: WorkIndex, right: WorkIndex) -> WorkIndex:
    candidates = [deepcopy(left), deepcopy(right)]
    candidates.sort(key=lambda work: (_SOURCE_PRIORITY.get(work.source, 99), work.work_id))
    merged = candidates[0]
    other = candidates[1]

    for field in ("title", "original_title", "plot", "poster_path", "fanart_path", "clearlogo_path", "dir_path"):
        if not getattr(merged, field) and getattr(other, field):
            setattr(merged, field, getattr(other, field))
    for field in ("genres", "studios", "cast", "tags", "related_works"):
        if not getattr(merged, field) and getattr(other, field):
            setattr(merged, field, deepcopy(getattr(other, field)))
    merged.rating = merged.rating or other.rating
    merged.last_played = max(filter(None, (merged.last_played, other.last_played)), default=None)
    merged.sources = _ordered_sources(
        (merged.sources or [merged.source]) + (other.sources or [other.source])
        + [episode.source for episode in merged.episodes + other.episodes if episode.source]
    )
    merged.source_locations = _merge_source_locations(candidates)
    merged.source_episode_counts = _merge_source_episode_counts(candidates)
    merged.episodes = _merge_seasonal_episodes(merged.episodes + deepcopy(other.episodes), merged.work_id)
    merged.seasons = _merge_seasonal_seasons(merged.seasons + deepcopy(other.seasons), merged.episodes)
    merged.metadata_state = _merged_metadata_state(
        merged.metadata_state,
        other.metadata_state,
    )
    for season in merged.seasons:
        season.work_id = merged.work_id
    return merged


def _merge_mirror_series_works(left: WorkIndex, right: WorkIndex) -> WorkIndex:
    candidates = [left, right]
    primary = min(candidates, key=lambda work: (
        not any(
            season.group_type == "season" and season.season_number > 0
            for season in work.seasons
        ),
        work.work_id,
    ))
    merged = _merge_seasonal_works(left, right)
    _rekey_library_work(merged, primary.work_id)
    for field in (
        "title", "original_title", "plot", "poster_path", "fanart_path",
        "clearlogo_path", "dir_path",
    ):
        if getattr(primary, field):
            setattr(merged, field, deepcopy(getattr(primary, field)))
    merged.rating = primary.rating or merged.rating
    return merged


def _merged_metadata_state(left: str, right: str) -> str:
    states = {left or "waiting_metadata", right or "waiting_metadata"}
    if "ready" in states:
        return "ready"
    if "waiting_review" in states:
        return "waiting_review"
    if "source_unavailable" in states:
        return "source_unavailable"
    return "waiting_metadata"


def _merge_manual_replacement_work(refreshed: WorkIndex, retained: WorkIndex) -> WorkIndex:
    """以本次刮削元数据为权威，只从旧卡补回其他来源的结构性贡献。"""
    merged = deepcopy(refreshed)
    merged.sources = _ordered_sources(
        (refreshed.sources or [refreshed.source])
        + (retained.sources or [retained.source])
    )
    if merged.sources:
        merged.source = merged.sources[0]
    merged.source_locations = _merge_source_locations([retained, refreshed])
    merged.source_episode_counts = _merge_source_episode_counts([retained, refreshed])
    merged.episodes = _merge_seasonal_episodes(
        deepcopy(retained.episodes) + deepcopy(refreshed.episodes),
        merged.work_id,
    )
    refreshed_season_keys = {
        (season.group_type, season.season_number)
        for season in refreshed.seasons
    }
    retained_extra_seasons = [
        deepcopy(season)
        for season in retained.seasons
        if (season.group_type, season.season_number) not in refreshed_season_keys
    ]
    merged.seasons = _merge_seasonal_seasons(
        deepcopy(refreshed.seasons) + retained_extra_seasons,
        merged.episodes,
    )
    merged.last_played = max(
        filter(None, (refreshed.last_played, retained.last_played)),
        default=None,
    )
    if not merged.related_works:
        merged.related_works = deepcopy(retained.related_works)
    if merged.tracking is None:
        merged.tracking = deepcopy(retained.tracking)
    return merged


def _merge_seasonal_episodes(episodes: list, work_id: str) -> list:
    selected = {}
    for episode in episodes:
        if episode.episode_number > 0:
            key = (episode.group_type, episode.season_number, episode.episode_number)
        else:
            key = (
                episode.group_type, episode.season_number, episode.episode_number,
                episode.kind, episode.title, episode.episode_id,
            )
        rank = (_SOURCE_PRIORITY.get(episode.source, 99), episode.episode_id)
        current = selected.get(key)
        if current is None or rank < current[0]:
            candidate = deepcopy(episode)
            candidate.work_id = work_id
            selected[key] = (rank, candidate)
    return sorted((item[1] for item in selected.values()), key=lambda episode: (
        episode.group_type not in {"season", "special"}, episode.season_number,
        episode.episode_number, episode.title, episode.episode_id,
    ))


def _merge_seasonal_seasons(seasons: list, episodes: list) -> list:
    selected = {}
    for season in seasons:
        key = (season.group_type, season.season_number)
        current = selected.get(key)
        if current is None or _season_completeness(season) > _season_completeness(current):
            selected[key] = deepcopy(season)
    for (group_type, season_number), season in selected.items():
        expected_group = "special" if group_type in {"special", "sps"} else "season"
        season.episode_count = sum(
            episode.season_number == season_number and episode.group_type == expected_group
            for episode in episodes
        )
    return sorted(
        selected.values(),
        key=lambda season: (season.group_type not in {"season", "special"}, season.season_number),
    )


def _season_completeness(season) -> tuple[int, int, int]:
    return (bool(season.scraped), bool(season.tmdb_id), season.episode_count)


def _normalize_work_sources(work: WorkIndex) -> None:
    work.sources = _ordered_sources(
        (work.sources or [work.source]) + [episode.source for episode in work.episodes if episode.source]
    )
    work.source_locations = _merge_source_locations([work])
    work.source_episode_counts = _merge_source_episode_counts([work])


def _work_source_episode_counts(work: WorkIndex) -> dict[str, int]:
    counts = {
        str(source): max(0, int(count or 0))
        for source, count in (work.source_episode_counts or {}).items()
        if source
    }
    visible: dict[str, int] = {}
    for episode in work.episodes:
        source = episode.source or work.source
        if source:
            visible[source] = visible.get(source, 0) + 1
    for source, count in visible.items():
        counts[source] = max(counts.get(source, 0), count)
    return counts


def _work_sources(work: WorkIndex) -> set[str]:
    return {
        source
        for source in (
            [work.source]
            + list(work.sources or [])
            + list((work.source_locations or {}).keys())
            + list(_work_source_episode_counts(work).keys())
            + [episode.source for episode in work.episodes]
        )
        if source
    }


def _merge_source_episode_counts(works: list[WorkIndex]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for work in works:
        for source, count in _work_source_episode_counts(work).items():
            merged[source] = max(merged.get(source, 0), count)
    return merged


def _merge_source_locations(works: list[WorkIndex]) -> dict[str, dict[str, str]]:
    selected: dict[str, tuple[tuple[str, str], dict[str, str]]] = {}
    for work in works:
        for source, location in (work.source_locations or {}).items():
            if not isinstance(location, dict):
                continue
            episode_id = str(location.get("episode_id") or "")
            strm_path = str(location.get("strm_path") or "")
            if episode_id and strm_path:
                _select_source_location(selected, source, episode_id, strm_path)
        for episode in work.episodes:
            if episode.source and episode.episode_id and episode.strm_path:
                _select_source_location(
                    selected,
                    episode.source,
                    episode.episode_id,
                    episode.strm_path,
                )
    return {
        source: selected[source][1]
        for source in _ordered_sources(list(selected))
    }


def _select_source_location(
    selected: dict[str, tuple[tuple[str, str], dict[str, str]]],
    source: str,
    episode_id: str,
    strm_path: str,
) -> None:
    rank = (episode_id, strm_path.casefold())
    current = selected.get(source)
    if current is None or rank < current[0]:
        selected[source] = (rank, {"episode_id": episode_id, "strm_path": strm_path})


def _source_episode_ids(work: WorkIndex) -> dict[str, str]:
    return {
        source: str(location.get("episode_id") or "")
        for source, location in (work.source_locations or {}).items()
        if isinstance(location, dict) and location.get("episode_id")
    }


def _ordered_sources(sources: list[str]) -> list[str]:
    return sorted(set(filter(None, sources)), key=lambda source: (_SOURCE_PRIORITY.get(source, 99), source))


def _library_work_completeness(work: WorkIndex) -> tuple[int, int, int]:
    return (
        len(work.episodes),
        sum(season.episode_count for season in work.seasons),
        len(work.seasons),
    )


def _normalize_path(path: str) -> str:
    if not path:
        return ""
    return os.path.abspath(path).replace("\\", "/")


@_serialized_library_index_operation
def get_library(
    source: Optional[str] = None,
    q: Optional[str] = None,
    media_type: Optional[str] = None,
    card_type: Optional[str] = None,
    compact: bool = False,
) -> dict:
    """获取媒体库

    返回 {works, summary, generated_at, needs_rescan}。
    """
    index = load_library_index()

    if index is None:
        return {
            "works": [],
            "summary": {"work_count": 0, "episode_count": 0, "source_summary": {}},
            "generated_at": "",
            "needs_rescan": True,
        }

    # Repair duplicate records left by older partial refreshes on the first
    # read, so every API consumer and later detail lookup sees the same card.
    works = _deduplicate_library_works(index.works)
    if len(works) != len(index.works):
        index.works = works
        for indexed_source in {
            source_name
            for work in works
            for source_name in _work_sources(work)
        }:
            index.source_summary = _refresh_source_summary_from_works(
                index.source_summary,
                indexed_source,
                works,
            )
        index.generated_at = _now_iso()
        save_library_index(index)

    visible_works = _visible_library_works(index, works)
    visible_source_summary = deepcopy(index.source_summary)
    for source_name in {
        *visible_source_summary.keys(),
        *(source_name for work in works for source_name in _work_sources(work)),
    }:
        visible_source_summary = _refresh_source_summary_from_works(
            visible_source_summary,
            source_name,
            visible_works,
        )
    works = visible_works

    # 筛选
    if source:
        works = [w for w in works if source in (w.sources or [w.source])]
    if q:
        q_lower = q.lower()
        works = [w for w in works if q_lower in (w.title or "").lower() or q_lower in (w.original_title or "").lower()]
    if media_type:
        works = [w for w in works if w.media_type == media_type]
    if card_type:
        works = [w for w in works if w.card_type == card_type]

    episode_count = sum(len(w.episodes) for w in works)

    return {
        "works": [_work_summary_to_dict(w) if compact else _work_to_dict(w) for w in works],
        "summary": {
            "work_count": len(works),
            "episode_count": episode_count,
            "source_summary": visible_source_summary,
        },
        "generated_at": index.generated_at,
        "needs_rescan": False,
    }


def _visible_library_works(index: LibraryIndex, works: list[WorkIndex]) -> list[WorkIndex]:
    exception_ids = _library_exception_work_ids()
    return [
        work for work in works
        if work.work_id in exception_ids
        or work.metadata_state in {"waiting_review", "source_unavailable"}
        or (
            work.metadata_state == "ready"
            and (index.version >= 2 or _legacy_work_has_complete_metadata(work))
        )
    ]


def _legacy_work_has_complete_metadata(work: WorkIndex) -> bool:
    """Validate v1 cache evidence without triggering a mirror or source rescan."""
    if not _asset_reference_available(work.poster_path):
        return False
    if not _asset_reference_available(work.fanart_path):
        return False
    primary_episodes = [
        episode for episode in work.episodes
        if episode.group_type in {"season", "special", "sps", "movie"}
    ]
    if not primary_episodes:
        return False
    if work.media_type != "movie" and any(
        episode.metadata_pending for episode in primary_episodes
    ):
        return False
    primary_seasons = [
        season for season in work.seasons
        if season.group_type in {"season", "special", "sps"}
    ]
    if work.media_type != "movie" and (
        not primary_seasons or not all(season.scraped for season in primary_seasons)
    ):
        return False
    return True


def _asset_reference_available(value: str) -> bool:
    # 列表读取只验证索引中是否存在引用；实际资源由资源接口按需检查。
    # 这里探测文件会让离线盘符阻塞整个媒体库请求。
    return bool(str(value or "").strip())


def _library_exception_work_ids() -> set[str]:
    """Map existing review/failure/tracking states back to LibraryIndex cards."""
    from app.scrape.models import ScrapeTarget
    from app.scrape.review_queue import load_review_queue
    from app.scrape.store import load_failed_cases
    from app.scrape.target_builder import build_scrape_targets

    ids: set[str] = set()
    try:
        for binding in list_tracking_bindings():
            scrape_result = (binding.last_result or {}).get("scrape") or {}
            failed_scrape = int(scrape_result.get("failed") or 0) > 0
            if binding.attention_state in {"waiting_review", "source_unavailable"} or failed_scrape:
                ids.add(binding.work_id)
    except Exception:
        pass

    plan_cache: dict[str, object] = {}
    target_cache: dict[str, dict[str, object]] = {}

    def load_plan_targets(plan_id: str):
        if not plan_id:
            return None, {}
        if plan_id not in plan_cache:
            plan_cache[plan_id] = load_import_plan(plan_id=plan_id)
        plan = plan_cache[plan_id]
        if plan is None:
            return None, {}
        if plan_id not in target_cache:
            target_cache[plan_id] = {
                target.scrape_target_id: target
                for target in build_scrape_targets(plan)
            }
        return plan, target_cache[plan_id]

    try:
        review_items = [
            item for item in load_review_queue().items
            if item.status == "pending"
        ]
    except Exception:
        review_items = []
    for item in review_items:
        plan, targets = load_plan_targets(item.import_plan_id)
        target = targets.get(item.scrape_target_id)
        if plan is not None and target is not None:
            ids.update(_affected_library_work_ids(plan, [target]))

    try:
        failed_cases = load_failed_cases()
    except Exception:
        failed_cases = []
    target_fields = set(ScrapeTarget.__dataclass_fields__)
    for case in failed_cases:
        target_data = case.get("target") or {}
        plan_id = str(target_data.get("import_plan_id") or "")
        target_id = str(
            case.get("scrape_target_id")
            or target_data.get("scrape_target_id")
            or ""
        )
        plan, targets = load_plan_targets(plan_id)
        target = targets.get(target_id)
        if target is None and target_data:
            target = ScrapeTarget(**{
                key: value for key, value in target_data.items()
                if key in target_fields
            })
        if plan is not None and target is not None:
            ids.update(_affected_library_work_ids(plan, [target]))
        elif target is not None and target.work_id:
            ids.add(target.work_id)
    return ids


def _summary_artwork_path(w: WorkIndex, field: str) -> str:
    work_path = getattr(w, field, "")
    if work_path:
        return work_path
    season_path = next(
        (path for season in w.seasons if (path := getattr(season, field, ""))),
        "",
    )
    if season_path:
        return season_path

    start = Path(w.dir_path) if w.dir_path else None
    if start is None:
        return ""
    roots = [start]
    name = start.name.casefold()
    if (
        name.startswith("season ")
        or name in {"sps", "sp", "special", "specials", "op-ed", "op_ed"}
        or (name.startswith("s") and name[1:].isdigit())
    ):
        roots.append(start.parent)
    filenames = {
        "poster_path": ("poster.jpg", "poster.png", "poster.webp"),
        "fanart_path": (
            "fanart.jpg", "fanart.png", "fanart.webp",
            "backdrop.jpg", "backdrop.png", "backdrop.webp",
        ),
        "clearlogo_path": (
            "clearlogo.png", "clearlogo.jpg", "clearlogo.webp",
            "logo.png", "logo.jpg", "logo.webp",
        ),
    }.get(field, ())
    for root in roots:
        for filename in filenames:
            candidate = root / filename
            if candidate.is_file():
                return str(candidate)
    return ""


def _local_summary_artwork_path(w: WorkIndex, field: str) -> str:
    """Return an existing local artwork path without replacing the canonical reference."""
    work_path = getattr(w, field, "")
    if work_path and not str(work_path).lower().startswith(("http://", "https://")):
        if Path(work_path).is_file():
            return work_path

    season_path = next(
        (
            path for season in w.seasons
            if (path := getattr(season, field, ""))
            and not str(path).lower().startswith(("http://", "https://"))
            and Path(path).is_file()
        ),
        "",
    )
    if season_path:
        return season_path

    start = Path(w.dir_path) if w.dir_path else None
    if start is None:
        return ""
    roots = [start]
    name = start.name.casefold()
    if (
        name.startswith("season ")
        or name in {"sps", "sp", "special", "specials", "op-ed", "op_ed"}
        or (name.startswith("s") and name[1:].isdigit())
    ):
        roots.append(start.parent)
    filenames = {
        "poster_path": ("poster.jpg", "poster.png", "poster.webp"),
        "fanart_path": (
            "fanart.jpg", "fanart.png", "fanart.webp",
            "backdrop.jpg", "backdrop.png", "backdrop.webp",
        ),
        "clearlogo_path": (
            "clearlogo.png", "clearlogo.jpg", "clearlogo.webp",
            "logo.png", "logo.jpg", "logo.webp",
        ),
    }.get(field, ())
    for root in roots:
        for filename in filenames:
            candidate = root / filename
            if candidate.is_file():
                return str(candidate)
    return ""


def _work_summary_to_dict(w: WorkIndex) -> dict:
    main_episode_count = sum(1 for e in w.episodes if e.group_type == "season")
    latest_episode_number = max(
        (e.episode_number for e in w.episodes if e.group_type == "season"),
        default=0,
    )
    total_episode_count = len(w.episodes)
    return {
        "work_id": w.work_id,
        "title": w.title,
        "original_title": w.original_title,
        "year": w.year,
        "rating": w.rating,
        "plot": w.plot,
        "genres": w.genres,
        "show_type": w.show_type,
        "media_type": w.media_type,
        "source": w.source,
        "sources": w.sources or [w.source],
        "provider_id": w.provider_id,
        "ingest_method": w.ingest_method,
        "source_route_id": w.source_route_id,
        "source_episode_ids": _source_episode_ids(w),
        "import_scope": w.import_scope,
        "card_type": w.card_type,
        "poster_path": _summary_artwork_path(w, "poster_path"),
        "fanart_path": _summary_artwork_path(w, "fanart_path"),
        "clearlogo_path": _summary_artwork_path(w, "clearlogo_path"),
        "local_poster_path": _local_summary_artwork_path(w, "poster_path"),
        "local_fanart_path": _local_summary_artwork_path(w, "fanart_path"),
        "local_clearlogo_path": _local_summary_artwork_path(w, "clearlogo_path"),
        "dir_path": w.dir_path,
        "seasons": [
            {"season_id": s.season_id, "season_number": s.season_number,
             "group_type": s.group_type, "label": s.label, "episode_count": s.episode_count,
             "work_id": s.work_id,
             "poster_path": s.poster_path,
             "fanart_path": s.fanart_path,
             "clearlogo_path": s.clearlogo_path,
             "rating": s.rating,
             "scraped": s.scraped}
            for s in w.seasons
        ],
        "episodes": [],
        "episode_count": total_episode_count,
        "main_episode_count": main_episode_count,
        "latest_episode_number": latest_episode_number,
        "related_works": [
            {"work_id": r.work_id, "title": r.title, "year": r.year,
             "card_type": r.card_type, "relation_type": r.relation_type,
             "poster_path": r.poster_path, "fanart_path": r.fanart_path,
             "show_type": r.show_type}
            for r in w.related_works
        ],
        "cast": w.cast,
        "tags": w.tags,
        "last_played": w.last_played,
        "tracking": w.tracking,
        "metadata_state": w.metadata_state,
        "certification": w.certification,
        "certification_country": w.certification_country,
        "artwork_provenance": w.artwork_provenance,
    }


def get_work_detail(work_id: str) -> Optional[dict]:
    """获取单个作品详情"""
    index = load_library_index()
    if index is None:
        return None

    for work in index.works:
        if work.work_id == work_id:
            return _work_to_dict(work)

    return None


def _work_to_dict(w: WorkIndex) -> dict:
    return {
        "work_id": w.work_id,
        "title": w.title,
        "original_title": w.original_title,
        "year": w.year,
        "rating": w.rating,
        "plot": w.plot,
        "genres": w.genres,
        "show_type": w.show_type,
        "media_type": w.media_type,
        "source": w.source,
        "sources": w.sources or [w.source],
        "provider_id": w.provider_id,
        "ingest_method": w.ingest_method,
        "source_route_id": w.source_route_id,
        "source_episode_ids": _source_episode_ids(w),
        "import_scope": w.import_scope,
        "card_type": w.card_type,
        "poster_path": w.poster_path,
        "fanart_path": w.fanart_path,
        "clearlogo_path": w.clearlogo_path,
        "dir_path": w.dir_path,
        "seasons": [
            {"season_id": s.season_id, "season_number": s.season_number,
             "group_type": s.group_type, "label": s.label, "episode_count": s.episode_count,
             "work_id": s.work_id,
             "scrape_target_id": s.scrape_target_id,
             "scrape_title": s.scrape_title,
             "scrape_year": s.scrape_year,
             "tmdb_id": s.tmdb_id,
             "tmdb_type": s.tmdb_type,
             "tmdb_season_number": s.tmdb_season_number,
             "nfo_path": s.nfo_path,
             "poster_path": s.poster_path,
             "fanart_path": s.fanart_path,
             "clearlogo_path": s.clearlogo_path,
             "plot": s.plot,
             "rating": s.rating,
             "scraped": s.scraped}
            for s in w.seasons
        ],
        "episodes": [
            {"episode_id": e.episode_id, "source": e.source,
             "provider_id": e.provider_id,
             "season_number": e.season_number,
             "episode_number": e.episode_number, "title": e.title,
             "plot": e.plot, "runtime": e.runtime,
             "group_type": e.group_type, "strm_path": e.strm_path,
             "nfo_path": e.nfo_path, "thumb_path": e.thumb_path,
             "availability": e.availability, "metadata_pending": e.metadata_pending}
            for e in w.episodes
        ],
        "related_works": [
            {"work_id": r.work_id, "title": r.title, "year": r.year,
             "card_type": r.card_type, "relation_type": r.relation_type,
             "poster_path": r.poster_path, "fanart_path": r.fanart_path,
             "show_type": r.show_type}
            for r in w.related_works
        ],
        "cast": w.cast,
        "tags": w.tags,
        "last_played": w.last_played,
        "tracking": w.tracking,
        "metadata_state": w.metadata_state,
        "certification": w.certification,
        "certification_country": w.certification_country,
        "artwork_provenance": w.artwork_provenance,
    }
