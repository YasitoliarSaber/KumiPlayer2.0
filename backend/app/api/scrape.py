# -*- coding: utf-8 -*-
"""刮削 API"""

import os
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.scrape.models import ScrapeTarget
from app.scrape import service as scrape_service
from app.scrape.store import load_failed_cases
from app.scrape.review_queue import get_pending_review_items, prune_pending_review_items, resolve_review_item
from app.tasks.registry import get_task_manager

router = APIRouter(prefix="/api/scrape", tags=["scrape"])

# target 缓存
_targets_cache: dict = {}  # target_id -> ScrapeTarget


# ============================================================
# 请求模型
# ============================================================

class SelectRequest(BaseModel):
    target_id: str
    tmdb_id: int
    tmdb_type: str = "tv"
    tmdb_season_number: Optional[int] = None
    selected_by: str = "manual"
    search_query: Optional[str] = None
    include_episode: bool = True
    library_work_id: str | None = None
    scope: Literal["season", "work"] = "season"


class AutoScrapeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "pan115"
    plan_id: Optional[str] = None
    threshold: float = 70
    include_episode: bool = True


class ReviewSkipRequest(BaseModel):
    target_id: str


# ============================================================
# 辅助函数
# ============================================================

def _try_restore_targets(source: str) -> bool:
    """尝试从最新 confirmed plan 重建 targets 缓存"""
    try:
        targets, error = scrape_service.get_targets(source)
        if error or not targets:
            return False
        for t in targets:
            _targets_cache[t.scrape_target_id] = t
        return True
    except Exception:
        return False


def _normalize_review_source(source: Optional[str]) -> Optional[str]:
    value = (source or "").strip().lower()
    if value in {"", "all"}:
        return None
    if value not in {"pan115", "baidu", "local", "openlist"}:
        raise HTTPException(status_code=400, detail=f"不支持的来源: {source}")
    return value


def _prune_stale_review_items(source: Optional[str]) -> None:
    """Keep review queue aligned with the latest current ImportPlan targets."""
    pending = get_pending_review_items(source)
    sources = [source] if source else sorted({item.source for item in pending if item.source})

    for src in sources:
        if not src:
            continue
        targets, error = scrape_service.get_targets(src)
        valid_ids = {target.scrape_target_id for target in targets} if not error else set()
        prune_pending_review_items(valid_ids, src)


def _get_target_or_restore(target_id: str) -> Optional[ScrapeTarget]:
    """获取 target，缓存未命中时尝试恢复

    恢复顺序：
    1. 从内存缓存恢复
    2. 从最新 confirmed plan 重建 targets
    3. 从 scrape_map.json 持久化记录恢复
    """
    target = _targets_cache.get(target_id)
    if target:
        return target

    for source in ("pan115", "baidu", "local", "openlist"):
        if _try_restore_targets(source):
            target = _targets_cache.get(target_id)
            if target:
                return target

    # 从 scrape_map 恢复
    target = _restore_target_from_scrape_map(target_id)
    if target:
        _targets_cache[target_id] = target
        return target

    return None


def _restore_target_from_scrape_map(target_id: str) -> Optional[ScrapeTarget]:
    """恢复 target 信息：V3 SQLite binding 优先（metadata_json），legacy JSON 兜底。"""
    try:
        from app.scrape.effective_store import load_all_bindings_scrape_map
        from app.scrape.store import load_scrape_map

        item = next(
            (i for i in load_all_bindings_scrape_map().items if i.scrape_target_id == target_id),
            None,
        )
        if item is None:
            item = next(
                (i for i in load_scrape_map().items if i.scrape_target_id == target_id),
                None,
            )
        if item is None:
            return None
        return ScrapeTarget(
            scrape_target_id=item.scrape_target_id,
            source=item.source,
            import_plan_id=item.import_plan_id,
            work_id=item.work_id,
            card_type=item.card_type,
            media_type=item.media_type,
            show_type="",
            group_type="season" if item.media_type == "tv" else "movie",
            series_group=item.series_group,
            local_title=item.local_title,
            original_title=item.original_title,
            source_subwork_dir=item.source_subwork_dir,
            local_year=item.local_year,
            local_season_number=item.local_season_number,
            scrape_title=item.scrape_title,
            scrape_year=item.scrape_year,
            scrape_type=item.tmdb_type or "tv",
            target_dir="",
            target_nfo_path=item.nfo_path,
            target_poster_path=item.poster_path,
            target_fanart_path=item.fanart_path,
            target_clearlogo_path=item.clearlogo_path,
            tmdb_hint_id=item.tmdb_id,
            tmdb_hint_type=item.tmdb_type,
            needs_review=False,
        )
    except Exception:
        pass
    return None


