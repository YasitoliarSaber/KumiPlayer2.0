# -*- coding: utf-8 -*-
"""自动刮削服务

保守策略：高置信度自动采用，低置信度进入 review_queue。
拒绝自动采用：needs_review、低分候选、类型不匹配、年份差距大。
"""

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.scrape.models import ScrapeCandidate, ScrapeTarget
from app.scrape.completeness import target_already_scraped
from app.scrape.service import execute_scrape, resolve_tmdb_season_number, search_candidates
from app.scrape.store import build_failed_case, load_scrape_map, save_failed_case  # noqa: F401  # load_scrape_map 保留为外部 monkeypatch/兼容目标
from app.scrape.tmdb_client import TMDBClient
from app.scrape.validator import blocking_issues, issue_messages, validate_scrape_metadata
from app.tasks.logs import append_task_log
from app.tasks.models import TaskCancelledError

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _find_series_tmdb_id(target: ScrapeTarget, scrape_map) -> Optional[int]:
    """从已确认的同系列 TV 映射中查找唯一 TMDB ID。"""
    binding = _find_series_binding(target, scrape_map)
    return binding[0] if binding else None


def _find_series_binding(target: ScrapeTarget, scrape_map) -> Optional[tuple[int, Optional[int]]]:
    """查找唯一系列 ID，并优先返回相同本地季的已确认 TMDB 季号。"""
    if not target.series_group:
        return None
    if target.scrape_type != "tv" or target.card_type != "main_series":
        return None
    matched_ids: set[int] = set()
    exact_season_bindings: set[tuple[int, Optional[int]]] = set()
    target_key = _series_binding_key(target.series_group)
    for item in scrape_map.items:
        item_key = _series_binding_key(getattr(item, "series_group", ""))
        same_series = item_key == target_key
        if not same_series and (target.local_season_number or 1) > 1:
            same_series = _series_binding_key_with_season_suffix(target.series_group) == item_key
        if (
            same_series
            and getattr(item, "card_type", "") == "main_series"
            and item.tmdb_id
            and item.tmdb_type == "tv"
            and _series_map_identity_is_usable(target, item)
        ):
            matched_ids.add(int(item.tmdb_id))
            if item.local_season_number == target.local_season_number:
                exact_season_bindings.add((int(item.tmdb_id), item.tmdb_season_number))
    if len(matched_ids) != 1:
        return None
    tmdb_id = next(iter(matched_ids))
    matching_exact = {binding for binding in exact_season_bindings if binding[0] == tmdb_id}
    if len(matching_exact) == 1:
        return next(iter(matching_exact))
    return tmdb_id, None


def _series_map_identity_is_usable(target: ScrapeTarget, item) -> bool:
    nfo_path = getattr(item, "nfo_path", "")
    if not nfo_path or not Path(nfo_path).exists():
        return True
    try:
        root = ET.parse(nfo_path).getroot()
    except (ET.ParseError, OSError):
        return False
    candidate = ScrapeCandidate(
        tmdb_type="tv",
        title=(root.findtext("title") or "").strip(),
        original_title=(root.findtext("originaltitle") or "").strip(),
    )
    map_target = ScrapeTarget(
        scrape_type="tv",
        scrape_title=getattr(item, "scrape_title", ""),
        local_title=getattr(item, "local_title", ""),
        series_group=getattr(item, "series_group", ""),
        original_title=getattr(item, "original_title", ""),
    )
    title_safe, _ = _candidate_title_identity_safe(map_target, candidate)
    if title_safe:
        return True
    evidence = getattr(item, "identity_evidence", None) or {}
    evidence_candidate = ScrapeCandidate(
        provider=str(evidence.get("provider") or ""),
        tmdb_type="tv",
        title=str(evidence.get("candidate_title") or candidate.title),
        original_title=str(evidence.get("candidate_original_title") or candidate.original_title),
        raw={
            "provider_title_aliases": list(evidence.get("provider_title_aliases") or []),
            "provider_tmdb_link": evidence.get("provider_tmdb_link") or "",
        },
    )
    return _candidate_has_trusted_provider_identity(map_target, evidence_candidate)


def _series_binding_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff\u3040-\u30ff]+", "", (value or "").casefold())


def _series_binding_key_with_season_suffix(value: str) -> str:
    """仅移除明确的末尾季度数字；是否可用还由目标季号约束。"""
    key = _series_binding_key(value)
    return re.sub(r"(?:season|s)?[2-9]$", "", key)


def _build_candidate_from_series(
    target: ScrapeTarget,
    tmdb_id: int,
    client: TMDBClient,
    confirmed_tmdb_season_number: Optional[int] = None,
) -> Tuple[Optional[ScrapeCandidate], Optional[int]]:
    """用同系列的 tmdb_id 直接构建候选，跳过搜索

    返回: (candidate, tmdb_season_number) 或 (None, None)
    """
    from dataclasses import replace as dc_replace
    from app.scrape.service import _tmdb_result_to_candidate

    try:
        detail = client.get_tv_detail(tmdb_id)
    except Exception as e:
        logger.warning("获取同系列 TMDB 详情失败: tmdb_id=%s, %s", tmdb_id, e)
        return None, None

    seasons = detail.get("seasons") or []
    local_season = 0 if target.group_type == "special" else (target.local_season_number or 1)

    # 检查 TMDB 是否有对应的 season_number
    matched_season = None
    if confirmed_tmdb_season_number is not None:
        matched_season = next(
            (s for s in seasons if s.get("season_number") == confirmed_tmdb_season_number),
            None,
        )
    for s in seasons:
            if matched_season is None and s.get("season_number") == local_season:
                matched_season = s
                break

    # 如果精确匹配失败，用年份匹配
    if not matched_season and target.scrape_year:
        for s in seasons:
            air_date = s.get("air_date") or ""
            if air_date[:4].isdigit() and int(air_date[:4]) == target.scrape_year:
                matched_season = s
                break

    if not matched_season:
        return None, None

    tmdb_season_number = matched_season.get("season_number", local_season)

    # 构建候选
    fake_result = {
        "id": tmdb_id,
        "name": detail.get("name") or target.scrape_title,
        "original_name": detail.get("original_name") or "",
        "first_air_date": detail.get("first_air_date") or "",
        "overview": detail.get("overview") or "",
        "popularity": detail.get("popularity") or 0,
        "vote_average": detail.get("vote_average") or 0,
        "vote_count": detail.get("vote_count") or 0,
        "genre_ids": [],
        "origin_country": detail.get("origin_country") or [],
    }
    candidate = _tmdb_result_to_candidate(fake_result, target, "tv")
    candidate = dc_replace(candidate, score=100.0)
    candidate.reasons = ["同系列复用（已确认 TMDB ID）"]
    return candidate, tmdb_season_number


def _should_reuse_series_scrape(target: ScrapeTarget) -> bool:
    """Return whether a target may inherit an already confirmed TV series id."""
    if target.scrape_type != "tv" or target.card_type != "main_series":
        return False
    if not target.series_group:
        return False
    return target.group_type in {"season", "special"}


