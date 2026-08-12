# -*- coding: utf-8 -*-
"""Library/ScrapeMap consistency diagnostics.

This module is read-only.  It verifies that scrape results have been reflected
into LibraryIndex without making LibraryIndex a new source of truth.
"""

import hashlib
from pathlib import Path
from typing import Optional

from app.library.store import load_library_index
from app.import_plan.store import load_import_plan
from app.scrape.store import load_scrape_map


def diagnose_library_consistency(source: Optional[str] = None) -> dict:
    """Check scrape_map -> library_index consistency.

    The main case this protects is a multi-season series card: each local
    season can bind to a different TMDB season or even a different TMDB item,
    but LibraryIndex must preserve that mapping on the matching SeasonIndex.
    """
    index = load_library_index()
    scrape_map = load_scrape_map()

    errors: list[dict] = []
    warnings: list[dict] = []

    if index is None:
        return {
            "ok": False,
            "summary": {
                "work_count": 0,
                "scrape_item_count": len(scrape_map.items),
                "checked_scrape_seasons": 0,
                "error_count": 1,
                "warning_count": 0,
                "seasonal_work_count": 0,
                "skipped_seasonal_scrape_items": 0,
            },
            "errors": [{"code": "library_index_missing", "message": "library_index.json 不存在，请先重扫媒体库"}],
            "warnings": [],
        }

    works_by_id = {work.work_id: work for work in index.works}
    # 卡片可按已确认 TMDB 身份合并中英文目录别名，不能再用旧的
    # ``source + series_group`` 哈希反查。scrape_target_id 是从计划到镜像、
    # ScrapeMap 和 LibraryIndex 都保存的稳定季级身份，应优先使用。
    seasons_by_target_id: dict[str, tuple] = {}
    for indexed_work in index.works:
        if source and indexed_work.source != source:
            continue
        for indexed_season in indexed_work.seasons:
            if indexed_season.scrape_target_id:
                seasons_by_target_id.setdefault(
                    indexed_season.scrape_target_id,
                    (indexed_work, indexed_season),
                )
    checked_scrape_seasons = 0
    skipped_seasonal_scrape_items = 0
    plan_scope_cache: dict[str, str] = {}

    for item in scrape_map.items:
        if source and item.source != source:
            continue
        if _is_seasonal_scrape_item(item, plan_scope_cache):
            skipped_seasonal_scrape_items += 1
            continue
        if item.tmdb_type != "tv" or item.local_season_number is None:
            continue

        checked_scrape_seasons += 1
        work_id = _library_work_id_from_scrape_item(item)
        work = works_by_id.get(work_id)
        season = None
        merged_equivalent = False
        target_match = seasons_by_target_id.get(item.scrape_target_id)
        if target_match is not None:
            work, season = target_match
            work_id = work.work_id
        elif item.tmdb_id and item.tmdb_type:
            # 同一作品的不同目录别名可能分别有刮削映射，但 LibraryIndex
            # 会只保留一个代表版本的 target ID。TMDB 季身份一致即为同一
            # 已确认季，不能把另一个别名误报成丢卡。
            expected_group = "special" if item.local_season_number == 0 else "season"
            for indexed_work in index.works:
                if indexed_work.source != item.source:
                    continue
                candidate = next(
                    (
                        s for s in indexed_work.seasons
                        if s.group_type == expected_group
                        and s.season_number == item.local_season_number
                        and s.tmdb_id == item.tmdb_id
                        and s.tmdb_type == item.tmdb_type
                        and s.tmdb_season_number == item.tmdb_season_number
                    ),
                    None,
                )
                if candidate is not None:
                    work, season = indexed_work, candidate
                    work_id = work.work_id
                    merged_equivalent = True
                    break
        context = _scrape_context(item, work_id)

        if work is None:
            errors.append({
                **context,
                "code": "library_work_missing",
                "message": "ScrapeMap 已有刮削映射，但 LibraryIndex 中找不到对应作品卡片",
            })
            continue

        if season is None:
            expected_group = "special" if item.local_season_number == 0 else "season"
            season = next(
                (
                    s for s in work.seasons
                    if s.group_type == expected_group
                    and s.season_number == item.local_season_number
                ),
                None,
            )
        if season is None:
            errors.append({
                **context,
                "code": "library_season_missing",
                "message": "ScrapeMap 已有刮削映射，但 LibraryIndex 中找不到对应本地季",
            })
            continue

        if season.tmdb_id != item.tmdb_id or season.tmdb_season_number != item.tmdb_season_number:
            errors.append({
                **context,
                "code": "season_tmdb_mismatch",
                "message": "LibraryIndex 中的季级 TMDB 映射和 ScrapeMap 不一致",
                "library_tmdb_id": season.tmdb_id,
                "library_tmdb_season_number": season.tmdb_season_number,
            })

        if season.scrape_target_id != item.scrape_target_id and not merged_equivalent:
            warnings.append({
                **context,
                "code": "season_target_id_mismatch",
                "message": "LibraryIndex 的 scrape_target_id 和 ScrapeMap 不一致，建议重扫媒体库",
                "library_scrape_target_id": season.scrape_target_id,
            })

        _check_asset_exists(item.nfo_path, "nfo_missing", "NFO 文件不存在", context, warnings)
        _check_asset_exists(item.poster_path, "poster_missing", "poster 图片不存在", context, warnings)
        _check_asset_exists(item.fanart_path, "fanart_missing", "fanart 图片不存在", context, warnings)
        _check_asset_exists(item.clearlogo_path, "clearlogo_missing", "clearlogo 图片不存在", context, warnings)

    return {
        "ok": not errors,
        "summary": {
            "work_count": len(index.works),
            "scrape_item_count": len([i for i in scrape_map.items if not source or i.source == source]),
            "checked_scrape_seasons": checked_scrape_seasons,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "seasonal_work_count": len([
                work for work in index.works
                if work.import_scope == "seasonal" and (not source or work.source == source)
            ]),
            "skipped_seasonal_scrape_items": skipped_seasonal_scrape_items,
        },
        "errors": errors,
        "warnings": warnings,
}


def _is_seasonal_scrape_item(item, plan_scope_cache: dict[str, str]) -> bool:
    plan_id = item.import_plan_id or ""
    if not plan_id:
        return False
    if plan_id not in plan_scope_cache:
        plan = load_import_plan(plan_id=plan_id)
        plan_scope_cache[plan_id] = plan.import_scope if plan is not None else ""
    return plan_scope_cache[plan_id] == "seasonal"


def _library_work_id_from_scrape_item(item) -> str:
    if item.card_type == "main_series" and item.series_group:
        content = f"{item.source}:{item.series_group}"
        return "series_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    return item.work_id


def _scrape_context(item, library_work_id: str) -> dict:
    return {
        "source": item.source,
        "library_work_id": library_work_id,
        "scrape_target_id": item.scrape_target_id,
        "series_group": item.series_group,
        "scrape_title": item.scrape_title,
        "local_season_number": item.local_season_number,
        "tmdb_id": item.tmdb_id,
        "tmdb_season_number": item.tmdb_season_number,
    }


def _check_asset_exists(path: str, code: str, message: str, context: dict, warnings: list[dict]) -> None:
    if path and path.lower().startswith(("http://", "https://")):
        return
    if path and not Path(path).exists():
        warnings.append({
            **context,
            "code": code,
            "message": message,
            "path": path,
        })