def _resolve_tmdb_season_number(
    target: ScrapeTarget,
    tmdb_id: int,
    requested_season_number: Optional[int],
    tmdb_type: str,
) -> Optional[int]:
    """统一手动/自动刮削的 TMDB 季号推断。

    先检查用户选中的 TMDB 条目是否存在本地季号；存在则使用本地季号，
    不存在时才回落到 Season 1。这样 CLANNAD S2 会绑定同一 TMDB 条目的
    Season 2，而独立续作条目仍能绑定 Season 1。
    """
    return scrape_service.resolve_tmdb_season_number(
        target=target,
        tmdb_id=tmdb_id,
        tmdb_type=tmdb_type,
        requested_season_number=requested_season_number,
    )


def _library_scrape_target_refs(
    work_id: str,
    season_number: Optional[int] = None,
    group_type: Optional[str] = None,
) -> tuple[set[str], set[str]]:
    """Resolve aggregate LibraryIndex work_id to concrete scrape target references."""
    target_ids: set[str] = set()
    raw_work_ids: set[str] = set()

    try:
        from app.library.store import load_library_index
        index = load_library_index()
    except Exception:
        index = None
    if index is None:
        return target_ids, raw_work_ids

    matched = False
    for work in index.works:
        if work.work_id == work_id:
            matched = True
            for season in work.seasons:
                if season_number is not None and season.season_number != season_number:
                    continue
                if group_type and season.group_type != group_type:
                    continue
                target_id = getattr(season, "scrape_target_id", "") or ""
                if target_id:
                    target_ids.add(target_id)
            break

    if matched:
        try:
            from app.import_plan import revision_store
            from app.import_plan.store import load_latest_confirmed_import_plan
            from app.library.index import _library_work_id

            sources = [source for source in ("pan115", "baidu", "local", "openlist")]
            for src in sources:
                # Module 5：V3 current revisions 优先；无 V3 state 才回退 legacy latest
                plans = revision_store.list_current_plans(src)
                if not plans:
                    legacy_plan = load_latest_confirmed_import_plan(src)
                    if legacy_plan is not None:
                        plans = [legacy_plan]
                for plan in plans:
                    matched_series_groups = {
                        item.series_group
                        for item in plan.items
                        if item.series_group
                        and item.card_type == "main_series"
                        and _library_work_id(item) == work_id
                    }
                    for item in plan.items:
                        if item.resource_type != "video" or item.action != "generate_strm":
                            continue
                        if item.group_type in {"ignored", "op_ed"}:
                            continue
                        same_card = _library_work_id(item) == work_id
                        same_series = (
                            item.card_type == "main_series"
                            and item.series_group in matched_series_groups
                        )
                        if not same_card and not same_series:
                            continue
                        if season_number is not None and item.season_number != season_number:
                            continue
                        if group_type and item.group_type != group_type:
                            continue
                        if item.work_id:
                            raw_work_ids.add(item.work_id)
        except Exception:
            pass

    return target_ids, raw_work_ids


def _library_scrape_target_paths(work_id: str) -> set[str]:
    """返回聚合作品可用于反查原始 ScrapeTarget 的镜像目录。"""
    try:
        from app.library.store import load_library_index
        index = load_library_index()
    except Exception:
        return set()
    if index is None:
        return set()

    for work in index.works:
        if work.work_id != work_id:
            continue
        paths = {work.dir_path} if work.dir_path else set()
        for season in work.seasons:
            if season.nfo_path:
                paths.add(str(Path(season.nfo_path).parent))
        for episode in work.episodes:
            if episode.strm_path:
                paths.add(str(Path(episode.strm_path).parent))
        return {_normalized_path(path) for path in paths if path}
    return set()


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _target_matches_library_paths(target: ScrapeTarget, paths: set[str]) -> bool:
    if not paths or not target.target_dir:
        return False
    target_path = _normalized_path(target.target_dir)
    for path in paths:
        try:
            if target_path == path or os.path.commonpath([target_path, path]) in {target_path, path}:
                return True
        except ValueError:
            continue
    return False