def decide_auto_candidate(
    target: ScrapeTarget,
    candidates: List[ScrapeCandidate],
    threshold: float = 70,
) -> Tuple[Optional[ScrapeCandidate], str]:
    """决定是否自动采用候选

    返回: (candidate_or_None, reason)

    拒绝自动采用的条件：
    - 无候选
    - 候选类型不匹配
    - target 标题明显是 Season 1 / movie 等脏标题
    - TV 第一季候选存在明确年份冲突
    - candidate.tmdb_type != target.scrape_type
    """
    # 无候选
    if not candidates:
        return None, "无候选"

    # 候选分数是排序信号，不是作品身份的证明。真实目录树里经常会出现
    # “路人”等共用词把错误条目推到第一名，因此 TV 优先选择已经通过
    # 标题身份校验的候选；没有可靠身份候选时才保留最高分供人工确认。
    ranked_candidates = sorted(candidates, key=lambda c: (c.score, c.popularity), reverse=True)
    best = ranked_candidates[0]
    if target.scrape_type == "tv":
        for candidate in ranked_candidates:
            if candidate.tmdb_type != target.scrape_type:
                continue
            if (
                _is_tmdb_hint_match(target, candidate)
                or _candidate_identity_safe(target, candidate)[0]
            ):
                best = candidate
                break
    elif target.scrape_type == "movie":
        # “前篇/后篇”等分部标识是作品身份约束，而不是普通的评分项。
        # 搜索器已写入该证据；此处必须在采用前排除反向篇章，避免热度或
        # 分数略高的同系列另一部电影覆盖正确候选。
        matching_part_candidates = [
            candidate
            for candidate in ranked_candidates
            if candidate.tmdb_type == target.scrape_type
            and not _candidate_has_part_mismatch(candidate)
        ]
        if matching_part_candidates:
            best = matching_part_candidates[0]

    # 类型不匹配
    if best.tmdb_type != target.scrape_type:
        return None, f"类型不匹配: {best.tmdb_type} != {target.scrape_type}"

    if _is_tmdb_hint_match(target, best):
        return best, "TMDB ID 命中，自动采用"

    if _is_generic_scrape_title(target):
        return None, "target 需要人工确认"

    year_conflict, year_reason = _has_strict_year_conflict(target, best)
    if year_conflict:
        return None, year_reason

    if target.scrape_type == "movie":
        candidate_safe, safe_reason = _movie_candidate_safe_for_auto(target, best)
    else:
        candidate_safe, safe_reason = _tv_candidate_safe_for_auto(target, best, threshold, len(candidates))
    if not candidate_safe:
        return None, safe_reason

    if len(candidates) == 1:
        return best, "唯一候选且类型匹配，自动采用"
    return best, "最高分候选，自动采用"


def _tv_candidate_safe_for_auto(
    target: ScrapeTarget,
    candidate: ScrapeCandidate,
    threshold: float,
    candidate_count: int,
) -> Tuple[bool, str]:
    if target.scrape_type != "tv":
        return True, ""
    # Bangumi 命中不是标题身份的替代证明。Bangumi 模糊搜索可能返回标题
    # 接近但实际是完全不同作品的条目（如“明日同学的水手服”→“明日的与一”）。
    # 候选仍须经过标题身份校验；只有 {tmdb-...} ID 强绑定可跳过。
    if _is_tmdb_hint_match(target, candidate):
        return True, "TMDB ID 命中，自动采用"
    if _candidate_title_exactly_matches_target(target, candidate):
        return True, ""
    title_safe, title_reason = _candidate_identity_safe(target, candidate)
    if title_safe:
        return True, ""
    return False, title_reason


def _candidate_identity_safe(
    target: ScrapeTarget,
    candidate: ScrapeCandidate,
) -> Tuple[bool, str]:
    title_safe, reason = _candidate_title_identity_safe(target, candidate)
    if title_safe:
        return True, reason
    if _candidate_has_trusted_provider_identity(target, candidate):
        return True, "可信元数据提供方别名链"
    return False, reason


def _candidate_title_identity_safe(
    target: ScrapeTarget, candidate: ScrapeCandidate
) -> Tuple[bool, str]:
    """独立校验作品标题身份，综合分、年份和热度不能绕过这一关。"""
    target_titles = {
        _normalize_identity_title(target.scrape_title),
        _normalize_identity_title(target.local_title),
        _normalize_identity_title(target.series_group),
        _normalize_identity_title(target.original_title),
    } - {""}
    candidate_titles = {
        _normalize_identity_title(candidate.title),
        _normalize_identity_title(candidate.original_title),
    } - {""}
    for expected in target_titles:
        for actual in candidate_titles:
            if expected == actual:
                return True, "标题完全一致"

            if (
                target.scrape_type == "movie"
                and candidate.tmdb_type == "movie"
                and _movie_descriptor_signature(expected)
                and _movie_descriptor_signature(expected) == _movie_descriptor_signature(actual)
            ):
                return True, "动画电影篇章标识一致"

            shorter, longer = sorted((expected, actual), key=len)
            containment = len(shorter) / len(longer) if shorter and shorter in longer else 0.0
            similarity = SequenceMatcher(None, expected, actual).ratio()
            cjk = bool(re.search(r"[\u3400-\u9fff]", expected + actual))

            # TMDB 有时把罗马音放在主标题、日文放在 original_title，目录却
            # 使用中文译名。例如“天元突破”与“天元突破グレンラガン”。四个以上
            # 的中日韩字符前缀足以表达稳定作品名，不接受短词或中间子串。
            # 注意：“异世界”仅 3 字，低于 4 字阈值，不会误配。
            if cjk and len(expected) >= 4 and actual.startswith(expected):
                return True, "中日韩标题前缀一致"

            # 中日韩标题只允许接近完整的别名/副标题包含关系；共享少量常见字符不能证明是同一作品。
            if containment >= 0.82 and len(shorter) >= (4 if cjk else 5):
                return True, "标题主体一致"
            if similarity >= (0.86 if cjk else 0.82) and min(len(expected), len(actual)) >= 5:
                return True, "标题高度一致"

    return False, "TV 候选标题不够明确：候选与本地作品标题不是同一作品，需人工确认"


