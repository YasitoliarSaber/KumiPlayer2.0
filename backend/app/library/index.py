# -*- coding: utf-8 -*-
"""LibraryIndex 构建器

从 ImportPlan + ScrapeMap + MirrorScanResult 构建 LibraryIndex。
所有结构字段来自 ImportPlan，不从 mirror 文件名推断。
"""

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.library.identity import _is_local_collection_root, library_card_identity
from app.library.models import EpisodeIndex, LibraryIndex, RelatedWork, SeasonIndex, WorkIndex
from app.library.scanner import MirrorAsset, MirrorFile, MirrorScanResult
from app.scrape.completeness import target_already_scraped
from app.scrape.target_builder import build_scrape_targets


def build_library_index(
    plan: ImportPlan,
    scrape_map: Optional[Any] = None,
    scan_result: Optional[MirrorScanResult] = None,
) -> LibraryIndex:
    """构建 LibraryIndex

    参数:
        plan: confirmed/executed ImportPlan
        scrape_map: ScrapeMap（可选）
        scan_result: MirrorScanResult（可选）
    """
    # 建立 strm_path → MirrorFile 索引，只使用当前 plan.source 的扫描结果。
    # rescan all sources 时 scan_result 会包含多个 namespace，不能把其他来源的
    # .strm 计入当前来源的 orphan。
    strm_index: Dict[str, MirrorFile] = {}
    if scan_result:
        for mf in scan_result.strm_files:
            if mf.source == plan.source:
                strm_index[_normalize_path(mf.strm_path)] = mf

    # 建立 work_id → ScrapeMapItem 索引（用于 NFO/图片路径）
    scrape_work_index: Dict[str, dict] = {}
    scrape_items_by_work_id: Dict[str, List[dict]] = defaultdict(list)
    scrape_series_index: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    if scrape_map:
        for item in scrape_map.items:
            item_dict = _scrape_item_to_dict(item)
            # CP2：索引键优先 canonical_work_id（V3 前台作品身份），
            # 不同 canonical 即使 work_id 相同也不得串线；legacy 无 canonical
            # 时保留 work_id 键兼容。
            scrape_key = getattr(item, "canonical_work_id", "") or item.work_id
            if scrape_key:
                scrape_work_index[scrape_key] = item_dict
                scrape_items_by_work_id[scrape_key].append(item_dict)
            if (
                item.source
                and item.series_group
                and _is_main_series_scrape_candidate(item_dict, item.series_group)
            ):
                series_group = _library_series_group_from_scrape_item(item)
                if series_group:
                    scrape_series_index[(item.source, series_group)].append(item_dict)

    library_work_id = _plan_library_work_id_resolver(plan)

    # 收集 asset 路径
    asset_index: Dict[str, List[MirrorAsset]] = defaultdict(list)
    if scan_result:
        for asset in scan_result.assets:
            parent = str(Path(asset.path).parent)
            asset_index[parent].append(asset)

    # 构建 works
    works_map: Dict[str, WorkIndex] = {}
    raw_work_ids_by_library: Dict[str, set[str]] = defaultdict(set)
    canonical_work_ids_by_library: Dict[str, set[str]] = defaultdict(set)
    episode_positions: Dict[Tuple[str, str, int, int], int] = {}
    episode_variant_keys: Dict[Tuple[str, str, int, int], tuple] = {}
    missing_strm = 0
    orphan_strm = 0

    for item in plan.items:
        if (
            item.resource_type != "video"
            or item.action != "generate_strm"
            or item.group_type in {"ignored", "op_ed"}
        ):
            continue

        work_id = library_work_id(item)
        if not work_id:
            continue
        if item.work_id:
            raw_work_ids_by_library[work_id].add(item.work_id)
        if getattr(item, "canonical_work_id", ""):
            canonical_work_ids_by_library[work_id].add(item.canonical_work_id)

        # 获取或创建 WorkIndex
        if work_id not in works_map:
            works_map[work_id] = _build_work_index(
                item, work_id, scrape_work_index, scrape_series_index, asset_index,
                plan.import_scope,
            )

        work = works_map[work_id]

        # 检查 .strm 是否存在
        strm_path = item.target_strm_path
        if not strm_path:
            missing_strm += 1
            continue

        norm_path = _normalize_path(strm_path)
        mf = strm_index.get(norm_path)

        if mf is None:
            # .strm 不存在
            missing_strm += 1
            continue

        # 构建 EpisodeIndex。同一作品目录的副本可能指向同一季集；保留镜像
        # 文件，但媒体库卡片只展示一个稳定选出的可播放剧集。
        episode = _build_episode_index(item, mf, work_id)
        episode_key = _primary_episode_key(work_id, item)
        if episode_key is None:
            work.episodes.append(episode)
            continue
        variant_key = _episode_variant_sort_key(item)
        previous_position = episode_positions.get(episode_key)
        if previous_position is None:
            episode_positions[episode_key] = len(work.episodes)
            episode_variant_keys[episode_key] = variant_key
            work.episodes.append(episode)
        elif variant_key < episode_variant_keys[episode_key]:
            work.episodes[previous_position] = episode
            episode_variant_keys[episode_key] = variant_key

    # 删除镜像文件后，ImportPlan 可能还保留条目；没有任何现存 .strm 的
    # WorkIndex 不应继续出现在前端，否则会留下 0 集空卡片。
    # 旧计划曾把 CM/PV/Menu 等附属视频写成 S01E00；这类条目即使仍有
    # .strm，也不能单独撑起一张番剧卡。
    if scan_result is not None:
        works_map = {
            work_id: work
            for work_id, work in works_map.items()
            if _work_has_primary_episode(work)
        }
    else:
        works_map = {
            work_id: work
            for work_id, work in works_map.items()
            if _work_has_primary_episode(work)
            or _plan_work_has_primary_target_strm(plan, work_id, library_work_id)
        }

    # 构建 SeasonIndex
    for work in works_map.values():
        work.episodes.sort(key=_episode_sort_key)
        _normalize_special_episode_numbers(work.episodes)
        work.episodes.sort(key=_episode_sort_key)
        # V3：按 canonical 精确取刮削信息（work.work_id 即 canonical）；只有
        # legacy（无 canonical）才按 raw work_id 兼容取回。绝不从其他 canonical
        # 借 season 级 poster/fanart/NFO。
        canonical_ids = canonical_work_ids_by_library.get(work.work_id, set())
        if canonical_ids:
            scrape_infos = [
                info
                for cid in canonical_ids
                for info in scrape_items_by_work_id.get(cid, [])
            ]
        else:
            scrape_infos = [
                info
                for raw_work_id in raw_work_ids_by_library.get(work.work_id, set())
                for info in scrape_items_by_work_id.get(raw_work_id, [])
            ]
        work.seasons = _build_season_indexes(
            work, scrape_infos, asset_index
        )

    _apply_work_metadata_states(plan, works_map, scrape_map, library_work_id)

    # 计算 orphans（mirror 有但 plan 没有的 strm）
    plan_strm_paths = set()
    for item in plan.items:
        if item.target_strm_path and item.group_type not in {"ignored", "op_ed"}:
            plan_strm_paths.add(_normalize_path(item.target_strm_path))

    for mf_path in strm_index:
        if mf_path not in plan_strm_paths:
            orphan_strm += 1

    # 构建不同卡片之间的 related_works。季/季度已经聚合在同一张
    # WorkIndex 内，不应再作为“相关作品”重复展示。
    for work in works_map.values():
        work.related_works = _build_related_works(work, plan, works_map, library_work_id)

    _disambiguate_local_split_titles(works_map.values())

    # source_summary
    source_summary = _build_source_summary(
        plan, scan_result, works_map, missing_strm, orphan_strm,
        scraped_work_count=sum(
            work.metadata_state == "ready" for work in works_map.values()
        ),
    )

    return LibraryIndex(
        version=2,
        works=list(works_map.values()),
        source_summary=source_summary,
        generated_at=_now(),
    )