def _target_card_slot(target: ScrapeTarget) -> tuple[str, Optional[int], str]:
    """同一卡片内用于去除多来源重复季度的稳定键。"""
    return (
        target.group_type,
        target.local_season_number,
        (target.series_group or target.local_title or target.scrape_title).strip().casefold(),
    )


def _get_targets_for_library_work(
    work_id: str,
    preferred_source: Optional[str] = None,
) -> list[ScrapeTarget]:
    """返回当前 LibraryIndex 卡片对应的全部季度目标。

    LibraryIndex 中显式保存的 scrape_target_id 最可靠；只有旧索引缺少该字段时，
    才回退到原始 work_id 和镜像路径匹配。
    """
    library_target_ids, library_work_ids = _library_scrape_target_refs(work_id)
    library_paths = _library_scrape_target_paths(work_id)
    sources = ["pan115", "baidu", "local", "openlist"]
    if preferred_source in sources:
        sources.remove(preferred_source)
        sources.insert(0, preferred_source)

    exact_matches: list[ScrapeTarget] = []
    fallback_matches: list[ScrapeTarget] = []
    seen_target_ids: set[str] = set()
    for target_id in sorted(library_target_ids):
        restored = _get_target_or_restore(target_id)
        if restored is None or restored.scrape_target_id in seen_target_ids:
            continue
        exact_matches.append(restored)
        seen_target_ids.add(restored.scrape_target_id)

    for source in sources:
        targets, error = scrape_service.get_targets(source)
        if error:
            continue
        for target in targets:
            _targets_cache[target.scrape_target_id] = target
            if target.scrape_target_id in seen_target_ids:
                continue
            if target.scrape_target_id in library_target_ids:
                exact_matches.append(target)
                seen_target_ids.add(target.scrape_target_id)
                continue
            if (
                target.work_id == work_id
                or target.work_id in library_work_ids
                or _target_matches_library_paths(target, library_paths)
            ):
                fallback_matches.append(target)
                seen_target_ids.add(target.scrape_target_id)

    def sort_key(target: ScrapeTarget):
        return (
            0 if target.source == preferred_source else 1,
            0 if target.group_type == "movie" else 1,
            target.local_season_number if target.local_season_number is not None else 999,
            target.scrape_target_id,
        )

    matches: list[ScrapeTarget] = []
    seen_slots: set[tuple[str, Optional[int], str]] = set()
    for target in [*sorted(exact_matches, key=sort_key), *sorted(fallback_matches, key=sort_key)]:
        slot = _target_card_slot(target)
        if slot in seen_slots:
            continue
        seen_slots.add(slot)
        matches.append(target)
    return sorted(matches, key=sort_key)


# ============================================================
# 端点
# ============================================================

@router.get("/targets")
def get_scrape_targets(source: str = "pan115", plan_id: Optional[str] = None):
    """获取可刮削目标"""
    targets, error = scrape_service.get_targets(source, plan_id)
    if error:
        raise HTTPException(status_code=400, detail=error)

    for t in targets:
        _targets_cache[t.scrape_target_id] = t

    summary = {
        "total": len(targets),
        "tv_count": sum(1 for t in targets if t.scrape_type == "tv"),
        "movie_count": sum(1 for t in targets if t.scrape_type == "movie"),
        "needs_review_count": sum(1 for t in targets if t.needs_review),
    }
    return {
        "targets": [_target_to_dict(t) for t in targets],
        "summary": summary,
    }


@router.get("/target-by-work")
def get_target_by_work(
    work_id: str,
    source: Optional[str] = None,
    season_number: Optional[int] = None,
    group_type: Optional[str] = None,
):
    """按 LibraryIndex 的 work_id 找到详情页手动刮削可用的 target。"""
    sources = [source] if source else ["pan115", "baidu", "local", "openlist"]
    matches: list[ScrapeTarget] = []
    library_target_ids, library_work_ids = _library_scrape_target_refs(work_id, season_number, group_type)
    library_paths = _library_scrape_target_paths(work_id)

    for src in sources:
        if not src:
            continue
        targets, error = scrape_service.get_targets(src)
        if error:
            continue
        for target in targets:
            _targets_cache[target.scrape_target_id] = target
            if (
                target.work_id != work_id
                and target.scrape_target_id not in library_target_ids
                and target.work_id not in library_work_ids
                and not _target_matches_library_paths(target, library_paths)
            ):
                continue
            if season_number is not None and target.local_season_number != season_number:
                continue
            if group_type and target.group_type != group_type:
                continue
            matches.append(target)

    if not matches:
        raise HTTPException(status_code=404, detail=f"未找到作品可刮削目标: {work_id}")

    def sort_key(target: ScrapeTarget):
        return (
            0 if target.group_type == "movie" else 1,
            0 if not target.needs_review else 1,
            target.local_season_number if target.local_season_number is not None else 999,
        )

    target = sorted(matches, key=sort_key)[0]
    return {"target": _target_to_dict(target)}