def _normalize_identity_title(value: str) -> str:
    """归一化候选身份标题，同时统一常见动画电影与简繁体写法。"""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(
        r"the\s*motion\s*picture|劇場版|剧场版|電影版|电影版|映画",
        "movie",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = normalized.translate(str.maketrans({"戰": "战", "場": "场", "劇": "剧"}))
    return re.sub(r"[^0-9a-z\u3400-\u9fff\u3040-\u30ff]+", "", normalized)


def _movie_descriptor_signature(normalized_title: str) -> str:
    """提取“新剧场版：序/破”这类跨语言仍稳定的电影篇章片段。"""
    marker = "movie"
    marker_index = normalized_title.find(marker)
    if marker_index < 0:
        return ""
    before = normalized_title[max(0, marker_index - 1):marker_index]
    after = normalized_title[marker_index + len(marker):marker_index + len(marker) + 1]
    if len(before + after) < 2:
        return ""
    return f"{before}{marker}{after}"


def _candidate_title_exactly_matches_target(
    target: ScrapeTarget, candidate: ScrapeCandidate
) -> bool:
    """直接核对标题字段，避免评分器漏写 reasons 时误拦正确候选。"""
    targets = {
        _normalize_identity_title(target.scrape_title),
        _normalize_identity_title(target.local_title),
        _normalize_identity_title(target.series_group),
    }
    candidates = {
        _normalize_identity_title(candidate.title),
        _normalize_identity_title(candidate.original_title),
    }
    targets.discard("")
    candidates.discard("")
    return bool(targets & candidates)


def _candidate_has_title_evidence(candidate: ScrapeCandidate) -> bool:
    title_reasons = (
        "标题完全匹配",
        "标题部分匹配",
        "原名部分匹配",
        "候选标题前缀匹配",
        "标题高度相似",
        "标题相似",
        "同系列复用",
    )
    return any(any(marker in reason for marker in title_reasons) for reason in candidate.reasons)


def _candidate_has_part_mismatch(candidate: ScrapeCandidate) -> bool:
    """Return whether search scoring identified an opposite movie part."""
    return any("篇章不匹配" in reason for reason in candidate.reasons)


def _is_latin_search_title(title: str) -> bool:
    text = title or ""
    return bool(re.search(r"[A-Za-z]", text)) and not bool(re.search(r"[\u3400-\u9fff]", text))


def _movie_candidate_safe_for_auto(
    target: ScrapeTarget,
    candidate: ScrapeCandidate,
) -> Tuple[bool, str]:
    """Movie auto-scrape needs stronger evidence than TV.

    Series movie cards are often one-file entries whose folder contains a
    concrete subtitle. A generic candidate for the parent TV series must not be
    auto-applied to several movie cards.
    """
    target_year = target.scrape_year or target.local_year
    if _is_tmdb_hint_match(target, candidate):
        return True, "TMDB ID 命中，自动采用"
    if _candidate_has_part_mismatch(candidate):
        return False, "电影篇章不匹配，需人工确认"
    if target_year and candidate.year and abs(target_year - candidate.year) > 1:
        return False, f"电影候选年份不匹配: {candidate.year} != {target_year}"

    identity_safe, identity_reason = _candidate_identity_safe(target, candidate)
    if not identity_safe:
        return False, identity_reason.replace("TV 候选", "电影候选")

    return True, "电影候选匹配明确，自动采用"


def _is_tmdb_hint_match(target: ScrapeTarget, candidate: ScrapeCandidate) -> bool:
    return bool(target.tmdb_hint_id and candidate.tmdb_id == target.tmdb_hint_id)


def _has_strict_year_conflict(
    target: ScrapeTarget,
    candidate: ScrapeCandidate,
) -> Tuple[bool, str]:
    """唯一候选时的年份冲突判断。

    用户期望：
    - 只有一个候选时，默认就采用；
    - 但如果年份明确不一致，就不要自动确定；
    - 如果年份一致但 score 偏低，也照样采用，减少无意义人工确认。
    """
    target_year = target.scrape_year or target.local_year
    candidate_year = candidate.year
    if target.scrape_type == "movie" and candidate.tmdb_type == "movie":
        return False, ""
    if (
        target.scrape_type == "tv"
        and candidate.tmdb_type == "tv"
        and (target.local_season_number or 1) > 1
    ):
        return False, ""
    if target_year and candidate_year and target_year != candidate_year:
        # 网盘目录的年份经常来自压制/发布年份，而 TMDB 使用首播年份。
        # 对第一季仍保留严格的身份校验；只有标题证据明确且刚好跨一年时
        # 才放行，避免像《伪恋》这类正确候选被无意义地漏刮。
        if (
            target.scrape_type == "tv"
            and candidate.tmdb_type == "tv"
            and abs(target_year - candidate_year) == 1
            and _candidate_has_title_evidence(candidate)
        ):
            return False, ""
        return True, f"唯一候选但年份不一致: {candidate_year} != {target_year}"
    return False, ""


def _is_generic_scrape_title(target: ScrapeTarget) -> bool:
    title = (target.scrape_title or target.local_title or "").strip()
    normalized_title = title.lower().replace("_", " ").strip()
    return normalized_title in {"", "season 0", "season 1", "season 2", "season 3", "season 4", "tv", "movie"}


def _can_relax_review_target(
    target: ScrapeTarget,
    best: ScrapeCandidate,
    candidates: List[ScrapeCandidate],
) -> Tuple[bool, str]:
    """判断 needs_review 的 target 是否可以自动采用。

    策略偏向减少无意义确认，但只对 TV 放宽；电影和短标题仍保守。
    """
    if target.scrape_type != "tv":
        return False, "target 需要人工确认"

    title = (target.scrape_title or target.local_title or "").strip()

    title_exact = any("标题完全匹配" in reason for reason in best.reasons)
    year_known = bool(target.scrape_year or target.local_year)
    candidate_count = len(candidates)
    runner_up = sorted(candidates, key=lambda c: (c.score, c.popularity), reverse=True)[1] if candidate_count > 1 else None
    strong_quality = best.vote_average >= 7 and best.popularity >= 3
    very_popular = best.popularity >= 20 and best.vote_average >= 7
    clear_top = (
        runner_up is not None
        and best.vote_average >= 7
        and best.popularity >= 8
        and runner_up.popularity <= best.popularity * 0.35
    )

    if title_exact and best.score >= 40:
        return True, "标题完全匹配，自动采用"

    # 罗马音/英文目录名经常搜到唯一中文或日文官方候选，但标题相似度分数很低。
    # 只在候选唯一且质量明显时放行，避免把泛词误配出去。
    if candidate_count == 1 and strong_quality and len(title) >= 5:
        return True, "唯一高质量 TV 候选，自动采用"

    if clear_top and len(title) >= 5:
        return True, "明显领先的 TV 候选，自动采用"

    if candidate_count <= 2 and very_popular and len(title) >= 5:
        return True, "高热度 TV 候选，自动采用"

    if year_known:
        return False, "target 需要人工确认"

    return False, "target 需要人工确认"


def infer_auto_tmdb_season_number(target: ScrapeTarget) -> Optional[int]:
    """自动刮削时推导 TMDB season number。

    这是无 TMDB 候选上下文时的保守回退。真正执行刮削时应调用
    service.resolve_tmdb_season_number(target, tmdb_id, tmdb_type, ...)，
    先检查选中 TMDB 条目是否存在本地季号。
    """
    if target.scrape_type != "tv":
        return None
    if target.group_type == "special":
        return 0

    if target.local_season_number is not None:
        return target.local_season_number
    return 1


def _is_transient_external_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        keyword in message
        for keyword in (
            "超时",
            "timeout",
            "网络",
            "connect",
            "connection",
            "temporarily",
            "rate limit",
            "速率限制",
            "tmdb",
            "anilist",
            "bangumi",
            "图片下载不完整",
            "artwork incomplete",
        )
    )