def _apply_work_metadata_states(
    plan: ImportPlan,
    works_map: Dict[str, WorkIndex],
    scrape_map: Optional[Any],
    library_work_id: Callable[[ImportPlanItem], str],
) -> None:
    """Derive display readiness from the same outputs used by auto-scrape."""
    scrape_index = {
        item.scrape_target_id: item
        for item in (getattr(scrape_map, "items", None) or [])
    }
    item_work_ids = {
        item.id: library_work_id(item)
        for item in plan.items
        if item.id
    }
    targets_by_work: Dict[str, List[Any]] = defaultdict(list)
    for target in build_scrape_targets(plan):
        work_ids = {
            item_work_ids[item_id]
            for item_id in target.item_ids
            if item_id in item_work_ids
        }
        for work_id in work_ids:
            targets_by_work[work_id].append(target)

    for work_id, work in works_map.items():
        targets = targets_by_work.get(work_id, [])
        work.metadata_state = (
            "ready"
            if targets and all(
                target_already_scraped(
                    target,
                    scrape_index,
                    include_episode=True,
                    plan=plan,
                )
                for target in targets
            )
            else "waiting_metadata"
        )


def _build_work_index(
    item: ImportPlanItem,
    library_work_id: str,
    scrape_work_index: Dict[str, dict],
    scrape_series_index: Dict[Tuple[str, str], List[dict]],
    asset_index: Dict[str, List[MirrorAsset]],
    import_scope: str = "",
) -> WorkIndex:
    """从 ImportPlanItem 构建 WorkIndex"""
    # 标题：主系列用 series_group 聚合；独立电影必须保留具体电影标题。
    if item.card_type == "main_series":
        title = _library_series_group(item) or item.series_group or item.work_title
    elif item.group_type == "movie":
        title = _specific_standalone_title(item)
    else:
        title = item.work_title or item.series_group
    original_title = item.original_title or ""

    # 从 scrape_map 补充展示信息（CP6：有 canonical 身份只按 canonical 精确取，
    # 绝不跨 canonical 通过 series_group / work_id 兜底借料；legacy 无 canonical
    # 的旧计划才保留 series_group / work_id 兼容 fallback）
    canonical_id = str(getattr(item, "canonical_work_id", "") or "")
    if item.card_type == "main_series":
        if canonical_id:
            scrape_info = scrape_work_index.get(canonical_id) or {}
        else:
            scrape_info = (
                scrape_work_index.get(library_work_id)
                or _select_series_scrape_info(item, scrape_series_index)
                or scrape_work_index.get(item.work_id)
                or {}
            )
    else:
        if canonical_id:
            scrape_info = scrape_work_index.get(canonical_id) or {}
        else:
            scrape_info = (
                scrape_work_index.get(library_work_id)
                or scrape_work_index.get(item.work_id)
                or {}
            )

    # 图片路径
    poster_path = scrape_info.get("poster_path", "")
    fanart_path = scrape_info.get("fanart_path", "")
    clearlogo_path = scrape_info.get("clearlogo_path", "")

    # 检查 asset 是否存在。远程图片 URL 是有效展示资源，不走本地 Path 检查。
    if poster_path and not _is_remote_asset_path(poster_path) and not Path(poster_path).exists():
        poster_path = ""
    if fanart_path and not _is_remote_asset_path(fanart_path) and not Path(fanart_path).exists():
        fanart_path = ""
    if clearlogo_path and not _is_remote_asset_path(clearlogo_path) and not Path(clearlogo_path).exists():
        clearlogo_path = ""

    # 如果 scrape_map 没有图片，尝试从 target_dir 及同系列目录查找。
    # 刮削图片可能落在 Season 1、电影目录或系列根目录，主系列卡片必须都能兜住。
    if item.target_dir:
        poster_path = poster_path or _find_asset_path(item.target_dir, "poster", asset_index)
        fanart_path = fanart_path or _find_asset_path(item.target_dir, "fanart", asset_index)
        clearlogo_path = clearlogo_path or _find_asset_path(item.target_dir, "clearlogo", asset_index)

    # NFO 解析
    nfo_info = {}
    nfo_path_value = scrape_info.get("nfo_path", "")
    if nfo_path_value:
        nfo_path = Path(nfo_path_value)
        if nfo_path.exists():
            nfo_info = _parse_nfo(nfo_path)
    elif item.target_dir:
        nfo_path = Path(item.target_dir) / ("tvshow.nfo" if item.media_type == "tv" else "movie.nfo")
        if nfo_path.exists():
            nfo_info = _parse_nfo(nfo_path)

    # 已写入 NFO 的标题来自最终确认后的本地化详情，应始终用于展示。
    # 独立 TV/外传仅在没有 NFO 时保留本地子作品名，避免未完成刮削时误用主系列标题。
    if nfo_info.get("title"):
        display_title = _strip_tmdb_hint(nfo_info["title"])
    elif _preserve_local_work_title(item):
        display_title = _strip_tmdb_hint(title)
    else:
        display_title = _strip_tmdb_hint(scrape_info.get("title") or title)
    display_original = _strip_tmdb_hint(nfo_info.get("originaltitle") or original_title)
    display_year = nfo_info.get("year") or item.year or scrape_info.get("year")
    display_plot = nfo_info.get("plot") or scrape_info.get("overview", "")
    rating = nfo_info.get("rating", 0.0)
    certification = nfo_info.get("mpaa", "")
    certification_country = nfo_info.get("certificationcountry", "")
    genres = nfo_info.get("genres", [])
    studios = nfo_info.get("studios", [])
    cast = nfo_info.get("cast", [])
    media_type = _normalize_media_type(item, scrape_info)
    show_type = _normalize_show_type(item, media_type)

    return WorkIndex(
        work_id=library_work_id,
        title=display_title,
        original_title=display_original,
        year=display_year,
        rating=rating,
        plot=display_plot,
        genres=genres,
        studios=studios,
        show_type=show_type,
        media_type=media_type,
        source=item.source,
        provider_id=getattr(item, "provider_id", "") or "",
        ingest_method=getattr(item, "ingest_method", "") or "",
        source_route_id=getattr(item, "source_route_id", "") or "",
        import_scope=import_scope,
        card_type=item.card_type,
        poster_path=poster_path,
        fanart_path=fanart_path,
        clearlogo_path=clearlogo_path,
        dir_path=item.target_dir,
        cast=cast,
        certification=certification,
        certification_country=certification_country,
    )


def _specific_standalone_title(item: ImportPlanItem) -> str:
    """Prefer the concrete title for standalone movie cards."""
    filename_title = _movie_filename_title(item.relative_path)
    for raw in (item.title, filename_title, item.original_title, item.work_title, item.series_group):
        title = _clean_standalone_movie_title(raw)
        if title:
            return title
    return item.work_title or item.series_group or ""