@router.get("/candidates")
def get_candidates(target_id: str, query: Optional[str] = None, year: Optional[int] = None):
    """搜索 TMDB 候选"""
    target = _get_target_or_restore(target_id)
    if not target:
        raise HTTPException(
            status_code=404,
            detail=f"target 不存在: {target_id}，请先 GET /api/scrape/targets",
        )

    try:
        candidates = scrape_service.search_candidates(target, query=query, year=year)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TMDB 搜索失败: {e}")

    return {
        "target": _target_to_dict(target),
        "search_queries": scrape_service.build_candidate_search_queries(target, query=query, prefer_query=bool(query)),
        "candidates": [_candidate_to_dict(c) for c in candidates],
    }


@router.post("/select")
def select_candidate(req: SelectRequest):
    """用户选择 TMDB 候选，创建刮削任务"""
    target = _get_target_or_restore(req.target_id)
    if not target:
        raise HTTPException(status_code=404, detail=f"target 不存在: {req.target_id}")

    if req.selected_by == "manual_replace":
        if not req.library_work_id:
            raise HTTPException(status_code=422, detail="详情页手动刮削必须提供 library_work_id")
        if not _target_belongs_to_library_work(target, req.library_work_id):
            raise HTTPException(status_code=409, detail="刮削目标不属于当前作品卡片，请刷新详情页后重试")

    manager = get_task_manager()
    try:
        tmdb_season_number = _resolve_tmdb_season_number(
            target,
            req.tmdb_id,
            req.tmdb_season_number,
            req.tmdb_type,
        )
        if req.selected_by == "manual_replace" and req.scope == "work":
            work_targets = _get_targets_for_library_work(
                req.library_work_id or "",
                preferred_source=target.source,
            )
            if target.scrape_target_id not in {item.scrape_target_id for item in work_targets}:
                selected_slot = _target_card_slot(target)
                work_targets = [
                    item for item in work_targets
                    if _target_card_slot(item) != selected_slot
                ]
                work_targets.append(target)
            work_targets.sort(key=lambda item: (
                item.local_season_number if item.local_season_number is not None else 999,
                item.group_type,
                item.scrape_target_id,
            ))
        else:
            work_targets = []

        if len(work_targets) > 1:
            record = manager.submit_queued(
                task_type="scrape_manual_work",
                source=target.source,
                fn=_run_selected_work_scrape,
                target=target,
                work_targets=work_targets,
                tmdb_id=req.tmdb_id,
                tmdb_type=req.tmdb_type,
                tmdb_season_number=tmdb_season_number,
                selected_by=req.selected_by,
                search_query=req.search_query,
                include_episode=req.include_episode,
                library_work_id=req.library_work_id,
                queue_name="scrape",
                initial_result={
                    "plan_id": target.import_plan_id,
                    "scrape_target_id": target.scrape_target_id,
                    "scope": "work",
                    "total_targets": len(work_targets),
                },
                message=f"整部作品刮削: {target.series_group or target.scrape_title}",
            )
            return {"task_id": record.task_id, "status": record.status}

        record = manager.submit_queued(
            task_type="scrape_select",
            source=target.source,
            fn=_run_selected_scrape_and_update_preset,
            target=target,
            tmdb_id=req.tmdb_id,
            tmdb_type=req.tmdb_type,
            tmdb_season_number=tmdb_season_number,
            selected_by=req.selected_by,
            search_query=req.search_query,
            include_episode=req.include_episode,
            library_work_id=req.library_work_id,
            rescan_after=True,
            queue_name="scrape",
            initial_result={"plan_id": target.import_plan_id, "scrape_target_id": target.scrape_target_id},
            message=f"刮削: {target.scrape_title}",
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"task_id": record.task_id, "status": record.status}