def _scrape_result_has_required_artwork(result: object) -> bool:
    """生产返回中海报和背景图必须完整；旧测试桩未返回字段时保持兼容。"""
    if not isinstance(result, dict):
        return True
    required_keys = {"poster_path", "fanart_path"}
    if not required_keys.issubset(result):
        return True
    return all(str(result.get(key) or "").strip() for key in required_keys)


def _group_targets_by_library_work(
    targets: list[ScrapeTarget],
    plans_by_id: dict[str, object],
) -> tuple[dict[str, str], dict[str, list[ScrapeTarget]]]:
    """Group scrape targets by the existing LibraryIndex card identity."""
    from app.library.index import _library_work_id

    keys_by_target: dict[str, str] = {}
    targets_by_key: dict[str, list[ScrapeTarget]] = {}
    for target in targets:
        plan = plans_by_id.get(target.import_plan_id)
        item_ids = set(target.item_ids or [])
        library_ids = {
            _library_work_id(item)
            for item in (getattr(plan, "items", None) or [])
            if item.id in item_ids and _library_work_id(item)
        }
        if len(library_ids) == 1:
            key = f"library:{target.source}:{next(iter(library_ids))}"
        else:
            fallback = target.work_id or target.scrape_target_id
            key = f"target:{target.source}:{target.import_plan_id}:{fallback}"
        keys_by_target[target.scrape_target_id] = key
        targets_by_key.setdefault(key, []).append(target)
    return keys_by_target, targets_by_key


def _work_targets_complete(
    targets: list[ScrapeTarget],
    scrape_index: dict[str, object],
    plans_by_id: dict[str, object],
    include_episode: bool,
) -> bool:
    return all(
        _target_already_scraped(
            target,
            scrape_index,
            include_episode=include_episode,
            plan=plans_by_id.get(target.import_plan_id),
        )
        for target in targets
    )


def _target_outputs_complete_after_scrape(
    target: ScrapeTarget,
    scrape_index: dict[str, object],
    plan: object,
    include_episode: bool,
) -> bool:
    return _target_already_scraped(
        target,
        scrape_index,
        include_episode=include_episode,
        plan=plan,
    )