def _clean_standalone_movie_title(raw: str) -> str:
    """Clean release-folder noise while preserving the concrete movie title."""
    title = _strip_tmdb_hint(raw or "")
    if not title:
        return ""
    title = re.sub(r"^\d+[.．]\s*", "", title).strip()
    title = re.sub(r"[.．]\d{4}$", "", title).strip()
    title = re.sub(r"[(\（]\d{4}[)\）]$", "", title).strip()
    try:
        from app.recognition.title_cleaner import clean_work_title_container

        title = clean_work_title_container(title).title or title
    except Exception:
        title = re.sub(r"\[[^\]]*(?:sub|raws?|vcb|studio|nekomoe|ai\-?rota)[^\]]*\]", " ", title, flags=re.IGNORECASE)
        title = re.sub(r"\[[^\]]*(?:ma\d+p|hi\d+p|\d{3,4}p|x26[45]|hevc|avc|flac|aac|hdr|sdr|dovi)[^\]]*\]", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"^\d+[.．]\s*", "", title).strip()
    title = re.sub(r"\s+", " ", title).strip(" .-_")
    if re.sub(r"[\s._\-·:：/\\()（）【】\[\]]+", "", title).casefold() in {
        "剧场版", "movie", "movies", "映画", "电影",
    }:
        return ""
    return title


def _movie_filename_title(relative_path: str) -> str:
    filename = Path((relative_path or "").replace("\\", "/")).name
    stem = re.sub(r"\.[^.]+$", "", filename)
    tokens = [m.group(1).strip() for m in re.finditer(r"\[([^\]]+)\]", stem)]
    movie_positions = [
        idx for idx, token in enumerate(tokens)
        if re.fullmatch(r"movies?", token, flags=re.IGNORECASE)
    ]
    if not movie_positions:
        # 一些发布文件只在父目录以“剧场版”标注，文件名本身没有 [MOVIE]。
        # 此时去掉字幕组/技术标签后的主体仍是比分类目录可靠得多的片名。
        path_parts = (relative_path or "").replace("\\", "/").split("/")[:-1]
        generic_movie_dir_names = {"剧场版", "movie", "movies", "映画", "电影"}
        has_generic_movie_dir = any(
            re.sub(r"[\s._\-·:：/\\()（）【】\[\]]+", "", part).casefold()
            in generic_movie_dir_names
            for part in path_parts
        )
        if not has_generic_movie_dir:
            return ""
        plain = re.sub(r"[\[【][^\]】]*[\]】]", " ", stem)
        plain = plain.replace("_", " ")
        plain = re.sub(r"\s+", " ", plain).strip(" .-_")
        if re.search(r"[A-Za-z\u4e00-\u9fff]", plain):
            return plain
        return ""

    ordered_indexes = []
    for pos in movie_positions:
        ordered_indexes.extend([pos - 1, pos + 1])
    ordered_indexes.extend(range(len(tokens)))

    seen = set()
    for idx in ordered_indexes:
        if idx < 0 or idx >= len(tokens) or idx in seen:
            continue
        seen.add(idx)
        title = _clean_movie_filename_token(tokens[idx])
        if title:
            return title
    return ""


def _clean_movie_filename_token(token: str) -> str:
    cleaned = (token or "").replace("_", " ").strip()
    lower = cleaned.casefold()
    compact = re.sub(r"[\s._-]+", "", lower)
    tech_tokens = {
        "movie", "movies", "bdrip", "bluray", "bd", "webdl", "webrip",
        "chs", "cht", "jpn", "eng", "mp4", "mkv", "avc", "aac", "flac",
        "x264", "x265", "h264", "h265", "hevc", "hi10p", "ma10p",
    }
    if not cleaned or lower in tech_tokens or compact in tech_tokens:
        return ""
    if "&" in cleaned or "＆" in cleaned:
        return ""
    if re.fullmatch(r"\d+\s*[-~]\s*\d+(?:\s*\+\s*movies?)?", cleaned, flags=re.IGNORECASE):
        return ""
    if re.fullmatch(r"\d{3,4}p", cleaned, flags=re.IGNORECASE):
        return ""
    if re.search(r"\b(?:bdrip|bluray|web[- ]?dl|webrip|hevc|avc|aac|flac|x26[45]|h\.?26[45])\b", cleaned, flags=re.IGNORECASE):
        return ""
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", cleaned):
        return ""
    return " ".join(cleaned.split()).strip(" .-_")


def _preserve_local_work_title(item: ImportPlanItem) -> bool:
    return (
        item.card_type == "standalone"
        and item.media_type == "tv"
        and item.group_type != "movie"
        and bool(item.work_title)
    )