def _target_belongs_to_library_work(target: ScrapeTarget, library_work_id: str) -> bool:
    """确认详情页选中的季度确实属于当前卡片，防止并发刷新后误改其他作品。"""
    try:
        from app.library.store import load_library_index

        index = load_library_index()
    except Exception:
        return False
    if index is None or not any(work.work_id == library_work_id for work in index.works):
        return False
    if target.work_id == library_work_id:
        return True

    target_ids, raw_work_ids = _library_scrape_target_refs(
        library_work_id,
        target.local_season_number,
        target.group_type,
    )
    if target.scrape_target_id in target_ids or target.work_id in raw_work_ids:
        return True
    return _target_matches_library_paths(target, _library_scrape_target_paths(library_work_id))


@router.post("/review-queue/skip")
def skip_review_item(req: ReviewSkipRequest):
    """跳过一个人工确认条目"""
    item = next((entry for entry in get_pending_review_items() if entry.scrape_target_id == req.target_id), None)
    ok = resolve_review_item(req.target_id, "skipped")
    if not ok:
        raise HTTPException(status_code=404, detail=f"review 条目不存在: {req.target_id}")
    if item and item.import_plan_id:
        _mark_plan_ready_when_review_complete(item.import_plan_id)
    return {"target_id": req.target_id, "status": "skipped"}


def _mark_plan_ready_when_review_complete(plan_id: str) -> None:
    if not plan_id:
        return
    remaining = [item for item in get_pending_review_items() if item.import_plan_id == plan_id]
    if not remaining:
        from app.media_presets.service import mark_preset_lifecycle
        mark_preset_lifecycle(plan_id, "ready")


def _run_selected_scrape_and_update_preset(
    target: ScrapeTarget,
    progress_callback=None,
    should_cancel=None,
    **kwargs,
):
    """执行人工选定候选；成功后再关闭人工项并推进媒体库状态。"""
    result = scrape_service.execute_scrape(
        target=target,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
        **kwargs,
    )
    resolve_review_item(target.scrape_target_id, "resolved")
    _mark_plan_ready_when_review_complete(target.import_plan_id)
    return {"plan_id": target.import_plan_id, "scrape_target_id": target.scrape_target_id, **(result or {})}