def run_auto_scrape(
    source: str,
    plan_id: Optional[str] = None,
    threshold: float = 70,
    include_episode: bool = True,
    tmdb_client: Optional[TMDBClient] = None,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    target_ids: Optional[set[str]] = None,
    library_work_id: str = "",
    publish_library: bool = True,
) -> dict:
    """执行自动刮削

    - publish_library=True（legacy 默认）：每完成一个作品局部刷新 legacy
      LibraryIndex（即时反馈）；
    - publish_library=False（V3 durable）：不直接更新 legacy LibraryIndex，
      由 durable handler 写 SQLite binding/artifact 后统一 enqueue_library_rebuild。
    """
    from app.scrape.service import get_targets
    from app.import_plan.store import load_import_plan, load_latest_confirmed_import_plan
    from app.scrape.review_queue import (
        add_to_review_queue,
        get_pending_review_items,
        prune_pending_review_items,
        resolve_review_item,
    )

    now = _now_iso()
    owns_client = tmdb_client is None
    client = tmdb_client or TMDBClient()
    logs: List[dict] = []

    def ensure_not_cancelled() -> None:
        """在安全边界检查取消状态。取消后立即抛出，阻止后续处理。"""
        if should_cancel and should_cancel():
            raise TaskCancelledError()

    def push_log(message: str, kind: str = "info", progress: Optional[int] = None, patch: Optional[dict] = None) -> None:
        ensure_not_cancelled()
        append_task_log(logs, message, kind)
        if progress_callback:
            result_patch = {"logs": list(logs)}
            if patch:
                result_patch.update(patch)
            progress_callback(progress if progress is not None else 5, message, result_patch)

    sources = [source]
    if source == "all":
        sources = ["pan115", "baidu", "local", "openlist"]

    # 获取 targets。source=all 时跳过没有计划的来源。
    targets = []
    skipped_sources = []
    for src in sources:
        src_targets, error = get_targets(src, plan_id if src == source else None)
        if error:
            skipped_sources.append({"source": src, "reason": error})
            continue
        targets.extend(src_targets)

    if not targets:
        push_log("没有找到可刮削的作品，请先导入并生成镜像。", "warn", 100)
        if owns_client:
            client.close()
        return {
            "error": "未找到可刮削目标",
            "skipped_sources": skipped_sources,
            "auto_scraped": 0,
            "review_queued": 0,
            "failed": 0,
            "logs": logs,
        }

    # 只处理 season/movie
    scrape_targets = [t for t in targets if t.scrape_type in ("tv", "movie")]
    requested_target_ids = None if target_ids is None else {
        str(target_id).strip() for target_id in target_ids if str(target_id).strip()
    }
    if requested_target_ids is not None:
        scrape_targets = [
            target for target in scrape_targets
            if target.scrape_target_id in requested_target_ids
        ]
    completeness_plans: dict[str, object] = {}
    if plan_id:
        base_plan = load_import_plan(plan_id=plan_id)
    elif source != "all":
        base_plan = load_latest_confirmed_import_plan(source)
    else:
        base_plan = None
    if base_plan is not None:
        completeness_plans[base_plan.plan_id] = base_plan
    for target in scrape_targets:
        if target.import_plan_id and target.import_plan_id not in completeness_plans:
            target_plan = load_import_plan(plan_id=target.import_plan_id)
            if target_plan is not None:
                completeness_plans[target.import_plan_id] = target_plan

    # 按系列和季号排序，确保同系列第一季先被处理（后续季度可复用第一季 tmdb_id）
    scrape_targets.sort(key=lambda t: (
        t.series_group or t.local_title or "",
        t.local_season_number or 1,
    ))

    valid_target_ids = {target.scrape_target_id for target in scrape_targets}
    stale_count = 0
    if requested_target_ids is None:
        stale_count = prune_pending_review_items(
            valid_target_ids,
            None if source == "all" else source,
        )
    if stale_count:
        push_log(f"已归档失效的待确认条目：{stale_count} 个", "info", 2)
    pending_candidates = {
        item.scrape_target_id: item
        for item in get_pending_review_items(None if source == "all" else source)
    }

    if not scrape_targets:
        push_log("没有 season/movie 类型的刮削目标。", "warn", 100)
        if owns_client:
            client.close()
        return {
            "message": "无 season/movie 类型的刮削目标",
            "auto_scraped": 0,
            "review_queued": 0,
            "failed": 0,
            "logs": logs,
        }

    auto_scraped = 0
    review_queued = 0
    failed = 0
    skipped_existing = 0
    consecutive_external_failures = 0
    retry_counts: dict[str, int] = {}
    results = []
    total = len(scrape_targets)
    from app.scrape.effective_store import is_v3_revision

    if is_v3_revision(plan_id):
        existing_scrape_index = _build_existing_scrape_index(plan_id)
    else:
        # legacy：0 参数调用，保持外部 monkeypatch（lambda: {}）兼容
        existing_scrape_index = _build_existing_scrape_index()
    scrape_map = _load_scrape_map_for_plan(plan_id)
    work_key_by_target, targets_by_work_key = _group_targets_by_library_work(
        scrape_targets,
        completeness_plans,
    )
    published_work_keys: set[str] = set()
    library_refreshed_count = 0
    library_refresh_revision = 0
    library_refresh_result: dict = {
        "mode": "deferred",
        "work_count": 0,
    }

    def publish_work_if_complete(
        target: ScrapeTarget,
        current_label: str,
        progress: int,
        progress_patch: dict,
    ) -> None:
        """局部发布所有产物已完整的作品，包括本轮直接跳过的已有目标。"""
        nonlocal library_refreshed_count, library_refresh_revision, library_refresh_result
        # Module 5：V3 durable path（publish_library=False）不直接更新
        # legacy LibraryIndex——SQLite binding/artifact 写完后由 durable
        # library_rebuild 统一重建投影。
        if not publish_library:
            return

        work_key = work_key_by_target[target.scrape_target_id]
        work_targets = targets_by_work_key[work_key]
        if work_key in published_work_keys or not _work_targets_complete(
            work_targets,
            existing_scrape_index,
            completeness_plans,
            include_episode,
        ):
            return

        refresh_patch = {
            **progress_patch,
            "auto_scraped": auto_scraped,
            "skipped_existing": skipped_existing,
            "library_refreshed_count": library_refreshed_count,
            "library_refresh_revision": library_refresh_revision,
        }
        try:
            from app.library.service import refresh_library_for_scrape_targets

            if library_work_id:
                library_refresh_result = refresh_library_for_scrape_targets(
                    work_targets,
                    library_work_id=library_work_id,
                )
            else:
                library_refresh_result = refresh_library_for_scrape_targets(work_targets)
            published_work_keys.add(work_key)
            library_refreshed_count += 1
            library_refresh_revision += 1
            push_log(
                f"作品数据已更新：{current_label}",
                "done",
                progress,
                {
                    **refresh_patch,
                    "library_refreshed_count": library_refreshed_count,
                    "library_refresh_revision": library_refresh_revision,
                    "library_refresh": library_refresh_result,
                },
            )
        except Exception as refresh_error:
            library_refresh_result = {
                "mode": "partial_failed",
                "work_count": 0,
                "warnings": [str(refresh_error)],
            }
            push_log(
                f"作品已刮削，但媒体库局部更新失败：{current_label}，{refresh_error}",
                "warn",
                progress,
                {
                    **refresh_patch,
                    "library_refresh": library_refresh_result,
                },
            )

    for target in scrape_targets:
        ensure_not_cancelled()
        current_label = _format_target_label(target)
        completed_targets = auto_scraped + review_queued + failed + skipped_existing
        current_index = min(completed_targets + 1, total)
        progress = 5 + int(completed_targets * 90 / max(total, 1))
        remaining_targets = max(0, total - completed_targets)
        progress_patch = {
            "current_target": current_label,
            "current_index": current_index,
            "total_targets": total,
            "completed_targets": completed_targets,
            "remaining_targets": remaining_targets,
            "auto_scraped": auto_scraped,
            "review_queued": review_queued,
            "failed": failed,
            "library_refreshed_count": library_refreshed_count,
            "library_refresh_revision": library_refresh_revision,
        }
        push_log(f"开始处理：{current_label}", "info", progress, progress_patch)
        try:
            if _target_already_scraped(
                target,
                existing_scrape_index,
                include_episode=include_episode,
                plan=completeness_plans.get(target.import_plan_id),
            ):
                skipped_existing += 1
                consecutive_external_failures = 0
                push_log(f"已有完整资料，跳过重复刮削：{current_label}", "done", progress, {
                    **progress_patch,
                    "skipped_existing": skipped_existing,
                })
                results.append({
                    "target_id": target.scrape_target_id,
                    "title": current_label,
                    "status": "skipped",
                    "reason": "already_scraped",
                })
                publish_work_if_complete(target, current_label, progress, progress_patch)
                continue

            # 搜索候选
            # 优先：同系列后续季度复用第一季的 tmdb_id
            series_candidate = None
            series_season_number = None
            if _should_reuse_series_scrape(target):
                series_binding = _find_series_binding(target, scrape_map)
                if series_binding:
                    series_tmdb_id, confirmed_tmdb_season_number = series_binding
                    push_log(f"同系列已刮削，复用 TMDB ID {series_tmdb_id}：{current_label}", "info", progress, progress_patch)
                    series_candidate, series_season_number = _build_candidate_from_series(
                        target, series_tmdb_id, client, confirmed_tmdb_season_number
                    )
                    if series_candidate:
                        candidates = [series_candidate]
                        candidate = series_candidate
                        reason = "同系列复用，自动采用"
                        tmdb_season_number = series_season_number
                        push_log(
                            f"同系列复用成功：{candidate.title} / Season {tmdb_season_number}",
                            "done", progress, progress_patch,
                        )

            if not series_candidate:
                pending_item = pending_candidates.get(target.scrape_target_id)
                cached_candidates = _restore_review_candidates(target, pending_item)
                if cached_candidates:
                    candidates = cached_candidates
                    push_log(
                        f"复用待确认候选 {len(candidates)} 个：{current_label}",
                        "info",
                        progress,
                        progress_patch,
                    )
                else:
                    push_log(f"搜索作品：{target.scrape_title or target.local_title}", "search", progress, progress_patch)
                    candidates = search_candidates(target, tmdb_client=client)
                push_log(
                    f"找到候选 {len(candidates)} 个{_candidate_provider_summary(candidates)}：{current_label}",
                    "info",
                    progress,
                    progress_patch,
                )
                filtered_candidates, removed_count = _filter_auto_candidates_by_domain(
                    target,
                    candidates,
                    client,
                )
                if removed_count and filtered_candidates:
                    candidates = filtered_candidates
                    push_log(
                        f"已排除 {removed_count} 个非动画 TV 候选：{current_label}",
                        "info",
                        progress,
                        progress_patch,
                    )

                # 决定是否自动采用
                candidate, reason = decide_auto_candidate(target, candidates, threshold)

            if candidate:
                # 自动采用
                try:
                    if not series_candidate:
                        tmdb_season_number = resolve_tmdb_season_number(
                            target,
                            candidate.tmdb_id,
                            candidate.tmdb_type,
                            tmdb_client=client,
                        )
                    validation_issues = _validate_auto_candidate(
                        target,
                        candidate,
                        tmdb_season_number,
                        client,
                        trusted_series_binding=series_candidate is not None,
                    )
                    blockers = _blocking_issues_after_candidate_evidence(
                        target,
                        candidate,
                        candidates,
                        blocking_issues(validation_issues),
                    )
                    if blockers:
                        reason = "；".join(issue_messages(blockers))
                        ensure_not_cancelled()
                        add_to_review_queue(
                            target=target,
                            reason=reason,
                            candidates=candidates,
                        )
                        review_queued += 1
                        consecutive_external_failures = 0
                        push_log(f"元数据校验未通过，转人工确认：{current_label}，{reason}", "warn", progress, {
                            **progress_patch,
                            "review_queued": review_queued,
                        })
                        results.append({
                            "target_id": target.scrape_target_id,
                            "title": current_label,
                            "status": "review_queued",
                            "reason": reason,
                            "candidate_count": len(candidates),
                        })
                        continue

                    push_log(
                        f"自动采用：{candidate.title}"
                        f"{f' ({candidate.year})' if candidate.year else ''}"
                        f" / 评分 {candidate.score:.0f}",
                        "done",
                        progress,
                        progress_patch,
                    )
                    ensure_not_cancelled()
                    scrape_result = execute_scrape(
                        target=target,
                        tmdb_id=candidate.tmdb_id,
                        tmdb_type=candidate.tmdb_type,
                        tmdb_season_number=tmdb_season_number,
                        selected_by="auto",
                        tmdb_client=client,
                        include_episode=include_episode,
                        trusted_series_binding=series_candidate is not None,
                        identity_evidence=_candidate_identity_evidence(
                            candidate,
                            auto_decision=_build_auto_decision_evidence(
                                target,
                                candidates,
                                candidate,
                                reason,
                            ),
                        ),
                        should_cancel=should_cancel,
                        log_callback=(
                            lambda msg, kind="info", progress=progress, progress_patch=progress_patch:
                            push_log(msg, kind, progress, progress_patch)
                        ),
                    )
                    if not _scrape_result_has_required_artwork(scrape_result):
                        missing = [
                            label
                            for key, label in (("poster_path", "海报"), ("fanart_path", "背景图"))
                            if not str((scrape_result or {}).get(key) or "").strip()
                        ]
                        raise RuntimeError(f"图片下载不完整：{'、'.join(missing)}")
                    # execute_scrape persists the newly accepted target. Refresh the
                    # in-memory map immediately so later seasons in the same batch can
                    # reuse the first season's TMDB id instead of running a full search.
                    scrape_map = _load_scrape_map_for_plan(plan_id)
                    existing_scrape_index = {
                        item.scrape_target_id: item for item in scrape_map.items
                    }
                    if not _target_outputs_complete_after_scrape(
                        target,
                        existing_scrape_index,
                        completeness_plans.get(target.import_plan_id),
                        include_episode,
                    ):
                        raise RuntimeError(
                            "刮削产物不完整：身份、NFO、海报、背景图或分集 NFO 未全部生成"
                        )
                    resolve_review_item(target.scrape_target_id, "resolved")
                    auto_scraped += 1
                    consecutive_external_failures = 0
                    publish_work_if_complete(target, current_label, progress, progress_patch)
                    push_log(f"完成作品：{current_label}", "done", progress, {
                        **progress_patch,
                        "auto_scraped": auto_scraped,
                        "library_refreshed_count": library_refreshed_count,
                        "library_refresh_revision": library_refresh_revision,
                    })
                    results.append({
                        "target_id": target.scrape_target_id,
                        "title": current_label,
                        "status": "auto_scraped",
                        "tmdb_id": candidate.tmdb_id,
                        "score": candidate.score,
                    })
                except TaskCancelledError:
                    raise
                except Exception as e:
                    if _is_transient_external_error(e) and retry_counts.get(target.scrape_target_id, 0) < 1:
                        retry_counts[target.scrape_target_id] = retry_counts.get(target.scrape_target_id, 0) + 1
                        scrape_targets.append(target)
                        consecutive_external_failures = 0
                        push_log(f"外部服务暂时失败，已跳过并排到本轮末尾重试：{current_label}，{e}", "warn", progress, {
                            **progress_patch,
                            "retry_scheduled": True,
                            "total_targets": total,
                        })
                        results.append({
                            "target_id": target.scrape_target_id,
                            "title": current_label,
                            "status": "retry_scheduled",
                            "stage": "auto_execute",
                            "error": str(e),
                        })
                        continue
                    failed += 1
                    if _is_transient_external_error(e):
                        consecutive_external_failures += 1
                    else:
                        consecutive_external_failures = 0
                    push_log(f"刮削失败：{current_label}，{e}", "error", progress, {
                        **progress_patch,
                        "failed": failed,
                    })
                    save_failed_case({
                        **build_failed_case(
                            target=target,
                            candidate=candidate,
                            candidates=candidates,
                            error=e,
                            stage="auto_execute",
                            extra={"timestamp": now},
                        ),
                    })
                    results.append({
                        "target_id": target.scrape_target_id,
                        "title": current_label,
                        "status": "failed",
                        "error": str(e),
                    })
                    if consecutive_external_failures >= 3:
                        push_log(
                            "外部元数据服务连续失败，已暂停本轮刮削。请稍后重试，或先处理已生成的镜像/人工确认项。",
                            "warn",
                            min(97, progress + 1),
                            {
                                **progress_patch,
                                "failed": failed,
                                "paused": True,
                            },
                        )
                        break
            else:
                # 进入 review queue
                ensure_not_cancelled()
                add_to_review_queue(
                    target=target,
                    reason=reason,
                    candidates=candidates,
                )
                review_queued += 1
                consecutive_external_failures = 0
                push_log(f"需要人工确认：{current_label}，{reason}", "warn", progress, {
                    **progress_patch,
                    "review_queued": review_queued,
                })
                results.append({
                    "target_id": target.scrape_target_id,
                    "title": current_label,
                    "status": "review_queued",
                    "reason": reason,
                    "candidate_count": len(candidates),
                })

        except TaskCancelledError:
            raise
        except Exception as e:
            if _is_transient_external_error(e) and retry_counts.get(target.scrape_target_id, 0) < 1:
                retry_counts[target.scrape_target_id] = retry_counts.get(target.scrape_target_id, 0) + 1
                scrape_targets.append(target)
                consecutive_external_failures = 0
                push_log(f"外部服务暂时失败，已跳过并排到本轮末尾重试：{current_label}，{e}", "warn", progress, {
                    **progress_patch,
                    "retry_scheduled": True,
                    "total_targets": total,
                })
                results.append({
                    "target_id": target.scrape_target_id,
                    "title": current_label,
                    "status": "retry_scheduled",
                    "stage": "auto_search",
                    "error": str(e),
                })
                continue
            failed += 1
            if _is_transient_external_error(e):
                consecutive_external_failures += 1
            else:
                consecutive_external_failures = 0
            push_log(f"搜索失败：{current_label}，{e}", "error", progress, {
                **progress_patch,
                "failed": failed,
            })
            save_failed_case({
                **build_failed_case(
                    target=target,
                    error=e,
                    stage="auto_search",
                    extra={"timestamp": now},
                ),
            })
            results.append({
                "target_id": target.scrape_target_id,
                "title": current_label,
                "status": "failed",
                "error": str(e),
            })
            if consecutive_external_failures >= 3:
                push_log(
                    "外部元数据服务连续失败，已暂停本轮刮削。请稍后重试，或先处理已生成的镜像/人工确认项。",
                    "warn",
                    min(97, progress + 1),
                    {
                        **progress_patch,
                        "failed": failed,
                        "paused": True,
                    },
                )
                break

        if progress_callback:
            completed_targets = auto_scraped + review_queued + failed + skipped_existing
            remaining_targets = max(0, total - completed_targets)
            progress_callback(5 + int(completed_targets * 90 / max(total, 1)), f"已处理：{current_label}", {
                "current_target": current_label,
                "current_index": min(completed_targets, total),
                "total_targets": total,
                "completed_targets": completed_targets,
                "remaining_targets": remaining_targets,
                "auto_scraped": auto_scraped,
                "review_queued": review_queued,
                "failed": failed,
                "library_refreshed_count": library_refreshed_count,
                "library_refresh_revision": library_refresh_revision,
                "logs": list(logs),
            })

    completed_targets = auto_scraped + review_queued + failed + skipped_existing
    remaining_targets = max(0, total - completed_targets)
    if progress_callback:
        progress_callback(95, "刮削处理完成", {
            "current_target": "",
            "current_index": min(completed_targets, total),
            "total_targets": total,
            "completed_targets": completed_targets,
            "remaining_targets": remaining_targets,
            "auto_scraped": auto_scraped,
            "review_queued": review_queued,
            "failed": failed,
            "library_refreshed_count": library_refreshed_count,
            "library_refresh_revision": library_refresh_revision,
            "logs": list(logs),
        })

    if remaining_targets == 0 and consecutive_external_failures < 3:
        push_log("自动刮削全部完成", "done", 100, {
            "current_target": "",
            "current_index": total,
            "total_targets": total,
            "completed_targets": completed_targets,
            "remaining_targets": 0,
            "library_refreshed_count": library_refreshed_count,
            "library_refresh_revision": library_refresh_revision,
            "library_refresh": library_refresh_result,
        })

    try:
        return {
            "source": source,
            "total_targets": total,
            "completed_targets": completed_targets,
            "remaining_targets": remaining_targets,
            "skipped_sources": skipped_sources,
            "auto_scraped": auto_scraped,
            "skipped_existing": skipped_existing,
            "review_queued": review_queued,
            "failed": failed,
            "threshold": threshold,
            "results": results,
            "logs": logs,
            "paused": consecutive_external_failures >= 3,
            "library_refresh": library_refresh_result,
        }
    finally:
        if owns_client:
            client.close()