def _strip_tmdb_hint(title: str) -> str:
    cleaned = re.sub(r"\s*[\{\[]\s*(?:tmdb|tmdbid)\s*[-_=：:]?\s*\d+\s*[\}\]]\s*", " ", title or "", flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()


def _is_main_series_scrape_candidate(info: dict, series_group: str) -> bool:
    """判断刮削记录是否适合作为系列聚合卡片的标题和图片来源。"""
    if info.get("card_type") != "main_series":
        return False
    if info.get("source") != "local":
        return True

    local_title = _display_title_key(info.get("local_title") or "")
    source_subwork = _display_title_key(info.get("source_subwork_dir") or "")
    group_title = _display_title_key(series_group or "")
    concrete_title = local_title or source_subwork
    if not concrete_title or not group_title:
        return True
    if concrete_title == group_title:
        return True
    if concrete_title.startswith(group_title) and _looks_like_season_suffix(concrete_title[len(group_title):]):
        return True
    return False


def _looks_like_season_suffix(suffix: str) -> bool:
    normalized = (suffix or "").strip(" -_·.．:：/\\()[]（）【】")
    if not normalized:
        return True
    return bool(
        re.search(r"(?:season|s)\s*\d+", normalized, flags=re.IGNORECASE)
        or re.search(r"(?:第\s*\d+\s*季|第二季|第三季|第四季|第五季)", normalized)
    )


def _episode_sort_key(ep: EpisodeIndex) -> tuple:
    group_order = {"season": 0, "special": 1, "sps": 1, "auxiliary": 2, "movie": 3, "op_ed": 4}
    return (
        group_order.get(ep.group_type, 9),
        ep.season_number if ep.season_number is not None else 999,
        ep.episode_number if ep.episode_number is not None else 9999,
        ep.title or "",
        ep.episode_id,
    )


def _plan_work_has_target_strm(plan: ImportPlan, work_id: str) -> bool:
    for item in plan.items:
        if _library_work_id(item) == work_id and item.target_strm_path:
            return True
    return False


def _plan_work_has_primary_target_strm(plan: ImportPlan, work_id: str, work_id_resolver=None) -> bool:
    if work_id_resolver is None:
        work_id_resolver = _library_work_id
    for item in plan.items:
        if work_id_resolver(item) != work_id or not item.target_strm_path:
            continue
        if _effective_episode_group_type(item) in {"movie", "season", "special", "sps"}:
            return True
    return False


def _work_has_primary_episode(work: WorkIndex) -> bool:
    return any(
        ep.group_type in {"movie", "season", "special", "sps"}
        for ep in work.episodes
    )


def _select_series_scrape_info(
    item: ImportPlanItem,
    scrape_series_index: Dict[Tuple[str, str], List[dict]],
) -> dict:
    """为主系列聚合卡片选择同 series_group 的刮削信息。"""
    series_group = _library_series_group(item)
    if not series_group:
        return {}

    candidates = [
        info for info in scrape_series_index.get((item.source, series_group), [])
        if _is_main_series_scrape_candidate(info, series_group)
    ]
    if not candidates:
        return {}

    # 优先选择带海报的 TV season，其次任意带图条目，最后第一条。
    def score(info: dict) -> tuple:
        has_poster = 1 if _asset_path_available(info.get("poster_path", "")) else 0
        has_fanart = 1 if _asset_path_available(info.get("fanart_path", "")) else 0
        is_tv = 1 if info.get("tmdb_type") == "tv" else 0
        season = info.get("local_season_number") or 999
        return (has_poster, has_fanart, is_tv, -season)

    return max(candidates, key=score)


def _is_remote_asset_path(value: str) -> bool:
    return bool(re.match(r"^https?://", value or "", flags=re.IGNORECASE))


def _asset_path_available(value: str) -> bool:
    if not value:
        return False
    if _is_remote_asset_path(value):
        return True
    return Path(value).exists()


def _find_asset_path(
    target_dir: str,
    kind: str,
    asset_index: Dict[str, List[MirrorAsset]],
) -> str:
    """从 target_dir、系列根目录和同系列子目录查找 poster/fanart/clearlogo。"""
    if not target_dir:
        return ""

    start = Path(target_dir)
    candidate_dirs = [start]
    if start.name.lower().startswith("season ") or start.name in ("SPs", "OP-ED"):
        candidate_dirs.append(start.parent)
        try:
            candidate_dirs.extend([p for p in start.parent.iterdir() if p.is_dir()])
        except OSError:
            pass

    filenames = {
        "poster": ("poster.jpg", "poster.png", "poster.webp"),
        "fanart": ("fanart.jpg", "fanart.png", "fanart.webp"),
        "clearlogo": ("clearlogo.png", "clearlogo.svg", "clearlogo.webp"),
    }.get(kind, ())

    for directory in candidate_dirs:
        parent_key = str(directory)
        for asset in asset_index.get(parent_key, []):
            if asset.kind == kind and Path(asset.path).exists():
                return asset.path
        for name in filenames:
            path = directory / name
            if path.exists():
                return str(path)

    return ""


def _normalize_media_type(item: ImportPlanItem, scrape_info: Optional[dict] = None) -> str:
    """为 LibraryIndex 生成稳定的前端媒体类型。

    旧计划里部分 main_series 的 media_type 为空，前端分类会把它们过滤掉。
    LibraryIndex 是展示索引，可以在不改变 ImportPlan 主真相的前提下补展示默认值。
    """
    # 已经成功写入 ScrapeMap 的 TMDB 类型来自具体条目详情，比目录名、
    # standalone 卡片形态更可靠。standalone 只表示单独成卡，不等于电影。
    scraped_type = str((scrape_info or {}).get("tmdb_type", "")).casefold()
    if scraped_type in {"tv", "movie"}:
        return scraped_type
    if item.media_type:
        return item.media_type
    if item.group_type == "movie":
        return "movie"
    return "tv"


def _normalize_show_type(item: ImportPlanItem, media_type: str) -> str:
    """首页大类来自导入目录语义；老计划没有该字段时做兼容推断。"""
    existing = getattr(item, "show_type", "")
    if media_type == "movie" or item.group_type == "movie":
        if existing == "live_series":
            return "live_movie"
        if existing == "anime_series":
            return "anime_movie"
        if existing in {"anime_movie", "live_movie"}:
            return existing

    if media_type == "tv":
        if existing == "anime_movie":
            return "anime_series"
        if existing == "live_movie":
            return "live_series"

    if existing:
        return existing

    category = _import_category_from_path(item.relative_path, item.source)
    if category == "动画电影":
        return "anime_movie"
    if category in {"动画", "新番", "刮削好的动画", "番剧", "TV", "TV动画"}:
        return "anime_movie" if media_type == "movie" else "anime_series"
    if category == "剧集":
        return "live_series"
    if category == "电影":
        return "live_movie"
    if item.source == "local":
        return "anime_movie" if media_type == "movie" else "anime_series"
    if item.source == "baidu":
        return "anime_movie" if media_type == "movie" else "anime_series"
    return "live_movie" if media_type == "movie" else "live_series"


def _import_category_from_path(relative_path: str, source: str) -> str:
    categories = {"动画", "新番", "刮削好的动画", "动画电影", "电影", "剧集", "番剧", "TV", "TV动画"}
    parts = [p.strip() for p in (relative_path or "").replace("\\", "/").split("/") if p.strip()]
    if source == "local":
        for part in parts[:2]:
            if part in categories:
                return part
        return ""
    return parts[0] if parts and parts[0] in categories else ""


def _build_episode_index(item: ImportPlanItem, mf: MirrorFile, library_work_id: str) -> EpisodeIndex:
    """从 ImportPlanItem 和 MirrorFile 构建 EpisodeIndex"""
    title = item.title
    if not title and item.target_filename:
        # 去掉 .strm 后缀
        title = item.target_filename
        if title.lower().endswith(".strm"):
            title = title[:-5]

    nfo_path = ""
    thumb_path = ""
    nfo_info = {}
    group_type = _effective_episode_group_type(item)
    if group_type in {"season", "special", "sps"} and item.target_strm_path:
        nfo = _find_episode_nfo(
            strm_path=item.target_strm_path,
            season_number=item.season_number or 0,
            episode_number=item.episode_number or item.special_number or 0,
        )
        if nfo:
            nfo_path = str(nfo)
            nfo_info = _parse_nfo(nfo)
            title = nfo_info.get("title") or title
            thumb_path = nfo_info.get("thumb", "")

    return EpisodeIndex(
        episode_id=item.id,
        work_id=library_work_id,
        source=item.source,
        provider_id=getattr(item, "provider_id", "") or "",
        season_number=item.season_number or 0,
        episode_number=item.episode_number or item.special_number or 0,
        title=title,
        plot=nfo_info.get("plot", ""),
        runtime=nfo_info.get("runtime", 0),
        group_type="special" if group_type == "sps" else group_type,
        kind=_effective_kind(item),
        strm_path=item.target_strm_path,
        nfo_path=nfo_path,
        thumb_path=thumb_path,
        availability=getattr(item, "availability", "available"),
        metadata_pending=bool(nfo_info.get("metadatapending")) if nfo_path else True,
    )


def _effective_episode_group_type(item: ImportPlanItem) -> str:
    """兼容旧计划：把明显的附属视频从 S01E00/Season 中拉回 auxiliary。"""
    if item.group_type in {"season", "special", "sps", ""} and _looks_like_auxiliary_item(item):
        return "auxiliary"
    return item.group_type


def _effective_kind(item: ImportPlanItem) -> str:
    """从 ImportPlanItem 推导 kind（main / ova / sp / ncop / nced / pv / extra）

    用于 EpisodeIndex.kind，判断属于正片、OVA/SP 还是附属视频。
    """
    group_type = item.group_type
    if group_type in {"season", "movie", "main"}:
        return "main"
    if group_type in {"special", "sps"}:
        # 如果是附属视频（如 NCOP/NCED/PV），返回具体类型
        text = " ".join(
            part for part in (item.relative_path, item.target_filename, item.title) if part
        )
        if re.search(r"NCOP", text, re.IGNORECASE):
            return "ncop"
        if re.search(r"NCED", text, re.IGNORECASE):
            return "nced"
        if re.search(r"(?:^|[^A-Za-z])PV\d+", text, re.IGNORECASE):
            return "pv"
        return "sp"
    if group_type == "auxiliary":
        text = " ".join(
            part for part in (item.relative_path, item.target_filename, item.title) if part
        )
        if re.search(r"NCOP", text, re.IGNORECASE):
            return "ncop"
        if re.search(r"NCED", text, re.IGNORECASE):
            return "nced"
        if re.search(r"(?:^|[^A-Za-z])PV\d+", text, re.IGNORECASE):
            return "pv"
        return "extra"
    return ""


def _looks_like_auxiliary_item(item: ImportPlanItem) -> bool:
    text = " ".join(
        part for part in (
            item.relative_path,
            item.target_filename,
            item.title,
        )
        if part
    )
    if not text:
        return False
    patterns = (
        r"NCOP",
        r"NCED",
        r"NON[-\s]?CREDIT\s+(?:OP|ED)",
        r"(?:^|[^A-Za-z])OP\d+",
        r"(?:^|[^A-Za-z])ED\d+",
        r"\[PV\d*\]",
        r"(?:^|[^A-Za-z])PV\d+",
        r"\[CM\d*\]",
        r"(?:^|[^A-Za-z])CM\d+",
        r"\[MENU\d*\]",
        r"(?:^|[^A-Za-z0-9])MENU\d*(?:$|[^A-Za-z0-9])",
        r"TV\s*SPOT",
        r"TRAILER",
        r"EYECATCH",
        r"菜单|预告|花絮",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _find_episode_nfo(
    strm_path: str,
    season_number: int,
    episode_number: int,
) -> Optional[Path]:
    """查找分集 NFO。

    本地 season_number 可能与 TMDB season_number 不一致，例如本地 CLANNAD S2
    映射到 TMDB 的 After Story S1。因此优先按本地 SxxExx 找，找不到时在同目录
    查找任意 S??E同集号.nfo。
    """
    if not strm_path or episode_number <= 0:
        return None

    directory = Path(strm_path).parent
    exact = directory / f"S{season_number:02d}E{episode_number:02d}.nfo"
    if exact.exists():
        return exact

    pattern = f"S??E{episode_number:02d}.nfo"
    matches = sorted(directory.glob(pattern))
    for match in matches:
        if match.is_file():
            return match

    return None


def _build_season_indexes(
    work: WorkIndex,
    scrape_infos: List[dict],
    asset_index: Dict[str, List[MirrorAsset]],
) -> List[SeasonIndex]:
    """从 episodes 构建 SeasonIndex 列表"""
    season_map: Dict[Tuple[str, int], List[EpisodeIndex]] = defaultdict(list)
    episodes = work.episodes
    for ep in episodes:
        if ep.group_type == "movie":
            continue
        key = (ep.group_type, ep.season_number)
        season_map[key].append(ep)

    seasons = []
    for (gt, sn), eps in season_map.items():
        if gt == "season":
            label = f"第{sn}季"
        elif gt in {"special", "sps"}:
            label = "特别篇"
        elif gt == "auxiliary":
            label = "附属视频"
        elif gt == "op_ed":
            label = "OP/ED"
        else:
            label = gt

        season = SeasonIndex(
            season_id=f"{eps[0].work_id}_{gt}_{sn}",
            work_id=eps[0].work_id,
            season_number=sn,
            group_type=gt,
            label=label,
            episode_count=len(eps),
        )
        _enrich_season_from_scrape_map(season, work, scrape_infos, asset_index)
        seasons.append(season)

    # 排序：season 按 season_number，SPs 在后，附属视频不混入特别篇。
    def sort_key(s: SeasonIndex):
        order = {"season": 0, "special": 1, "sps": 1, "auxiliary": 2, "op_ed": 3}
        return (order.get(s.group_type, 9), s.season_number)

    seasons.sort(key=sort_key)
    return seasons


def _normalize_special_episode_numbers(episodes: List[EpisodeIndex]) -> None:
    """为合入同一卡片的特别篇提供唯一、稳定的前端位置。"""
    specials = [episode for episode in episodes if episode.group_type == "special"]
    explicit_numbers = [episode.episode_number for episode in specials if episode.episode_number > 0]
    used_numbers: set[int] = set()
    next_number = max(explicit_numbers, default=0) + 1
    for episode in specials:
        number = episode.episode_number
        if number <= 0 or number in used_numbers:
            while next_number in used_numbers:
                next_number += 1
            episode.episode_number = next_number
            next_number += 1
        used_numbers.add(episode.episode_number)


def _enrich_season_from_scrape_map(
    season: SeasonIndex,
    work: WorkIndex,
    scrape_infos: List[dict],
    asset_index: Dict[str, List[MirrorAsset]],
) -> None:
    """把 ScrapeMap/NFO 中的季级刮削信息挂到 SeasonIndex 上。

    LibraryIndex 的作品卡片按 series_group 聚合，但刮削是按 season/movie target
    执行的。这里把二者重新接起来，避免第二季仍显示第一季的简介和图片。
    """
    if season.group_type not in {"season", "special", "sps"}:
        return

    info = _select_season_scrape_info(scrape_infos, season.season_number)
    if not info:
        return

    season.scrape_target_id = info.get("scrape_target_id", "")
    season.scrape_title = info.get("scrape_title", "") or info.get("title", "")
    season.scrape_year = info.get("scrape_year") or info.get("year")
    season.tmdb_id = info.get("tmdb_id")
    season.tmdb_type = info.get("tmdb_type", "")
    season.tmdb_season_number = info.get("tmdb_season_number")
    season.nfo_path = info.get("nfo_path", "")
    season.poster_path = info.get("poster_path", "")
    season.fanart_path = info.get("fanart_path", "")
    season.clearlogo_path = info.get("clearlogo_path", "")
    season.scraped = bool(season.tmdb_id or season.nfo_path)

    nfo_info = _parse_nfo(Path(season.nfo_path)) if season.nfo_path else {}
    if nfo_info:
        # tvshow.nfo 的 title 通常是 TMDB 剧集总标题。对于聚合卡片下的
        # 多季/子作品，季标题必须优先保留 ScrapeMap 的 scrape_title，
        # 例如 CLANNAD Season 2 -> CLANNAD After Story。
        season.scrape_title = season.scrape_title or nfo_info.get("title", "")
        season.scrape_year = season.scrape_year or nfo_info.get("year")
        season.plot = nfo_info.get("plot", "")
        season.rating = nfo_info.get("rating", 0.0)

    target_dir = Path(season.nfo_path).parent if season.nfo_path else None
    if target_dir:
        season.poster_path = season.poster_path or _find_asset_path(str(target_dir), "poster", asset_index)
        season.fanart_path = season.fanart_path or _find_asset_path(str(target_dir), "fanart", asset_index)
        season.clearlogo_path = season.clearlogo_path or _find_asset_path(str(target_dir), "clearlogo", asset_index)

    if season.poster_path and not _is_remote_asset_path(season.poster_path) and not Path(season.poster_path).exists():
        season.poster_path = ""
    if season.fanart_path and not _is_remote_asset_path(season.fanart_path) and not Path(season.fanart_path).exists():
        season.fanart_path = ""
    if season.clearlogo_path and not _is_remote_asset_path(season.clearlogo_path) and not Path(season.clearlogo_path).exists():
        season.clearlogo_path = ""

    # 没有季级图片时回退到作品卡片图片，但保留季级标题/简介/TMDB 信息。
    season.poster_path = season.poster_path or work.poster_path
    season.fanart_path = season.fanart_path or work.fanart_path
    season.clearlogo_path = season.clearlogo_path or work.clearlogo_path
    season.plot = season.plot or work.plot
    season.rating = season.rating or work.rating


def _select_season_scrape_info(scrape_infos: List[dict], season_number: int) -> dict:
    """按本地季号选择对应 ScrapeMapItem。"""
    season_matches = [
        info for info in scrape_infos
        if info.get("tmdb_type") == "tv"
        and info.get("local_season_number") == season_number
    ]
    if not season_matches:
        return {}

    def score(info: dict) -> tuple:
        has_nfo = 1 if info.get("nfo_path") and Path(info["nfo_path"]).exists() else 0
        has_poster = 1 if _asset_path_available(info.get("poster_path", "")) else 0
        has_fanart = 1 if _asset_path_available(info.get("fanart_path", "")) else 0
        selected = 1 if info.get("selected_by") in {"auto", "manual", "review"} else 0
        return (has_nfo, has_poster, has_fanart, selected)

    return max(season_matches, key=score)


def _disambiguate_local_split_titles(works) -> None:
    """本地库允许按季度拆卡，同名卡片需要把季度写进标题。"""
    groups: Dict[str, List[WorkIndex]] = defaultdict(list)
    for work in works:
        if (
            work.source == "local"
            and work.card_type == "main_series"
            and work.media_type == "tv"
            and work.title
        ):
            groups[_display_title_key(work.title)].append(work)

    for dupes in groups.values():
        if len(dupes) < 2:
            continue
        for work in dupes:
            label = _primary_local_split_label(work)
            if label and not _title_has_split_label(work.title, label):
                work.title = f"{work.title} {label}"


def _display_title_key(title: str) -> str:
    return re.sub(r"\s+", "", title or "").casefold()


def _primary_local_split_label(work: WorkIndex) -> str:
    season_groups = [s for s in work.seasons if s.group_type == "season"]
    if len(season_groups) == 1:
        return season_groups[0].label or f"第{season_groups[0].season_number}季"
    if not season_groups and any(s.group_type in {"special", "sps"} for s in work.seasons):
        return "特别篇"
    return ""


def _title_has_split_label(title: str, label: str) -> bool:
    if not title or not label:
        return False
    compact = _display_title_key(title)
    if _display_title_key(label) in compact:
        return True
    match = re.match(r"第(\d+)季", label)
    if match:
        n = match.group(1)
        return bool(re.search(rf"(?:S0*{n}\b|Season\s*0*{n}\b|第\s*{n}\s*季)", title, re.IGNORECASE))
    return False


def _build_related_works(
    work: WorkIndex,
    plan: ImportPlan,
    works_map: Dict[str, WorkIndex],
    work_id_resolver=None,
) -> List[RelatedWork]:
    """构建不同卡片之间的 related_works。

    只使用 ImportPlan 里的显式系列关系，不做标题模糊匹配，避免误把无关作品连在一起。
    """
    relation_keys_by_work: Dict[str, set[str]] = defaultdict(set)
    relation_type_by_work: Dict[str, str] = {}
    title_keys_by_work: Dict[str, set[str]] = defaultdict(set)

    def add_key(work_id: str, key: str) -> None:
        key = _related_key(key)
        if key:
            relation_keys_by_work[work_id].add(key)

    def add_title_keys(work_id: str, *values: str) -> None:
        for value in values:
            for key in _related_title_keys(value):
                title_keys_by_work[work_id].add(key)

    if work_id_resolver is None:
        work_id_resolver = _library_work_id

    for item in plan.items:
        if item.resource_type != "video" or item.action != "generate_strm":
            continue
        library_id = work_id_resolver(item)
        if not library_id or library_id not in works_map:
            continue

        relation_type_by_work.setdefault(
            library_id,
            item.relation_type or ("main" if item.card_type == "main_series" else item.group_type),
        )

        if item.card_type == "main_series":
            add_key(library_id, library_id)
            add_key(library_id, item.work_id)
            add_key(library_id, item.series_group)
            add_key(library_id, _library_series_group(item))
        elif item.card_type == "standalone" and item.belongs_to_series:
            add_key(library_id, item.belongs_to_series)
        add_title_keys(library_id, item.work_title, item.original_title, item.series_group, item.belongs_to_series)

    for work_id, keys in title_keys_by_work.items():
        relation_keys_by_work[work_id].update(keys)

    current_keys = relation_keys_by_work.get(work.work_id, set())
    if not current_keys:
        return []

    related: List[RelatedWork] = []
    for other in works_map.values():
        if other.work_id == work.work_id:
            continue
        other_keys = relation_keys_by_work.get(other.work_id, set())
        if not current_keys.intersection(other_keys):
            continue
        related.append(RelatedWork(
            work_id=other.work_id,
            title=other.title,
            year=other.year,
            card_type=other.card_type,
            relation_type=relation_type_by_work.get(other.work_id, other.card_type),
            poster_path=other.poster_path,
            fanart_path=other.fanart_path,
            show_type=other.show_type,
        ))

    related.sort(key=lambda r: (
        _related_relation_order(r.relation_type),
        r.year if r.year is not None else 9999,
        r.title,
        r.work_id,
    ))
    return related


def rebuild_related_works_for_plan(works: List[WorkIndex], plan: ImportPlan) -> None:
    """用完整来源计划重建同系列关联。

    刮削完成后的局部刷新只会重建一张作品卡；此时必须重新读取完整计划里的
    ``series_group`` / ``belongs_to_series``，否则原本明确的文件夹系列关系会被截断。
    """
    works_map = {work.work_id: work for work in works if work.source == plan.source}
    work_id_by_dir = {
        _normalize_path(work.dir_path): work.work_id
        for work in works_map.values()
        if work.dir_path
    }
    plan_work_id = _plan_library_work_id_resolver(plan)

    def resolve_existing_work_id(item: ImportPlanItem) -> str:
        # 已刮削电影可能已升级为 TMDB 卡片 ID，而 ImportPlan 仍保留原始
        # 父目录 work_id。镜像目录是一部具体作品的稳定边界，可无歧义地
        # 把完整计划条目映射回当前卡片。
        by_dir = work_id_by_dir.get(_normalize_path(item.target_dir or ""))
        if by_dir:
            return by_dir
        return plan_work_id(item)

    relation_keys_by_work: Dict[str, set[str]] = defaultdict(set)
    relation_type_by_work: Dict[str, str] = {}
    title_keys_by_work: Dict[str, set[str]] = defaultdict(set)

    def add_key(work_id: str, value: str) -> None:
        key = _related_key(value)
        if key:
            relation_keys_by_work[work_id].add(key)

    def add_title_keys(work_id: str, *values: str) -> None:
        for value in values:
            title_keys_by_work[work_id].update(_related_title_keys(value))

    # 完整计划可能包含数千个分集。关系键只需构建一次，不能为每张作品卡
    # 重复扫描计划并解析同一批 Windows 路径。
    for item in plan.items:
        if item.resource_type != "video" or item.action != "generate_strm":
            continue
        library_id = resolve_existing_work_id(item)
        if not library_id or library_id not in works_map:
            continue

        relation_type_by_work.setdefault(
            library_id,
            item.relation_type or ("main" if item.card_type == "main_series" else item.group_type),
        )
        if item.card_type == "main_series":
            add_key(library_id, library_id)
            add_key(library_id, item.work_id)
            add_key(library_id, item.series_group)
            add_key(library_id, _library_series_group(item))
        elif item.card_type == "standalone" and item.belongs_to_series:
            add_key(library_id, item.belongs_to_series)
        add_title_keys(
            library_id,
            item.work_title,
            item.original_title,
            item.series_group,
            item.belongs_to_series,
        )

    for work_id, keys in title_keys_by_work.items():
        relation_keys_by_work[work_id].update(keys)

    work_ids_by_key: Dict[str, set[str]] = defaultdict(set)
    for work_id, keys in relation_keys_by_work.items():
        for key in keys:
            work_ids_by_key[key].add(work_id)

    for work in works_map.values():
        related_ids: set[str] = set()
        for key in relation_keys_by_work.get(work.work_id, set()):
            related_ids.update(work_ids_by_key.get(key, set()))
        related_ids.discard(work.work_id)

        related = [
            RelatedWork(
                work_id=other.work_id,
                title=other.title,
                year=other.year,
                card_type=other.card_type,
                relation_type=relation_type_by_work.get(other.work_id, other.card_type),
                poster_path=other.poster_path,
                fanart_path=other.fanart_path,
                show_type=other.show_type,
            )
            for other_id in related_ids
            if (other := works_map.get(other_id)) is not None
        ]
        related.sort(key=lambda item: (
            _related_relation_order(item.relation_type),
            item.year if item.year is not None else 9999,
            item.title,
            item.work_id,
        ))
        work.related_works = related


def _related_key(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return _display_title_key(_strip_tmdb_hint(value))


def _related_title_keys(value: str) -> set[str]:
    """提取 related_works 的标题兜底 key。"""
    keys: set[str] = set()
    for candidate in (value or "", _strip_related_suffixes(value or "")):
        key = _related_key(candidate)
        if key:
            keys.add(key)
    return keys


def _strip_related_suffixes(title: str) -> str:
    """去掉剧场版 / 特别篇 / 季号等会阻断相关作品匹配的后缀。"""
    cleaned = _strip_tmdb_hint(title)
    cleaned = re.sub(r"\s*[\[(（【][^\])）】]*[\])）】]\s*$", " ", cleaned)
    cleaned = re.sub(
        r"\s*(?:剧场版|电影版|电影|動畫电影|动画电影|movie|film|ova|oad|oav|特别篇|番外篇|总集篇|前篇|后篇|上篇|下篇|sp|specials?)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s*(?:第\s*\d+\s*季|season\s*\d+|s\d{1,2})\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.split()).strip()


def _related_relation_order(relation_type: str) -> int:
    return {
        "main": 0,
        "movie": 1,
        "recap": 2,
        "spin_off": 3,
        "related": 4,
    }.get(relation_type or "", 9)


def _mirror_work_root(item: ImportPlanItem) -> str:
    """从镜像目标目录推导作品根（去掉 Season 结构段）。

    镜像目录按 series_group 命名，同一作品的多集/多季共享同一作品根，
    因此 TXT（带分类层）与 OpenList（无分类层）两种路径形态都稳定。
    """
    directory = Path(item.target_dir) if item.target_dir else None
    if directory is None and item.target_strm_path:
        directory = Path(item.target_strm_path).parent
    if directory is None:
        return ""
    if item.group_type in {"season", "special", "sps"} and re.fullmatch(
        r"(?:Season\s*\d+|S\d+|SPs)", directory.name, flags=re.IGNORECASE
    ):
        directory = directory.parent
    return f"{item.source}:{_normalize_path(str(directory))}"


def _library_work_id(item: ImportPlanItem) -> str:
    """Generate one card identity for one source-side mirror directory."""
    if getattr(item, "canonical_work_id", ""):
        return item.canonical_work_id
    # 主系列按 series_group 聚合（同一系列的季/特殊内容合并一张卡），
    # 不依赖镜像目录（target_dir 在镜像前为空，resolver 无法合并）。
    # 仅非 local 适用：local 走下方路径段逻辑（合集根合并、独立根分开）。
    if item.card_type == "main_series" and item.source != "local" and item.series_group:
        content = f"{item.source}:series:{item.series_group}"
        return "work_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    card_identity = library_card_identity(item)
    if card_identity:
        content = f"{item.source}:directory:{card_identity}"
        return "work_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    fallback = item.work_id or _library_series_group(item) or item.work_title
    if not fallback:
        return ""
    content = f"{item.source}:fallback:{fallback}"
    return "work_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]


def _plan_library_work_id_resolver(plan: ImportPlan) -> Callable[[ImportPlanItem], str]:
    """将同一镜像作品根的季度挂到正季原有卡片 ID，保持历史关联稳定。"""
    primary_by_root: dict[str, tuple[tuple[int, int, str], str]] = {}
    for item in plan.items:
        root = _main_series_mirror_root(item)
        if not root or item.group_type != "season":
            continue
        work_id = _library_work_id(item)
        season_number = item.season_number if item.season_number is not None else 999
        rank = (season_number != 1, season_number, work_id)
        current = primary_by_root.get(root)
        if current is None or rank < current[0]:
            primary_by_root[root] = (rank, work_id)

    def resolve(item: ImportPlanItem) -> str:
        root = _main_series_mirror_root(item)
        if root and root in primary_by_root:
            return primary_by_root[root][1]
        return _library_work_id(item)

    return resolve


def _main_series_mirror_root(item: ImportPlanItem) -> str:
    if (
        item.source == "local"
        or item.card_type != "main_series"
        or item.group_type not in {"season", "special", "sps"}
    ):
        return ""
    directory = Path(item.target_dir) if item.target_dir else None
    if directory is None and item.target_strm_path:
        directory = Path(item.target_strm_path).parent
    if directory is None or not re.fullmatch(
        r"(?:Season\s*\d+|S\d+|SPs)", directory.name, flags=re.IGNORECASE
    ):
        return ""
    return f"{item.source}:{_normalize_path(str(directory.parent))}"


def _library_series_group(item: ImportPlanItem) -> str:
    """LibraryIndex 使用的系列聚合名。

    本地库可能已经存在旧计划：每季的 series_group 分别是
    "Yuru Camp"、"Yuru Camp Season 2"。展示索引应把它们合成一个
    系列卡片；新导入则优先使用本地合集根目录。
    """
    if item.source != "local":
        return item.series_group
    collection = _local_collection_series_group(item.relative_path)
    if collection:
        return collection
    return _strip_series_suffix(item.series_group)


def _library_series_group_from_scrape_item(item) -> str:
    if getattr(item, "source", "") != "local":
        return getattr(item, "series_group", "")
    # ScrapeMapItem 没有 relative_path，只能从已有 series_group/source_subwork_dir 兼容旧数据。
    return _strip_series_suffix(getattr(item, "series_group", "") or getattr(item, "source_subwork_dir", ""))


def _library_tmdb_identity(tmdb_id: object, tmdb_type: str) -> str:
    normalized_type = (tmdb_type or "").casefold()
    if tmdb_id and normalized_type in {"tv", "movie"}:
        return f"tmdb:{normalized_type}:{tmdb_id}"
    return ""


def _primary_episode_key(item_work_id: str, item: ImportPlanItem) -> Tuple[str, str, int, int] | None:
    if item.group_type == "movie":
        # 同一电影的不同发布版本已按 TMDB 聚合时，详情页只保留一个稳定的
        # 默认播放入口，避免电影卡片重新出现“两个一集”的伪剧集列表。
        return (item_work_id, "movie", 0, 0)
    if item.group_type != "season":
        return None
    if item.season_number is None or item.episode_number is None:
        return None
    if item.season_number < 0 or item.episode_number <= 0:
        return None
    return (item_work_id, item.group_type, item.season_number, item.episode_number)


def _episode_variant_sort_key(item: ImportPlanItem) -> tuple:
    """让非副本目录优先，保证重复剧集展示结果不依赖计划条目顺序。"""
    label = " ".join((item.work_title or item.series_group or "").split())
    is_copy = bool(re.search(r"\s*[（(]\d+[)）]\s*$", label))
    # ``real_path`` 可以是当前不可用的网盘挂载盘符；这里只需要稳定排序，
    # 不应访问该盘符，更不能让索引重建因为盘符离线而失败。
    normalized_path = os.path.normcase(os.path.normpath(item.real_path or ""))
    return (1 if is_copy else 0, normalized_path.casefold())


def _local_collection_series_group(relative_path: str) -> str:
    parts = [p for p in (relative_path or "").replace("\\", "/").split("/") if p]
    if len(parts) < 3:
        return ""
    first = parts[0]
    lower = first.lower()
    is_collection = (
        (first.startswith("[") and "]" in first)
        or "vcb-studio" in lower
        or "collection" in lower
        or "合集" in first
        or "系列" in first
    )
    if not is_collection:
        return ""
    try:
        from app.recognition.title_cleaner import clean_work_title_container
        return clean_work_title_container(first).title
    except Exception:
        return _strip_series_suffix(first)


def _strip_series_suffix(title: str) -> str:
    """去掉标题里的季号后缀，只用于本地系列聚合 key。"""
    cleaned = (title or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s*\[[^\]]*(?:Ma\d+p|1080p|2160p|720p|x26[45]|HEVC|AVC|BDRip|WebRip)[^\]]*\]\s*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", cleaned)
    cleaned = re.sub(r"[.．]\d{4}\s*$", "", cleaned)
    patterns = [
        r"\s*(?:第\s*\d+\s*季|第[一二三四五六七八九十]+季)\s*$",
        r"\s*(?:Season|S)\s*\d+\s*$",
        r"\s*\d+(?:st|nd|rd|th)\s+Season\s*$",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return " ".join(cleaned.split()).strip() or title


def _parse_nfo(nfo_path: Path) -> dict:
    """解析 NFO 文件"""
    try:
        tree = ET.parse(str(nfo_path))
        root = tree.getroot()
        info = {}
        for tag in (
            "title", "originaltitle", "year", "plot", "tmdbid", "rating",
            "runtime", "thumb", "mpaa", "certificationcountry", "metadatapending",
        ):
            elem = root.find(tag)
            if elem is not None and elem.text:
                val = elem.text.strip()
                if tag == "year":
                    try:
                        info[tag] = int(val)
                    except ValueError:
                        pass
                elif tag == "runtime":
                    try:
                        info[tag] = int(float(val))
                    except ValueError:
                        pass
                elif tag == "rating":
                    try:
                        info[tag] = float(val)
                    except ValueError:
                        pass
                elif tag == "metadatapending":
                    info[tag] = val.casefold() in {"1", "true", "yes"}
                elif tag == "thumb" and re.fullmatch(r"/[A-Za-z0-9_-]+\.(?:jpe?g|png|webp)", val, re.IGNORECASE):
                    info[tag] = f"https://image.tmdb.org/t/p/original{val}"
                else:
                    info[tag] = val
        genres = [
            elem.text.strip() for elem in root.findall("genre")
            if elem is not None and elem.text and elem.text.strip()
        ]
        studios = [
            elem.text.strip() for elem in root.findall("studio")
            if elem is not None and elem.text and elem.text.strip()
        ]
        if genres:
            info["genres"] = genres
        if studios:
            info["studios"] = studios
        cast = []
        for actor in root.findall("actor"):
            name = (actor.findtext("name") or "").strip()
            if not name:
                continue
            cast.append({
                "name": name,
                "role": (actor.findtext("role") or "").strip(),
                "profile_path": (actor.findtext("thumb") or "").strip(),
            })
        if cast:
            info["cast"] = cast
        return info
    except Exception:
        return {}


def _build_source_summary(
    plan: ImportPlan,
    scan_result: Optional[MirrorScanResult],
    works_map: Dict[str, WorkIndex],
    missing_strm: int,
    orphan_strm: int,
    scraped_work_count: int = 0,
) -> dict:
    """构建 source_summary"""
    strm_count = (
        sum(1 for mf in scan_result.strm_files if mf.source == plan.source)
        if scan_result else 0
    )
    episode_count = sum(len(w.episodes) for w in works_map.values())

    poster_count = sum(1 for w in works_map.values() if w.poster_path)
    fanart_count = sum(1 for w in works_map.values() if w.fanart_path)
    clearlogo_count = sum(1 for w in works_map.values() if w.clearlogo_path)

    return {
        plan.source: {
            "work_count": len(works_map),
            "episode_count": episode_count,
            "strm_count": strm_count,
            "missing_strm_count": missing_strm,
            "orphan_strm_count": orphan_strm,
            "scraped_work_count": scraped_work_count,
            "poster_count": poster_count,
            "fanart_count": fanart_count,
            "clearlogo_count": clearlogo_count,
            "warnings": [],
        }
    }


def _scrape_item_to_dict(item) -> dict:
    """ScrapeMapItem 转 dict"""
    return {
        "scrape_target_id": item.scrape_target_id,
        "work_id": item.work_id,
        "canonical_work_id": getattr(item, "canonical_work_id", "") or "",
        "source": item.source,
        "import_plan_id": item.import_plan_id,
        "card_type": item.card_type,
        "media_type": item.media_type,
        "series_group": item.series_group,
        "local_title": item.local_title,
        "original_title": item.original_title,
        "source_subwork_dir": item.source_subwork_dir,
        "local_year": item.local_year,
        "local_season_number": item.local_season_number,
        "tmdb_id": item.tmdb_id,
        "tmdb_type": item.tmdb_type,
        "tmdb_season_number": item.tmdb_season_number,
        "title": item.scrape_title or item.local_title,
        "scrape_title": item.scrape_title,
        "year": item.scrape_year or item.local_year,
        "scrape_year": item.scrape_year,
        "selected_by": item.selected_by,
        "overview": "",
        "poster_path": item.poster_path,
        "fanart_path": item.fanart_path,
        "clearlogo_path": item.clearlogo_path,
        "nfo_path": item.nfo_path,
    }


def _normalize_path(p: str) -> str:
    """纯词法路径归一化，不探测可能离线的盘符或网络位置。"""
    return os.path.abspath(p).replace("\\", "/")


def _now() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).isoformat()