def _run_selected_work_scrape(
    target: ScrapeTarget,
    work_targets: list[ScrapeTarget],
    tmdb_id: int,
    tmdb_type: str,
    tmdb_season_number: Optional[int] = None,
    selected_by: str = "manual_replace",
    search_query: Optional[str] = None,
    include_episode: bool = True,
    library_work_id: str | None = None,
    progress_callback=None,
    should_cancel=None,
) -> dict:
    """先应用人工确认结果，再用成熟的自动链路处理同卡片其余季度。"""
    total_targets = max(1, len(work_targets))

    def manual_progress(progress: int, message: str, patch: Optional[dict] = None) -> None:
        if progress_callback:
            progress_callback(
                min(35, max(1, int(progress * 0.35))),
                message,
                {
                    **(patch or {}),
                    "scope": "work",
                    "total_targets": total_targets,
                    "current_index": 1,
                    "manual_scraped": 0,
                },
            )

    manual_result = scrape_service.execute_scrape(
        target=target,
        tmdb_id=tmdb_id,
        tmdb_type=tmdb_type,
        tmdb_season_number=tmdb_season_number,
        selected_by=selected_by,
        search_query=search_query,
        include_episode=include_episode,
        rescan_after=True,
        library_work_id=library_work_id,
        progress_callback=manual_progress,
        should_cancel=should_cancel,
    )
    resolve_review_item(target.scrape_target_id, "resolved")

    remaining_targets = [
        item for item in work_targets
        if item.scrape_target_id != target.scrape_target_id
    ]
    grouped_targets: dict[tuple[str, str], list[ScrapeTarget]] = {}
    for item in remaining_targets:
        grouped_targets.setdefault(
            (item.source, item.import_plan_id),
            [],
        ).append(item)

    aggregate = {
        "auto_scraped": 0,
        "skipped_existing": 0,
        "review_queued": 0,
        "failed": 0,
        "remaining_targets": 0,
        "results": [],
    }
    groups = list(grouped_targets.items())
    for group_index, ((source, plan_id), targets) in enumerate(groups):
        group_start = 35 + int(group_index * 60 / max(1, len(groups)))
        group_span = max(1, int(60 / max(1, len(groups))))

        def batch_progress(progress: int, message: str, patch: Optional[dict] = None) -> None:
            if progress_callback:
                progress_callback(
                    min(95, group_start + int(progress * group_span / 100)),
                    message,
                    {
                        **(patch or {}),
                        "scope": "work",
                        "total_targets": total_targets,
                        "manual_scraped": 1,
                    },
                )

        from app.scrape.auto import run_auto_scrape

        batch_result = run_auto_scrape(
            source=source,
            plan_id=plan_id or None,
            include_episode=include_episode,
            progress_callback=batch_progress,
            should_cancel=should_cancel,
            target_ids={item.scrape_target_id for item in targets},
            library_work_id=library_work_id or "",
        )
        for key in ("auto_scraped", "skipped_existing", "review_queued", "failed", "remaining_targets"):
            aggregate[key] += int(batch_result.get(key) or 0)
        aggregate["results"].extend(batch_result.get("results") or [])

    from app.library.service import refresh_library_for_scrape_targets

    library_refresh = refresh_library_for_scrape_targets(
        work_targets,
        library_work_id=library_work_id or "",
    )
    for plan_id in {item.import_plan_id for item in work_targets if item.import_plan_id}:
        _mark_plan_ready_when_review_complete(plan_id)
    if progress_callback:
        progress_callback(100, "整部作品刮削处理完成", {
            "scope": "work",
            "total_targets": total_targets,
            "completed_targets": max(0, total_targets - aggregate["remaining_targets"]),
            "manual_scraped": 1,
            **aggregate,
        })
    return {
        "scope": "work",
        "plan_id": target.import_plan_id,
        "scrape_target_id": target.scrape_target_id,
        "total_targets": total_targets,
        "manual_scraped": 1,
        "manual_result": manual_result,
        "library_refresh": library_refresh,
        **aggregate,
    }


@router.get("/tasks/{task_id}")
def get_scrape_task(task_id: str):
    """查询刮削任务状态"""
    manager = get_task_manager()
    record = manager.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _record_to_dict(record)


@router.get("/tasks")
def list_scrape_tasks(source: Optional[str] = None, limit: int = 20):
    """列出最近刮削任务，用于前端刷新后恢复进度和日志"""
    manager = get_task_manager()
    records = [
        record for record in manager.list_tasks(source=source)
        if record.task_type.startswith("scrape_")
    ][:max(1, min(limit, 100))]
    return {"tasks": [_record_to_dict(record) for record in records]}


@router.post("/tasks/{task_id}/cancel")
def cancel_scrape_task(task_id: str):
    """停止刮削任务，释放刮削通道"""
    manager = get_task_manager()
    record = manager.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    manager.cancel_task(task_id)
    return _record_to_dict(manager.get_task(task_id) or record)


@router.get("/failures")
def get_failures():
    """获取失败案例"""
    cases = load_failed_cases()
    return {"failures": cases}


def _run_auto_scrape_and_update_preset(
    source: str,
    plan_id: Optional[str] = None,
    threshold: float = 70,
    include_episode: bool = True,
    progress_callback=None,
    should_cancel=None,
):
    """执行刮削并让对应目录树档案进入正确生命周期。"""
    from app.media_presets.service import mark_preset_lifecycle
    from app.scrape.auto import run_auto_scrape

    result = run_auto_scrape(
        source,
        plan_id=plan_id,
        threshold=threshold,
        include_episode=include_episode,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
    )
    if plan_id:
        complete = (
            not result.get("error")
            and int(result.get("failed", 0)) == 0
            and int(result.get("review_queued", 0)) == 0
        )
        mark_preset_lifecycle(plan_id, "ready" if complete else "needs_attention")
    return {"plan_id": plan_id or "", **(result or {})}


@router.post("/auto")
def auto_scrape(req: AutoScrapeRequest):
    """自动刮削

    高置信度自动采用，低置信度进入 review_queue。
    """
    try:
        manager = get_task_manager()
        record = manager.submit_queued(
            "scrape_auto",
            req.source,
            _run_auto_scrape_and_update_preset,
            req.source,
            plan_id=req.plan_id,
            threshold=req.threshold,
            include_episode=req.include_episode,
            queue_name="scrape",
            initial_result={"plan_id": req.plan_id or ""},
            message="自动刮削",
        )
        return {"task_id": record.task_id, "status": record.status}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"自动刮削失败: {e}")