def _format_target_label(target: ScrapeTarget) -> str:
    """生成面向用户的当前刮削对象名称。"""
    title = target.scrape_title or target.local_title or target.series_group or "未命名作品"
    parts = [title]
    if target.scrape_year:
        parts.append(str(target.scrape_year))
    if target.scrape_type == "tv" and target.local_season_number:
        parts.append(f"第 {target.local_season_number} 季")
    if target.scrape_type == "movie":
        parts.append("电影")
    return " / ".join(parts)


def _candidate_provider_summary(candidates: List[ScrapeCandidate]) -> str:
    counts: Dict[str, int] = {}
    for candidate in candidates:
        provider = (candidate.provider or "tmdb").strip() or "tmdb"
        counts[provider] = counts.get(provider, 0) + 1
    if not counts:
        return ""
    labels = {
        "tmdb": "TMDB",
        "anilist": "AniList",
        "bangumi": "Bangumi",
    }
    parts = [f"{labels.get(provider, provider)} {count}" for provider, count in sorted(counts.items())]
    return f"（{' / '.join(parts)}）"


def _filter_auto_candidates_by_domain(
    target: ScrapeTarget,
    candidates: List[ScrapeCandidate],
    client: TMDBClient,
) -> Tuple[List[ScrapeCandidate], int]:
    """自动刮削前剔除明显不是动画的 TMDB TV 候选。"""
    if not _target_prefers_anime(target):
        return candidates, 0

    kept: List[ScrapeCandidate] = []
    removed = 0
    for candidate in candidates:
        keep, _reason = _candidate_matches_anime_domain(candidate, client)
        if keep:
            kept.append(candidate)
        else:
            removed += 1
    return kept, removed


def _candidate_matches_anime_domain(
    candidate: ScrapeCandidate,
    client: TMDBClient,
) -> Tuple[bool, str]:
    if candidate.tmdb_type != "tv":
        return True, ""
    provider = (candidate.provider or "tmdb").casefold()
    if provider in {"anilist", "bangumi"}:
        return True, ""

    genre_ids = _candidate_genre_ids(candidate)
    if genre_ids:
        if 16 in genre_ids:
            return True, ""
        return False, "TMDB TV 候选缺少 Animation genre"

    try:
        detail = client.get_tv_detail(candidate.tmdb_id)
    except Exception:
        logger.debug("auto domain detail check failed: tmdb_id=%s", candidate.tmdb_id, exc_info=True)
        return True, ""
    if _detail_has_animation_genre(detail):
        return True, ""
    if not (detail.get("genres") or detail.get("genre_ids")):
        return True, "TMDB TV 候选未提供类型信息，保留到最终身份校验"
    return False, "TMDB TV 候选详情缺少 Animation genre"


def _target_prefers_anime(target: ScrapeTarget) -> bool:
    show_type = (target.show_type or "").casefold()
    if show_type.startswith("anime"):
        return True
    text = " ".join(
        str(value or "")
        for value in (
            target.media_type,
            target.series_group,
            target.local_title,
            target.original_title,
            target.source_subwork_dir,
            target.target_dir,
        )
    ).casefold()
    return any(keyword in text for keyword in ("动画", "番剧", "anime"))


def _candidate_genre_ids(candidate: ScrapeCandidate) -> set[int]:
    raw = candidate.raw or {}
    values = raw.get("genre_ids") or []
    return {
        int(value)
        for value in values
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    }


def _detail_has_animation_genre(detail: dict) -> bool:
    genre_ids = {
        int(value)
        for value in (detail.get("genre_ids") or [])
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    }
    for genre in detail.get("genres") or []:
        if isinstance(genre, dict):
            genre_id = genre.get("id")
            if isinstance(genre_id, int):
                genre_ids.add(genre_id)
            name = str(genre.get("name") or "").casefold().strip()
            if name in {"animation", "anime", "动画", "アニメ"}:
                return True
        elif str(genre or "").casefold().strip() in {"animation", "anime", "动画", "アニメ"}:
            return True
    return 16 in genre_ids


def _validate_auto_candidate(
    target: ScrapeTarget,
    candidate: ScrapeCandidate,
    tmdb_season_number: Optional[int],
    client: TMDBClient,
    trusted_series_binding: bool = False,
):
    """Validate candidate metadata before auto-writing NFO/assets."""
    if candidate.tmdb_type == "movie":
        detail = client.get_movie_detail(candidate.tmdb_id)
    else:
        detail = client.get_tv_detail(candidate.tmdb_id)
    return validate_scrape_metadata(
        target=target,
        detail=detail,
        tmdb_type=candidate.tmdb_type,
        tmdb_season_number=tmdb_season_number,
        tmdb_client=client,
        trusted_series_binding=trusted_series_binding,
    )