@router.post("/certifications/backfill")
def backfill_certifications():
    """仅补全当前媒体库缺失的分级信息。"""
    from app.scrape.certification_backfill import backfill_missing_certifications

    manager = get_task_manager()
    record = manager.submit(
        "scrape_certification_backfill",
        "all",
        backfill_missing_certifications,
        message="补全缺失分级",
    )
    return {"task_id": record.task_id, "status": record.status}


@router.get("/review-queue")
def get_review_queue(source: Optional[str] = None):
    """获取待人工确认的刮削队列"""
    source_filter = _normalize_review_source(source)
    _prune_stale_review_items(source_filter)
    items = get_pending_review_items(source_filter)
    return {
        "items": [
            {
                "scrape_target_id": item.scrape_target_id,
                "source": item.source,
                "import_plan_id": item.import_plan_id or ((_get_target_or_restore(item.scrape_target_id) or ScrapeTarget()).import_plan_id),
                "series_group": item.series_group,
                "local_title": item.local_title,
                "scrape_title": item.scrape_title,
                "scrape_year": item.scrape_year,
                "scrape_type": item.scrape_type,
                "local_season_number": item.local_season_number,
                "reason": item.reason,
                "candidates": item.candidates,
                "added_at": item.added_at,
                "status": item.status,
            }
            for item in items
        ],
        "total": len(items),
    }


# ============================================================
# 序列化辅助
# ============================================================

def _target_to_dict(t: ScrapeTarget) -> dict:
    return {
        "scrape_target_id": t.scrape_target_id,
        "source": t.source,
        "import_plan_id": t.import_plan_id,
        "work_id": t.work_id,
        "card_type": t.card_type,
        "media_type": t.media_type,
        "show_type": getattr(t, "show_type", ""),
        "group_type": t.group_type,
        "series_group": t.series_group,
        "local_title": t.local_title,
        "original_title": t.original_title,
        "source_subwork_dir": t.source_subwork_dir,
        "local_year": t.local_year,
        "local_season_number": t.local_season_number,
        "scrape_title": t.scrape_title,
        "scrape_year": t.scrape_year,
        "scrape_type": t.scrape_type,
        "tmdb_hint_id": t.tmdb_hint_id,
        "tmdb_hint_type": t.tmdb_hint_type,
        "target_dir": t.target_dir,
        "target_nfo_path": t.target_nfo_path,
        "target_poster_path": t.target_poster_path,
        "target_fanart_path": t.target_fanart_path,
        "target_clearlogo_path": t.target_clearlogo_path,
        "item_ids": t.item_ids,
        "local_episode_count": t.local_episode_count,
        "needs_review": t.needs_review,
        "warnings": t.warnings,
    }


def _candidate_to_dict(c) -> dict:
    raw = getattr(c, "raw", {}) or {}
    return {
        "candidate_id": c.candidate_id,
        "scrape_target_id": c.scrape_target_id,
        "provider": c.provider,
        "tmdb_id": c.tmdb_id,
        "tmdb_type": c.tmdb_type,
        "title": c.title,
        "original_title": c.original_title,
        "year": c.year,
        "overview": c.overview[:200] if c.overview else "",
        "poster_path": c.poster_path,
        "popularity": c.popularity,
        "vote_average": c.vote_average,
        "score": c.score,
        "reasons": c.reasons,
        "source_meta": _candidate_source_meta(c.provider, raw),
    }


def _candidate_source_meta(provider: str, raw: dict) -> dict:
    if provider != "anilist":
        return {"provider": provider}
    media = raw.get("anilist") or {}
    titles = media.get("title") or {}
    return {
        "provider": provider,
        "anilist_id": media.get("id"),
        "format": media.get("format"),
        "season_year": media.get("seasonYear"),
        "episodes": media.get("episodes"),
        "average_score": media.get("averageScore"),
        "banner_image": media.get("bannerImage"),
        "native_title": titles.get("native") or "",
        "title_aliases": list(raw.get("provider_title_aliases") or []),
        "canonical_assets": raw.get("canonical_assets") or {},
    }


def _record_to_dict(record) -> dict:
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