def _blocking_issues_after_candidate_evidence(
    target: ScrapeTarget,
    candidate: ScrapeCandidate,
    candidates: List[ScrapeCandidate],
    blockers,
) -> list:
    """Relax title-only validation blocks when search evidence is unambiguous."""
    if not blockers:
        return []
    if candidate.tmdb_type != target.scrape_type:
        return blockers
    if any(issue.code != "title_low_similarity" for issue in blockers):
        return blockers
    if _candidate_has_trusted_provider_identity(target, candidate):
        return []
    return blockers


def _candidate_has_trusted_provider_identity(
    target: ScrapeTarget,
    candidate: ScrapeCandidate,
) -> bool:
    """验证本地标题、提供方别名及 TMDB 身份之间存在完整证据链。"""
    raw = candidate.raw or {}
    aliases = [str(value or "").strip() for value in raw.get("provider_title_aliases") or []]
    aliases = [value for value in aliases if value]
    if not aliases:
        return False

    target_matches_provider = any(
        _candidate_title_identity_safe(
            target,
            ScrapeCandidate(
                tmdb_type=candidate.tmdb_type,
                title=alias,
                original_title=alias,
            ),
        )[0]
        for alias in aliases
    )
    if not target_matches_provider:
        return False

    if raw.get("provider_tmdb_link") == "direct":
        return True

    return any(
        _candidate_title_identity_safe(
            ScrapeTarget(
                scrape_type=target.scrape_type,
                scrape_title=alias,
                local_title=alias,
            ),
            candidate,
        )[0]
        for alias in aliases
    )


def _build_auto_decision_evidence(
    target: ScrapeTarget,
    candidates: list[ScrapeCandidate],
    selected: ScrapeCandidate,
    adoption_reason: str,
) -> dict:
    """记录既有自动采用策略的决策证据，不参与决策也不调整评分。

    这让低分但身份明确的正确候选、TMDB 强提示和同系列复用均可被审计，
    同时把最高分、次高分和实际选中项的差异保留下来，供质量门禁与人工
    排查使用。任何字段仅为诊断数据，绝不能反向作为新的自动采用条件。
    """
    ranked = sorted(candidates, key=lambda item: (item.score, item.popularity), reverse=True)
    selected_rank = next(
        (
            index
            for index, candidate in enumerate(ranked, start=1)
            if candidate is selected
            or (
                candidate.candidate_id == selected.candidate_id
                and candidate.tmdb_id == selected.tmdb_id
                and candidate.tmdb_type == selected.tmdb_type
            )
        ),
        None,
    )
    runner_up = next(
        (
            candidate
            for candidate in ranked
            if candidate is not selected
            and not (
                candidate.candidate_id == selected.candidate_id
                and candidate.tmdb_id == selected.tmdb_id
                and candidate.tmdb_type == selected.tmdb_type
            )
        ),
        None,
    )
    hint_matched = _is_tmdb_hint_match(target, selected)
    identity_verified, identity_reason = _candidate_identity_safe(target, selected)
    verification_path = "tmdb_hint" if hint_matched else identity_reason
    return {
        "schema_version": 1,
        "candidate_count": len(candidates),
        "selected_tmdb_id": selected.tmdb_id,
        "selected_tmdb_type": selected.tmdb_type,
        "selected_score": selected.score,
        "selected_rank_by_score": selected_rank,
        "top_score": ranked[0].score if ranked else None,
        "runner_up_score": runner_up.score if runner_up is not None else None,
        "selected_score_margin": (
            round(selected.score - runner_up.score, 1)
            if runner_up is not None
            else None
        ),
        "tmdb_hint_matched": hint_matched,
        "identity_verified": hint_matched or identity_verified,
        "identity_verification_path": verification_path,
        "adoption_reason": adoption_reason,
    }


def _candidate_identity_evidence(
    candidate: ScrapeCandidate,
    *,
    auto_decision: dict | None = None,
) -> dict:
    raw = candidate.raw or {}
    evidence = {
        "provider": candidate.provider,
        "candidate_title": candidate.title,
        "candidate_original_title": candidate.original_title,
        "provider_title_aliases": list(raw.get("provider_title_aliases") or []),
        "provider_tmdb_link": raw.get("provider_tmdb_link") or "",
        "reasons": list(candidate.reasons or []),
    }
    if auto_decision is not None:
        evidence["auto_decision"] = dict(auto_decision)
    return evidence


def _restore_review_candidates(target: ScrapeTarget, pending_item) -> List[ScrapeCandidate]:
    if pending_item is None:
        return []
    restored: List[ScrapeCandidate] = []
    for data in pending_item.candidates or []:
        try:
            restored.append(ScrapeCandidate(
                candidate_id=str(data.get("candidate_id") or ""),
                scrape_target_id=target.scrape_target_id,
                provider=str(data.get("provider") or "tmdb"),
                tmdb_id=int(data.get("tmdb_id") or 0),
                tmdb_type=str(data.get("tmdb_type") or ""),
                title=str(data.get("title") or ""),
                original_title=str(data.get("original_title") or ""),
                year=data.get("year"),
                poster_path=str(data.get("poster_path") or ""),
                score=float(data.get("score") or 0),
                reasons=list(data.get("reasons") or []),
                popularity=float(data.get("popularity") or 0),
                vote_average=float(data.get("vote_average") or 0),
                raw=dict(data.get("raw") or {}),
            ))
        except (TypeError, ValueError):
            continue
    return restored


def _load_scrape_map_for_plan(plan_id: str = ""):
    """按 plan 代次加载刮削映射：V3 → SQLite bindings 投影；legacy → JSON。

    legacy 分支调用本模块的 load_scrape_map 属性，保持外部 monkeypatch
    （legacy 测试注入假 ScrapeMap）继续有效。
    """
    from app.scrape.effective_store import is_v3_revision

    if is_v3_revision(plan_id):
        from app.scrape.effective_store import load_effective_scrape_map

        return load_effective_scrape_map(plan_id)
    return load_scrape_map()


def _build_existing_scrape_index(plan_id: str = "") -> Dict[str, object]:
    """Build scrape_target_id -> ScrapeMapItem index.

    Auto scrape is an incremental filler.  A target with a persisted scrape map
    and an existing NFO is considered completed and should not be searched or
    downloaded again. V3 读 SQLite bindings 投影，legacy 读 JSON ScrapeMap。
    """
    try:
        return {item.scrape_target_id: item for item in _load_scrape_map_for_plan(plan_id).items}
    except Exception:
        logger.debug("load scrape map failed", exc_info=True)
        return {}


def _target_already_scraped(
    target: ScrapeTarget,
    scrape_index: Dict[str, object],
    include_episode: bool = True,
    plan=None,
) -> bool:
    return target_already_scraped(target, scrape_index, include_episode, plan)
