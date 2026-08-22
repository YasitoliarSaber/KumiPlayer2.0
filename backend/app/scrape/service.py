# -*- coding: utf-8 -*-
"""刮削服务函数"""

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.import_plan.models import ImportPlanItem
from app.import_plan.store import load_import_plan, load_latest_confirmed_import_plan
from app.recognition.title_cleaner import clean_work_title_container
from app.recognition.verified_titles import match_verified_tmdb_season
from app.scrape import anilist_candidates as anilist_candidate_service
from app.scrape.models import ScrapeCandidate, ScrapeMapItem, ScrapeTarget
from app.scrape.nfo import generate_episode_nfo, generate_movie_nfo, generate_tvshow_nfo, write_nfo
from app.scrape.store import build_failed_case, save_failed_case, upsert_scrape_map_item
from app.scrape.target_builder import build_scrape_targets
from app.scrape.anilist_client import AniListClient
from app.scrape.tmdb_client import TMDBClient, TMDBClientError, TMDBAuthError
from app.scrape.validator import issue_messages, validate_scrape_metadata
from app.tasks.models import TaskCancelledError

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def get_targets(source: str, plan_id: Optional[str] = None) -> Tuple[list, Optional[str]]:
    """获取可刮削目标

    返回: (targets, error_message)
    """
    if plan_id:
        # Review Fix 2：V3 revision 必须是 current 才能作为刮削目标
        # （stale/superseded 拒绝，不 build targets）；legacy plan_id 行为不变。
        from app.import_plan import revision_store
        from app.scrape.effective_store import is_v3_revision

        if is_v3_revision(plan_id) and not revision_store.is_current_revision(plan_id):
            return [], "该 V3 计划已被新版本取代（stale revision），不能执行刮削"
        plan = load_import_plan(plan_id=plan_id)
        if plan and plan.source != source:
            return [], f"plan.source={plan.source} 与 source={source} 不匹配"
    else:
        # Module 5：无 plan_id 时先取 V3 current revisions 聚合 targets；
        # openlist 已进入 V3——current 缺失不得偷偷回退旧 JSON latest，
        # legacy 来源（pan115/baidu/local）才允许回退。
        from app.import_plan import revision_store

        plans = revision_store.list_current_plans(source)
        if plans:
            targets: list = []
            for current_plan in plans:
                targets.extend(build_scrape_targets(current_plan))
            return targets, None
        if source == "openlist":
            return [], "当前没有可刮削的 V3 目标（current revision 缺失）"
        plan = load_latest_confirmed_import_plan(source)
    if plan is None:
        return [], "ImportPlan 不存在"
    return build_scrape_targets(plan), None


def search_candidates(
    target: ScrapeTarget,
    query: Optional[str] = None,
    year: Optional[int] = None,
    tmdb_client: Optional[TMDBClient] = None,
    anilist_client: Optional[AniListClient] = None,
    bangumi_client=None,
) -> list:
    """搜索可执行刮削候选。

    TMDB remains the canonical execution provider.  For anime targets, AniList
    runs as a parallel helper: it improves search coverage, then resolves back
    to a TMDB id so the existing NFO/image pipeline stays unchanged.
    """
    if query is None and year is None:
        cached_candidates = _load_trusted_cached_candidates(target)
        if cached_candidates:
            return cached_candidates

    client = tmdb_client or TMDBClient()

    anilist_candidates: List[ScrapeCandidate] = []
    if anilist_client is not False and _should_use_anilist(target):
        tmdb_candidates, anilist_candidates = _search_tmdb_and_anilist_candidates(
            target,
            query=query,
            year=year,
            tmdb_client=client,
            anilist_client=anilist_client,
        )
    else:
        tmdb_candidates = _search_tmdb_candidates(target, query=query, year=year, tmdb_client=client)
        if _candidate_search_is_confident(tmdb_candidates):
            candidates = _dedupe_candidates(tmdb_candidates)
            candidates.sort(key=lambda c: (c.score, c.popularity), reverse=True)
            _record_empty_candidate_search(target, candidates, query, year)
            _cache_candidates(candidates)
            return candidates

    tmdb_anilist_candidates: List[ScrapeCandidate] = [*tmdb_candidates, *anilist_candidates]
    if _candidate_search_is_confident(tmdb_anilist_candidates):
        candidates = _dedupe_candidates(tmdb_anilist_candidates)
        candidates.sort(key=lambda c: (c.score, c.popularity), reverse=True)
        _record_empty_candidate_search(target, candidates, query, year)
        _cache_candidates(candidates)
        return candidates

    bangumi_candidates: List[ScrapeCandidate] = []
    if bangumi_client is not False and _should_use_bangumi(target):
        try:
            bangumi_candidates = _search_bangumi_candidates(target, query, year, client, bangumi_client)
        except Exception:
            logger.debug("bangumi candidate search failed", exc_info=True)

    candidates: List[ScrapeCandidate] = [*tmdb_anilist_candidates, *bangumi_candidates]
    candidates = _dedupe_candidates(candidates)
    candidates.sort(key=lambda c: (c.score, c.popularity), reverse=True)
    _record_empty_candidate_search(target, candidates, query, year)
    _cache_candidates(candidates)
    return candidates


def _search_tmdb_and_anilist_candidates(
    target: ScrapeTarget,
    query: Optional[str],
    year: Optional[int],
    tmdb_client: TMDBClient,
    anilist_client: Optional[AniListClient],
) -> tuple[List[ScrapeCandidate], List[ScrapeCandidate]]:
    """Search TMDB and AniList together, keeping TMDB as the canonical provider."""
    owns_anilist = anilist_client is None
    ani_client = anilist_client or AniListClient()
    owns_anilist_tmdb = False
    anilist_tmdb_client = tmdb_client
    if isinstance(tmdb_client, TMDBClient):
        anilist_tmdb_client = TMDBClient()
        owns_anilist_tmdb = True
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scrape-candidates")
    tmdb_future = executor.submit(_search_tmdb_candidates, target, query, year, tmdb_client)
    anilist_future = executor.submit(_search_anilist_candidates, target, query, year, anilist_tmdb_client, ani_client)
    future_names = {
        tmdb_future: "tmdb",
        anilist_future: "anilist",
    }
    tmdb_candidates: List[ScrapeCandidate] = []
    anilist_candidates: List[ScrapeCandidate] = []
    try:
        done, pending = wait(future_names.keys(), timeout=float(_candidate_search_timeout()))
        for future in pending:
            future.cancel()
            logger.debug("%s candidate search timed out", future_names[future])
        for future in done:
            provider = future_names[future]
            try:
                result = future.result()
            except TMDBAuthError:
                logger.debug("%s candidate search auth failed", provider, exc_info=True)
                raise
            except Exception:
                logger.debug("%s candidate search failed", provider, exc_info=True)
                result = []
            if provider == "tmdb":
                tmdb_candidates = result
            else:
                anilist_candidates = result
        return tmdb_candidates, anilist_candidates
    finally:
        # A running Future cannot be cancelled. Do not defeat the outer search timeout by waiting here.
        # Closing owned clients below causes lingering provider calls to fail fast without blocking this task.
        executor.shutdown(wait=False, cancel_futures=True)
        if owns_anilist:
            ani_client.close()
        if owns_anilist_tmdb:
            anilist_tmdb_client.close()


def _candidate_search_timeout() -> int:
    """Return total candidate search timeout in seconds."""
    from app.core.config import load_config

    config = load_config()
    raw = getattr(config, "scrape_search_timeout", 35)
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        timeout = 35
    return max(10, min(timeout, 60))


def _candidate_search_is_confident(candidates: List[ScrapeCandidate], threshold: float = 70) -> bool:
    if not candidates:
        return False
    best = max(candidates, key=lambda c: (c.score, c.popularity))
    return best.score >= threshold


def _load_trusted_cached_candidates(
    target: ScrapeTarget,
    *,
    minimum_score: float = 90,
    max_age_days: int = 30,
) -> List[ScrapeCandidate]:
    """复用同一目标近期的强候选，避免网络波动被误报为“无候选”。"""
    try:
        from app.db.candidates import list_candidates, list_candidates_by_tmdb_identity

        rows = list_candidates(target.scrape_target_id)
        cache_from_tmdb_hint = False
        hint_type = target.tmdb_hint_type or target.scrape_type
        if not rows and target.tmdb_hint_id and hint_type in {"tv", "movie"}:
            rows = list_candidates_by_tmdb_identity(target.tmdb_hint_id, hint_type)
            cache_from_tmdb_hint = bool(rows)
    except Exception:
        logger.debug("load scrape candidate cache failed", exc_info=True)
        return []

    now = datetime.now(timezone.utc)
    candidates: List[ScrapeCandidate] = []
    expected_type = target.scrape_type or target.tmdb_hint_type
    for row in rows:
        try:
            tmdb_id = int(row.get("tmdb_id") or 0)
            tmdb_type = str(row.get("tmdb_type") or "")
            score = float(row.get("score") or 0)
            poster_path = str(row.get("poster_path") or "").strip()
            cached_at = datetime.fromisoformat(str(row.get("cached_at") or ""))
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            age = now - cached_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue

        if tmdb_id <= 0 or not poster_path or score < minimum_score:
            continue
        if expected_type and tmdb_type != expected_type:
            continue
        if age < timedelta(0) or age > timedelta(days=max_age_days):
            continue

        raw_reasons = row.get("reasons") or []
        if isinstance(raw_reasons, str):
            try:
                raw_reasons = json.loads(raw_reasons)
            except json.JSONDecodeError:
                raw_reasons = []
        reasons = [str(reason) for reason in raw_reasons if str(reason).strip()]
        if "复用本地候选缓存" not in reasons:
            reasons.append("复用本地候选缓存")
        if cache_from_tmdb_hint:
            hint_reason = f"TMDB ID 命中({target.tmdb_hint_id})"
            if hint_reason not in reasons:
                reasons.insert(0, hint_reason)
            score = max(score, 150.0)

        candidates.append(ScrapeCandidate(
            candidate_id=hashlib.md5(
                f"{target.scrape_target_id}:{tmdb_type}:{tmdb_id}".encode()
            ).hexdigest()[:12],
            scrape_target_id=target.scrape_target_id,
            provider=str(row.get("provider") or "tmdb"),
            tmdb_id=tmdb_id,
            tmdb_type=tmdb_type,
            title=str(row.get("title") or ""),
            original_title=str(row.get("original_title") or ""),
            year=int(row["year"]) if row.get("year") is not None else None,
            overview=str(row.get("overview") or ""),
            poster_path=poster_path,
            popularity=float(row.get("popularity") or 0),
            vote_average=float(row.get("vote_average") or 0),
            score=score,
            reasons=reasons,
            raw={
                "candidate_cache_reused": True,
                "cached_at": cached_at.isoformat(),
            },
        ))

    candidates = _dedupe_candidates(candidates)
    candidates.sort(key=lambda candidate: (candidate.score, candidate.popularity), reverse=True)
    return candidates


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
        )
    )


def build_candidate_search_queries(
    target: ScrapeTarget,
    query: Optional[str] = None,
    prefer_query: bool = False,
) -> List[str]:
    """Return the concrete title variants candidate search will try."""
    search_title = query or target.scrape_title
    return _build_target_search_title_variants(
        target,
        search_title,
        include_learned=True,
        prefer_title=prefer_query,
    )


def _record_empty_candidate_search(
    target: ScrapeTarget,
    candidates: List[ScrapeCandidate],
    query: Optional[str],
    year: Optional[int],
) -> None:
    if candidates:
        return
    try:
        save_failed_case(build_failed_case(
            target=target,
            error="候选搜索无结果",
            stage="candidate_search",
            extra={
                "query": query or "",
                "year": year or target.scrape_year,
                "attempted_queries": build_candidate_search_queries(target, query=query, prefer_query=bool(query)),
            },
        ))
    except Exception:
        logger.debug("record empty candidate search failed", exc_info=True)


def _search_tmdb_candidates(
    target: ScrapeTarget,
    query: Optional[str] = None,
    year: Optional[int] = None,
    tmdb_client: Optional[TMDBClient] = None,
) -> list:
    """搜索 TMDB 候选"""
    client = tmdb_client or TMDBClient()
    search_title = query or target.scrape_title
    search_year = year or target.scrape_year
    deadline = time.monotonic() + _candidate_search_timeout()

    candidates = []
    seen_ids = set()

    def has_budget() -> bool:
        return time.monotonic() < deadline

    def search_with_budget(kind: str, title: str, search_year_value: Optional[int]) -> list:
        if not has_budget():
            return []
        try:
            if kind == "tv":
                return client.search_tv(title, search_year_value)
            return client.search_movie(title, search_year_value)
        except TMDBClientError as e:
            if candidates and _is_transient_external_error(e):
                return []
            raise

    hint_candidate = _candidate_from_tmdb_hint(target, client)
    if hint_candidate:
        candidates.append(hint_candidate)
        seen_ids.add((hint_candidate.tmdb_id, hint_candidate.tmdb_type))

    search_variants = build_candidate_search_queries(target, query=search_title, prefer_query=bool(query))[:10]
    for title_variant in search_variants:
        if not has_budget():
            break
        score_target = target if title_variant == (target.scrape_title or "") else replace(target, scrape_title=title_variant)
        if target.scrape_type == "tv":
            results = search_with_budget("tv", title_variant, search_year)
            if not results and search_year:
                results = search_with_budget("tv", title_variant, None)
            for r in results[:10]:
                key = (r.get("id"), "tv")
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                candidate = _tmdb_result_to_candidate(r, score_target, "tv")
                candidates.append(candidate)
        else:
            results = search_with_budget("movie", title_variant, search_year)
            if not results and search_year:
                results = search_with_budget("movie", title_variant, None)
            for r in results[:10]:
                key = (r.get("id"), "movie")
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                candidate = _tmdb_result_to_candidate(r, score_target, "movie")
                candidates.append(candidate)

    if has_budget() and target.scrape_type == "tv" and _should_use_series_group_fallback(target):
        for title_variant in _build_search_title_variants(target.series_group)[:4]:
            if not has_budget():
                break
            if title_variant == search_title:
                continue
            before_count = len(candidates)
            score_target = replace(target, scrape_title=title_variant)
            results = search_with_budget("tv", title_variant, search_year)
            if not results and search_year:
                results = search_with_budget("tv", title_variant, None)
            for r in results[:10]:
                key = (r.get("id"), "tv")
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                candidate = _tmdb_result_to_candidate(r, score_target, "tv")
                candidates.append(candidate)
            if len(candidates) > before_count:
                break

    candidates.sort(key=lambda c: (c.score, c.popularity), reverse=True)
    return candidates


def _search_bangumi_candidates(
    target: ScrapeTarget,
    query: Optional[str],
    year: Optional[int],
    tmdb_client: TMDBClient,
    bangumi_client=None,
) -> List[ScrapeCandidate]:
    """Search Bangumi, then resolve subject titles back into TMDB candidates."""
    if bangumi_client is None:
        from app.integrations.bangumi import BangumiClient
        bangumi_client = BangumiClient()

    search_title = query or target.scrape_title
    search_year = year or target.scrape_year
    candidates: List[ScrapeCandidate] = []
    seen: set[tuple[int, str]] = set()

    for title_variant in build_candidate_search_queries(target, query=search_title, prefer_query=bool(query)):
        payload = bangumi_client.search_subjects(
            title_variant,
            limit=8,
            offset=0,
            subject_types=_bangumi_subject_types(target),
        )
        subjects = payload.get("data") if isinstance(payload, dict) else []
        for subject in (subjects or [])[:8]:
            candidate = _bangumi_subject_to_candidate(subject, target, search_year, tmdb_client)
            if not candidate or candidate.tmdb_id <= 0:
                continue
            key = (candidate.tmdb_id, candidate.tmdb_type)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        if candidates:
            break

    candidates.sort(key=lambda c: (c.score, c.popularity), reverse=True)
    return candidates


def _bangumi_subject_to_candidate(
    subject: dict,
    target: ScrapeTarget,
    year: Optional[int],
    tmdb_client: TMDBClient,
) -> Optional[ScrapeCandidate]:
    tmdb_type = "movie" if target.scrape_type == "movie" else "tv"
    subject_year = _bangumi_year(subject) or year or target.scrape_year or target.local_year
    title_variants = _bangumi_title_variants(subject)
    if not title_variants:
        return None

    best: Optional[ScrapeCandidate] = None
    for title in title_variants[:5]:
        try:
            results = (
                tmdb_client.search_movie(title, subject_year)
                if tmdb_type == "movie"
                else tmdb_client.search_tv(title, subject_year)
            )
            if not results and subject_year:
                results = (
                    tmdb_client.search_movie(title, None)
                    if tmdb_type == "movie"
                    else tmdb_client.search_tv(title, None)
                )
        except Exception:
            logger.debug("resolve Bangumi -> TMDB failed", exc_info=True)
            continue

        score_target = replace(target, scrape_title=title)
        for result in results[:5]:
            candidate = _tmdb_result_to_candidate(result, score_target, tmdb_type)
            if best is None or (candidate.score, candidate.popularity) > (best.score, best.popularity):
                best = candidate

    if best is None or best.score < 10:
        return None

    best.provider = "bangumi"
    best.candidate_id = hashlib.md5(
        f"{target.scrape_target_id}:bangumi:{subject.get('id')}:{best.tmdb_id}:{best.tmdb_type}".encode()
    ).hexdigest()[:12]
    best.raw = {
        "bangumi": subject,
        "tmdb_id": best.tmdb_id,
        "tmdb_type": best.tmdb_type,
        "provider_title_aliases": title_variants,
        "provider_tmdb_link": "title_resolution",
    }
    best.score = round(best.score + _bangumi_bonus(subject, target, tmdb_type), 1)
    best.reasons.insert(0, f"Bangumi 命中({subject.get('id')})")
    return best


def _bangumi_subject_types(target: ScrapeTarget) -> list[int]:
    # Bangumi subject type: 2 = anime, 6 = real.
    show_type = (target.show_type or "").lower()
    if show_type.startswith("live"):
        return [6]
    return [2]


def _bangumi_title_variants(subject: dict) -> List[str]:
    values = [
        subject.get("name_cn"),
        subject.get("name"),
        subject.get("name_jp"),
        subject.get("name_en"),
    ]
    infobox = subject.get("infobox") or []
    if isinstance(infobox, list):
        for row in infobox:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            if key in {"中文名", "别名", "英文名", "日文名"}:
                value = row.get("value")
                if isinstance(value, list):
                    values.extend(item.get("v") if isinstance(item, dict) else item for item in value)
                else:
                    values.append(value)

    variants: List[str] = []
    for value in values:
        cleaned = " ".join(str(value or "").split()).strip()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)
    return variants


def _bangumi_year(subject: dict) -> Optional[int]:
    date = str(subject.get("date") or "")
    match = re.search(r"(19|20)\d{2}", date)
    return int(match.group(0)) if match else None


def _bangumi_bonus(subject: dict, target: ScrapeTarget, tmdb_type: str) -> float:
    score = 7.0
    eps = subject.get("eps") or subject.get("total_episodes")
    if tmdb_type == "tv" and target.item_ids and eps:
        try:
            local_count = len(target.item_ids)
            episode_count = int(eps)
            if abs(local_count - episode_count) <= max(1, int(local_count * 0.15)):
                score += 12
        except (TypeError, ValueError):
            pass
    rating = subject.get("rating") or {}
    try:
        if float(rating.get("score") or 0) > 0:
            score += 2
    except (TypeError, ValueError):
        pass
    return score


def _search_anilist_candidates(
    target: ScrapeTarget,
    query: Optional[str],
    year: Optional[int],
    tmdb_client: TMDBClient,
    anilist_client: AniListClient,
) -> List[ScrapeCandidate]:
    """Search AniList through the dedicated anime resolver module."""
    search_title = query or target.scrape_title
    search_queries = build_candidate_search_queries(target, query=search_title, prefer_query=bool(query))
    return anilist_candidate_service.search_anilist_candidates(
        target,
        query,
        year,
        tmdb_client,
        anilist_client,
        search_queries=search_queries,
        tmdb_result_to_candidate=_tmdb_result_to_candidate,
    )


def _should_use_anilist(target: ScrapeTarget) -> bool:
    return anilist_candidate_service.should_use_anilist(target)


def _should_use_bangumi(target: ScrapeTarget) -> bool:
    if target.scrape_type in {"tv", "movie"}:
        return True
    show_type = (getattr(target, "show_type", "") or "").lower()
    if show_type.startswith(("anime", "live")):
        return True
    text = " ".join([
        target.series_group or "",
        target.local_title or "",
        target.original_title or "",
        target.source_subwork_dir or "",
        target.target_dir or "",
    ])
    return any(keyword in text for keyword in ("动画", "番剧", "anime", "Anime"))


def _should_use_series_group_fallback(target: ScrapeTarget) -> bool:
    """Only main-series season targets may fall back to the parent series title."""
    if target.scrape_type != "tv" or not target.series_group:
        return False
    if target.card_type != "main_series":
        return False
    if target.group_type != "season":
        return False
    local_key = _title_key(target.local_title or target.scrape_title)
    series_key = _title_key(target.series_group)
    if not local_key or not series_key:
        return False
    return local_key != series_key


def _title_key(title: str) -> str:
    return re.sub(r"\s+", "", title or "").casefold()


def _dedupe_candidates(candidates: List[ScrapeCandidate]) -> List[ScrapeCandidate]:
    best_by_key: dict[tuple[int, str], ScrapeCandidate] = {}
    unresolved: List[ScrapeCandidate] = []
    for candidate in candidates:
        if candidate.tmdb_id <= 0:
            unresolved.append(candidate)
            continue
        key = (candidate.tmdb_id, candidate.tmdb_type)
        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = candidate
            continue
        if (candidate.score, candidate.popularity) > (current.score, current.popularity):
            best_by_key[key] = _merge_candidate_identity_evidence(candidate, current)
        else:
            best_by_key[key] = _merge_candidate_identity_evidence(current, candidate)
    return list(best_by_key.values()) + unresolved


def _merge_candidate_identity_evidence(primary: ScrapeCandidate, secondary: ScrapeCandidate) -> ScrapeCandidate:
    """保留排序胜者，同时合并同 TMDB 身份的可信提供方别名证据。"""
    primary_raw = dict(primary.raw or {})
    secondary_raw = secondary.raw or {}
    primary_aliases = list(primary_raw.get("provider_title_aliases") or [])
    secondary_aliases = list(secondary_raw.get("provider_title_aliases") or [])
    if secondary_aliases:
        primary_raw["provider_title_aliases"] = list(dict.fromkeys([*primary_aliases, *secondary_aliases]))
    if secondary_raw.get("provider_tmdb_link") == "direct":
        primary_raw["provider_tmdb_link"] = "direct"
    primary.raw = primary_raw
    return primary


def _build_target_search_title_variants(
    target: ScrapeTarget,
    title: str,
    include_learned: bool = True,
    prefer_title: bool = False,
) -> List[str]:
    """Build search variants, preferring prior manual-success queries."""
    variants: List[str] = []

    def add(value: str) -> None:
        value = " ".join((value or "").split()).strip(" .:：")
        if value and not _is_search_query_noise(value) and value not in variants:
            variants.append(value)

    if prefer_title:
        for variant in _build_search_title_variants(title or ""):
            add(variant)
        for variant in _build_season_title_fallbacks(title or ""):
            add(variant)

    if include_learned:
        for learned in _learned_search_queries(target):
            add(learned)

    seed_titles = [
        title,
        target.scrape_title,
        target.local_title,
        target.source_subwork_dir,
        target.original_title,
    ]
    if _should_use_series_group_fallback(target):
        seed_titles.append(target.series_group)
    for seed in seed_titles:
        if prefer_title and seed == title:
            continue
        for variant in _build_search_title_variants(seed or ""):
            add(variant)
        for variant in _build_season_title_fallbacks(seed or ""):
            add(variant)

    return variants[:24]


def _build_season_title_fallbacks(title: str) -> List[str]:
    """Return conservative parent-title fallbacks for sequel/season folders.

    A lot of local libraries name season 2 with markers that official APIs do
    not index the same way: "S2", "Season 2", "第二季", "♭", "II".  These
    fallbacks let the scraper search the parent series as well, then map back
    to the local season number when writing episode NFOs.
    """
    if not title:
        return []

    variants: List[str] = []

    def add(value: str) -> None:
        value = " ".join((value or "").split()).strip(" .:：-－—–")
        if value and value not in variants:
            variants.append(value)

    cleaned = _strip_tmdb_hint_text(title)
    cleaned = re.sub(r"^\s*\d+\s*[.、_-]\s*", "", cleaned)
    cleaned = _remove_bracketed_season_markers(cleaned)
    add(cleaned)

    patterns = [
        r"\s*S(?:eason)?\s*\d+\b.*$",
        r"\s*第\s*[一二三四五六七八九十百\d]+\s*[季期部].*$",
        r"\s*\d+(?:st|nd|rd|th)\s+Season\b.*$",
        r"\s*Part\s*\d+\b.*$",
        r"\s*(?:Ⅱ|Ⅲ|Ⅳ|Ⅴ|II|III|IV|V)\s*$",
        r"\s*[♭♪]+$",
        r"\s+flat\b.*$",
    ]
    for pattern in patterns:
        add(re.sub(pattern, "", cleaned, flags=re.IGNORECASE))

    # Folders like "路人女主的养成方法.S1-S2+剧场版" should still yield the
    # parent title before the mixed-season suffix.
    add(re.sub(r"[.。_\s-]*(?:S\d+\s*[-+~～]\s*S\d+|S\d+).*$", "", cleaned, flags=re.IGNORECASE))
    add(re.sub(r"[.。_\s-]*(?:\d+\s*[季期].*)$", "", cleaned, flags=re.IGNORECASE))

    return variants


def _remove_bracketed_season_markers(title: str) -> str:
    """Drop bracketed season tokens without removing useful title text."""
    cleaned = title or ""
    cleaned = re.sub(
        r"[\[\(（【]\s*(?:S\d+|Season\s*\d+|第\s*[一二三四五六七八九十百\d]+\s*[季期部])\s*[\]\)）】]",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.split()).strip()


def _learned_search_queries(target: ScrapeTarget) -> List[str]:
    """Reuse manual search terms that previously produced a successful scrape.

    Module 5 Review Fix：V3 target 只从 SQLite all-bindings 学习（绝不读
    legacy JSON，避免 stale ScrapeMap 改变候选 query 顺序/内容）；legacy
    target 继续 scrape_map.json。
    """
    try:
        from app.scrape.effective_store import is_v3_revision, load_all_bindings_scrape_map

        if is_v3_revision(target.import_plan_id):
            items = load_all_bindings_scrape_map().items
        else:
            from app.scrape.store import load_scrape_map

            items = load_scrape_map().items
    except Exception:
        logger.debug("load learned scrape queries failed", exc_info=True)
        return []

    scored: List[tuple[int, str]] = []
    for item in items:
        if getattr(item, "selected_by", "") != "manual":
            continue
        query = _clean_saved_search_query(getattr(item, "search_query", ""))
        if not query:
            continue
        if getattr(item, "tmdb_type", "") != target.scrape_type:
            continue

        score = 0
        if item.scrape_target_id == target.scrape_target_id:
            score = 100
        elif item.source == target.source and item.source_subwork_dir and item.source_subwork_dir == target.source_subwork_dir:
            score = 80
        elif item.source == target.source and item.local_title and item.local_title == target.local_title:
            score = 70
        elif (
            item.source == target.source
            and item.series_group
            and item.series_group == target.series_group
            and item.local_year == target.local_year
        ):
            score = 55

        if score:
            scored.append((score, query))

    deduped: List[str] = []
    for _, query in sorted(scored, key=lambda entry: entry[0], reverse=True):
        if query not in deduped:
            deduped.append(query)
    return deduped[:4]


def _clean_saved_search_query(query: Optional[str]) -> str:
    """Normalize a user-successful manual query before persisting/reusing it."""
    query = " ".join((query or "").split()).strip(" .:：")
    if not query:
        return ""
    if len(query) > 120:
        return query[:120].strip()
    return query


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def resolve_tmdb_season_number(
    target: ScrapeTarget,
    tmdb_id: Optional[int],
    tmdb_type: str,
    requested_season_number: Optional[int] = None,
    tmdb_client: Optional[TMDBClient] = None,
) -> Optional[int]:
    """Resolve the TMDB season number for a local scrape target.

    Local cards may aggregate multiple seasons under one series. The correct
    TMDB mapping depends on the selected TMDB show:

    - CLANNAD Season 2 maps to TMDB show 24835 / Season 2.
    - A sequel that is a separate TMDB show maps to that show / Season 1.

    So we inspect the selected TMDB show's seasons first instead of guessing
    from the local title/subwork title.
    """
    if requested_season_number is not None:
        return requested_season_number
    if tmdb_type != "tv":
        return None
    if target.group_type == "special":
        return 0

    verified_season = match_verified_tmdb_season(
        int(tmdb_id or 0),
        " ".join(
            value
            for value in (
                target.source_subwork_dir,
                target.scrape_title,
                target.local_title,
            )
            if value
        ),
    )
    if verified_season is not None:
        return verified_season

    local_season_number = target.local_season_number or 1
    if not tmdb_id:
        return local_season_number

    try:
        client = tmdb_client or TMDBClient()
        detail = client.get_tv_detail(tmdb_id)
        seasons = detail.get("seasons") or []
        season_numbers = {
            season.get("season_number")
            for season in seasons
            if season.get("season_number") is not None
        }
        if local_season_number in season_numbers:
            return local_season_number

        if _should_map_to_absolute_tmdb_season_one(target, detail, local_season_number):
            return 1

        if target.scrape_year:
            for season in seasons:
                air_date = season.get("air_date") or ""
                season_number = season.get("season_number")
                if not season_number or len(air_date) < 4:
                    continue
                try:
                    if int(air_date[:4]) == target.scrape_year:
                        return season_number
                except ValueError:
                    continue

        if 1 in season_numbers and not _should_keep_local_season_on_tv_fallback(target, local_season_number):
            return 1
    except Exception:
        logger.debug("resolve tmdb season number failed", exc_info=True)

    return local_season_number


def _should_map_to_absolute_tmdb_season_one(target: ScrapeTarget, detail: dict, local_season_number: int) -> bool:
    """Detect TMDB shows that store several local seasons in Season 1."""
    if local_season_number <= 1:
        return False
    if target.group_type != "season" or target.scrape_type != "tv":
        return False
    if target.tmdb_hint_id and detail.get("id") != target.tmdb_hint_id:
        return False

    non_special_seasons = [
        season
        for season in detail.get("seasons") or []
        if int(season.get("season_number") or 0) > 0
    ]
    if len(non_special_seasons) != 1:
        return False
    only_season = non_special_seasons[0]
    if int(only_season.get("season_number") or 0) != 1:
        return False

    remote_count = int(only_season.get("episode_count") or 0)
    if remote_count <= 0:
        return False
    local_required = _cumulative_local_episode_count(target, local_season_number)
    return local_required > 0 and remote_count >= local_required


def _cumulative_local_episode_count(target: ScrapeTarget, local_season_number: int) -> int:
    try:
        from app.import_plan.store import load_import_plan

        plan = load_import_plan(plan_id=target.import_plan_id)
    except Exception:
        logger.debug("load import plan for absolute season detection failed", exc_info=True)
        plan = None
    if not plan:
        return target.local_episode_count or len(target.item_ids or [])

    counts: dict[int, set[int]] = {}
    for item in getattr(plan, "items", []) or []:
        if item.group_type != "season" or item.action != "generate_strm":
            continue
        if item.season_number is None or item.season_number > local_season_number:
            continue
        if not _same_scrape_series(target, item):
            continue
        ep_num = _local_episode_number(item)
        if ep_num is None:
            continue
        counts.setdefault(int(item.season_number), set()).add(int(ep_num))
    return sum(len(values) for season, values in counts.items() if season <= local_season_number)


def _should_keep_local_season_on_tv_fallback(target: ScrapeTarget, local_season_number: int) -> bool:
    """Avoid silently mapping plain same-series later seasons back to TMDB S1.

    Falling back to Season 1 is useful for independent sequel TV entries whose
    TMDB page only has one season.  It is harmful for a normal multi-season
    series directory such as "Re:Zero/Season 2" with a parent {tmdb-...} hint:
    if TMDB detail lookup is incomplete, the local season number is safer than
    writing S2/S3 metadata as S1.
    """
    if local_season_number <= 1:
        return False
    if target.group_type != "season" or target.card_type != "main_series":
        return False
    if (target.source_subwork_dir or "").strip():
        return False
    if not target.tmdb_hint_id:
        return False

    series_key = _title_key(target.series_group)
    if not series_key:
        return False
    local_key = _title_key(target.local_title or target.scrape_title)
    scrape_key = _title_key(target.scrape_title or target.local_title)
    return (not local_key or local_key == series_key) and (not scrape_key or scrape_key == series_key)


def _build_search_title_variants(title: str) -> List[str]:
    """生成少量保守搜索变体。

    目标是修复目录树噪声导致的 0 候选，例如：
    B 86-不存在的战区 -> 86 不存在的战区 / 86不存在的战区
    """
    if not title:
        return []

    variants = []
    raw_title = _strip_tmdb_hint_text(title)
    cleaned_title = _clean_release_search_title(raw_title)

    def add(value: str) -> None:
        value = " ".join((value or "").split()).strip(" .")
        if value and not _is_search_query_noise(value) and value not in variants:
            variants.append(value)

    search_bases: List[str] = []

    def add_base(value: str) -> None:
        value = " ".join((value or "").split()).strip(" .")
        if value and value not in search_bases:
            search_bases.append(value)

    add_base(cleaned_title)
    add_base(raw_title)

    for base_title in search_bases:
        add(base_title)

        normalized_symbols = (
            base_title.replace("〜", " ")
            .replace("～", " ")
            .replace("~", " ")
            .replace("－", "-")
            .replace("—", "-")
            .replace("–", "-")
        )
        add(normalized_symbols)
        add(_dot_title_to_space(normalized_symbols))

        for movie_variant in _build_movie_title_variants(normalized_symbols):
            add(movie_variant)

        # 常见“中文名.英文名”或“英文名.Japanese.Subtitle”保留了多个标题，
        # TMDB 搜索时分开查更稳。仅在清洗后的标题上拆点，避免把字幕组
        # [T.H.X] 拆成 H 这种错误作品名。
        for part in re.split(r"[.。]", normalized_symbols):
            add(part)

        cleaned = re.sub(r"^[A-Za-z]\s+(?=\d)", "", normalized_symbols).strip()
        add(cleaned)

        # 数字 + 中文之间的连接符经常影响搜索结果。
        hyphen_to_space = re.sub(r"(?<=\d)\s*[-－—–]\s*(?=[\u4e00-\u9fff])", " ", cleaned)
        add(hyphen_to_space)
        add(re.sub(r"(?<=\d)\s*[-－—–]\s*(?=[\u4e00-\u9fff])", "", cleaned))

        # 续作标题经常在 TMDB 中仍属于同一个 TV show，不一定能直接搜到
        # "After Story" 子标题。保留子标题用于评分和 season 推断，同时增加
        # 主标题回退，例：CLANNAD After Story -> CLANNAD。
        sequel_patterns = [
            r"\bafter\s+story\b.*$",
            r"\bseason\s+\d+\b.*$",
            r"\bs\d+\b.*$",
            r"\bpart\s+\d+\b.*$",
        ]
        for pattern in sequel_patterns:
            add(re.sub(pattern, "", normalized_symbols, flags=re.I))

    return variants[:12]


def _build_movie_title_variants(title: str) -> List[str]:
    """Generate movie-specific subtitle variants.

    Examples:
    - 钢之炼金术师 FA 剧场版：叹息之丘的圣星 -> 叹息之丘的圣星
    - 剧场版 命运石之门：负荷领域的既视感 -> 负荷领域的既视感

    TMDB often indexes anime movies by the subtitle plus year. These variants
    mirror the manual search flow without weakening TV search behavior.
    """
    if not title:
        return []

    variants: List[str] = []

    def add(value: str) -> None:
        value = " ".join((value or "").split()).strip(" .:：")
        if value and value not in variants:
            variants.append(value)

    # Keep the right side of the last colon. This is the most useful manual
    # query for titles like “剧场版：叹息之丘的圣星”.
    colon_parts = re.split(r"[:：]", title)
    if len(colon_parts) > 1:
        right = colon_parts[-1]
        add(right)
        add(_strip_movie_chapter_prefix(right))

    # Remove common descriptive movie markers. They are useful for local
    # grouping but often hurt TMDB search; manual scraping usually succeeds
    # after dropping words such as “总集篇”.
    marker_stripped = _strip_movie_descriptors(title)
    add(marker_stripped)
    add(_dot_title_to_space(marker_stripped))
    if marker_stripped != title:
        marker_parts = re.split(r"[:：]", marker_stripped)
        if len(marker_parts) > 1:
            right = marker_parts[-1]
            add(right)
            add(_strip_movie_chapter_prefix(right))

    # Movie folders often keep bilingual names joined by dots:
    # 红猪.Porco.Rosso.1992 -> Porco Rosso
    # 千年女优.Millennium.Actress -> Millennium Actress
    ascii_parts = re.findall(r"[A-Za-z][A-Za-z0-9'!&-]*(?:[. ][A-Za-z0-9'!&-]+)+", title)
    for part in ascii_parts:
        add(_dot_title_to_space(part))

    return variants


def _strip_movie_chapter_prefix(title: str) -> str:
    """Drop local chapter labels before the concrete movie subtitle."""
    cleaned = " ".join((title or "").split()).strip(" .:：")
    cleaned = re.sub(
        r"^(?:进击篇|進擊篇|序列之争|序列之爭|前篇|後篇|后篇|前編|後編|第[一二三四五六七八九十\d]+[章部篇]|"
        r"Part\s*\d+|Chapter\s*\d+)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" .:：")


def _dot_title_to_space(title: str) -> str:
    """Convert release-title dots between words to spaces for TMDB search."""
    cleaned = title or ""
    cleaned = re.sub(r"(?<=[A-Za-z])\.(?=[A-Za-z])", " ", cleaned)
    cleaned = re.sub(r"(?<=[\u4e00-\u9fff])\.(?=[A-Za-z])", " ", cleaned)
    cleaned = re.sub(r"(?<=[A-Za-z])\.(?=[\u4e00-\u9fff])", " ", cleaned)
    return " ".join(cleaned.split()).strip(" .:：")


def _strip_movie_descriptors(title: str) -> str:
    """Remove local anime-movie descriptor words from a search title."""
    cleaned = title or ""
    descriptors = [
        "总集篇",
        "總集篇",
        "剧场版",
        "劇場版",
        "映画",
        "The Movie",
        "Movie",
    ]
    for descriptor in descriptors:
        cleaned = re.sub(re.escape(descriptor), " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*[:：]\s*", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" .:：")


def _strip_tmdb_hint_text(title: str) -> str:
    cleaned = re.sub(r"\s*\{\s*(?:tmdb|tmdbid)\s*[-_:：]?\s*\d+\s*\}\s*", " ", title or "", flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()


def _clean_release_search_title(title: str) -> str:
    """Remove release-group and encode brackets before sending a title to search."""
    cleaned = clean_work_title_container(title or "").title
    cleaned = _strip_tmdb_hint_text(cleaned)
    return cleaned or _strip_tmdb_hint_text(title or "")


def _is_search_query_noise(value: str) -> bool:
    """Reject release-group/codec fragments that should never hit TMDB search."""
    query = " ".join((value or "").split()).strip(" .:：")
    if not query:
        return True

    if any(ch in query for ch in "[]【】（）()") and _clean_release_search_title(query) != query:
        return True
    if query.startswith(("[", "【", "(", "（")) or query.endswith(("]", "】", ")", "）")):
        return True

    # If the recognition title cleaner can extract a different useful title from
    # a bracket-heavy release name, use that extracted title instead of searching
    # the raw release string.
    if re.search(r"[\[【（(]", query) and _clean_release_search_title(query) != query:
        return True

    if re.fullmatch(r"[A-Za-z]", query):
        return True
    if re.fullmatch(r"\d{4}", query):
        return True

    compact = re.sub(r"[\s._:/\\()[\]【】（）{}+-]+", " ", query).strip().lower()
    if not compact:
        return True

    release_group_markers = [
        "vcb studio",
        "vcb",
        "t h x",
        "thx",
        "nekomoe",
        "lolihouse",
        "kissaten",
        "sakurato",
        "sairo",
        "saio raws",
        "raws",
        "fansub",
        "字幕组",
    ]
    if any(marker in compact for marker in release_group_markers):
        return True

    tech_markers = [
        "webrip",
        "web dl",
        "bdrip",
        "bdmv",
        "remux",
        "blu ray",
        "bluray",
        "hevc",
        "h264",
        "h265",
        "x264",
        "x265",
        "avc",
        "aac",
        "flac",
        "opus",
        "ma10p",
        "hi10p",
        "10bit",
        "8bit",
        "1080p",
        "2160p",
        "720p",
    ]
    if any(marker in compact for marker in tech_markers):
        return True

    if re.fullmatch(r"\d+\s*[-~～]\s*\d+", compact):
        return True
    if re.fullmatch(r"(?:\d{3,4}p|\d+\s*bit|ma\d+p|hi\d+p)(?:\s+\w+)*", compact):
        return True

    return False


def _normalize_text(text: str) -> str:
    """归一化文本用于比较"""
    return re.sub(r"[\s\-_.:：。·]+", "", text.lower())


def _compute_candidate_score(
    result: dict,
    target: ScrapeTarget,
    tmdb_type: str,
) -> Tuple[float, list]:
    """计算候选匹配分数和原因

    评分维度：
    - 标题相似度
    - 年份匹配
    - media_type 匹配
    - popularity
    - vote_average
    - original_title 命中

    返回: (score, reasons)
    """
    score = 0.0
    reasons = []

    if target.tmdb_hint_id and result.get("id") == target.tmdb_hint_id:
        score += 120
        reasons.append(f"TMDB ID 命中({target.tmdb_hint_id})")

    title = result.get("name") or result.get("title") or ""
    original_title = result.get("original_name") or result.get("original_title") or ""
    year = None
    date_str = result.get("first_air_date") or result.get("release_date") or ""
    if date_str and len(date_str) >= 4:
        try:
            year = int(date_str[:4])
        except ValueError:
            pass

    # 标题匹配
    search_title = target.scrape_title or target.local_title
    if search_title:
        norm_search = _normalize_text(search_title)
        norm_title = _normalize_text(title)
        norm_original = _normalize_text(original_title)

        if norm_search == norm_title or (norm_original and norm_search == norm_original):
            score += 40
            reasons.append("标题完全匹配")
        elif norm_title and norm_search in norm_title:
            score += 25
            reasons.append("标题部分匹配")
        elif norm_original and norm_search in norm_original:
            score += 25
            reasons.append("原名部分匹配")
        elif (
            (norm_title and norm_search.startswith(norm_title) and len(norm_title) >= 3)
            or (norm_original and norm_search.startswith(norm_original) and len(norm_original) >= 3)
        ):
            if target.scrape_type == "movie":
                score += 12
                reasons.append("候选标题过宽")
            else:
                score += 34
                reasons.append("候选标题前缀匹配")
        elif (norm_title and norm_title in norm_search) or (norm_original and norm_original in norm_search):
            score += 12
            reasons.append("候选标题过宽")

        similarity = _best_title_similarity(norm_search, norm_title, norm_original)
        if similarity >= 0.58:
            score += 24
            reasons.append("标题高度相似")
        elif similarity >= 0.42:
            score += 16
            reasons.append("标题相似")
        elif similarity >= 0.30:
            score += 8
            reasons.append("标题有重合")

        if target.scrape_type == "movie":
            marker_delta, marker_reason = _movie_part_marker_score(search_title, title, original_title)
            if marker_delta:
                score += marker_delta
                reasons.append(marker_reason)

    # 年份匹配
    search_year = target.scrape_year or target.local_year
    if search_year and year:
        diff = abs(search_year - year)
        if diff == 0:
            score += 30
            reasons.append("年份匹配")
        elif diff <= 1:
            score += 15
            reasons.append(f"年份接近（差{diff}年）")
        elif diff <= 3:
            score += 5
            reasons.append(f"年份略有偏差（差{diff}年）")

    # media_type 匹配（TMDB 搜索结果的 media_type）
    result_media_type = result.get("media_type", "")
    if result_media_type and result_media_type == tmdb_type:
        score += 10
        reasons.append("类型匹配")

    # popularity 加分（对数缩放）
    popularity = result.get("popularity", 0)
    if popularity > 0:
        import math
        pop_score = min(10, math.log10(max(1, popularity)) * 3)
        score += pop_score
        if popularity > 50:
            reasons.append(f"高热度({popularity:.0f})")

    # vote_average 加分
    vote = result.get("vote_average", 0)
    if vote >= 7:
        score += 5
        reasons.append(f"高评分({vote:.1f})")

    return round(score, 1), reasons


def _best_title_similarity(norm_search: str, norm_title: str, norm_original: str) -> float:
    """Return a tolerant similarity for multilingual movie titles."""
    if not norm_search:
        return 0.0
    candidates = [v for v in (norm_title, norm_original) if v]
    if not candidates:
        return 0.0
    return max(SequenceMatcher(None, norm_search, candidate).ratio() for candidate in candidates)


def _movie_part_marker_score(search_title: str, title: str, original_title: str) -> Tuple[float, str]:
    """Score 前篇/后篇 style markers for two-part anime movies."""
    target_marker = _movie_part_marker(search_title)
    if not target_marker:
        return 0.0, ""
    candidate_marker = _movie_part_marker(f"{title} {original_title}")
    if not candidate_marker:
        return 0.0, ""
    if target_marker == candidate_marker:
        return 18.0, "篇章匹配"
    return -35.0, "篇章不匹配"


def _movie_part_marker(text: str) -> str:
    normalized = text or ""
    if re.search(r"前篇|前編|part\s*1|part\s*i\b", normalized, re.IGNORECASE):
        return "front"
    if re.search(r"后篇|後篇|后编|後編|part\s*2|part\s*ii\b", normalized, re.IGNORECASE):
        return "back"
    # 《孤独摇滚》剧场总集篇使用 Re: / Re:Re: 区分上下篇。
    # 仅识别标题尾部标记，避免普通英文单词中的 "re" 被误判。
    compact = re.sub(r"[\s._-]+$", "", normalized).casefold()
    if re.search(r"re\s*:\s*re\s*:?$", compact):
        return "back"
    if re.search(r"re\s*:?$", compact):
        return "front"
    return ""


def _tmdb_result_to_candidate(result: dict, target: ScrapeTarget, tmdb_type: str) -> ScrapeCandidate:
    """将 TMDB 搜索结果转为 ScrapeCandidate"""
    title = result.get("name") or result.get("title") or ""
    original_title = result.get("original_name") or result.get("original_title") or ""
    year = None
    date_str = result.get("first_air_date") or result.get("release_date") or ""
    if date_str and len(date_str) >= 4:
        try:
            year = int(date_str[:4])
        except ValueError:
            pass

    score, reasons = _compute_candidate_score(result, target, tmdb_type)

    return ScrapeCandidate(
        candidate_id=hashlib.md5(f"{target.scrape_target_id}:{result.get('id')}".encode()).hexdigest()[:12],
        scrape_target_id=target.scrape_target_id,
        provider="tmdb",
        tmdb_id=result.get("id", 0),
        tmdb_type=tmdb_type,
        title=title,
        original_title=original_title,
        year=year,
        overview=result.get("overview", ""),
        poster_path=result.get("poster_path", ""),
        popularity=result.get("popularity", 0),
        vote_average=result.get("vote_average", 0),
        score=score,
        reasons=reasons,
        raw=result,
    )


def _candidate_from_tmdb_hint(target: ScrapeTarget, client: TMDBClient) -> Optional[ScrapeCandidate]:
    """Build a high-confidence candidate directly from {tmdb-...} hints."""
    if not target.tmdb_hint_id:
        return None

    hint_type = target.tmdb_hint_type or target.scrape_type
    if hint_type not in {"tv", "movie"}:
        hint_type = target.scrape_type

    try:
        if hint_type == "movie":
            detail = client.get_movie_detail(target.tmdb_hint_id)
            tmdb_type = "movie"
        else:
            detail = client.get_tv_detail(target.tmdb_hint_id)
            tmdb_type = "tv"
    except Exception:
        logger.debug("tmdb hint lookup failed", exc_info=True)
        return None

    result = dict(detail)
    result["id"] = target.tmdb_hint_id
    candidate = _tmdb_result_to_candidate(result, target, tmdb_type)
    if f"TMDB ID 命中({target.tmdb_hint_id})" not in candidate.reasons:
        candidate.reasons.insert(0, f"TMDB ID 命中({target.tmdb_hint_id})")
    candidate.score = max(candidate.score, 150.0)
    return candidate


def execute_scrape(
    target: ScrapeTarget,
    tmdb_id: int,
    tmdb_type: str,
    tmdb_season_number: Optional[int] = None,
    selected_by: str = "manual",
    search_query: Optional[str] = None,
    tmdb_client: Optional[TMDBClient] = None,
    include_episode: bool = True,
    rescan_after: bool = False,
    artwork_mode: Optional[str] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
    trusted_series_binding: bool = False,
    identity_evidence: Optional[dict] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    library_work_id: str | None = None,
) -> dict:
    """执行刮削：拉取详情、下载图片、生成 NFO、写入 scrape_map

    返回 result dict。任何异常都记录到 failed_cases.json 并抛出。
    """
    # Review Fix 2 defense-in-depth：V3 stale target 在创建 TMDB client /
    # 请求网络 / 写文件之前直接拒绝（0 网络 0 NFO 0 binding 0 artifact）。
    from app.import_plan import revision_store
    from app.scrape.effective_store import is_v3_revision

    if is_v3_revision(target.import_plan_id) and not revision_store.is_current_revision(
        target.import_plan_id
    ):
        return {
            "status": "obsolete",
            "error": "该 V3 计划已被新版本取代（stale revision），不能执行刮削",
            "scrape_target_id": target.scrape_target_id,
        }
    owns_client = tmdb_client is None
    client = tmdb_client or TMDBClient()
    warnings = []
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    logs = []
    progress_cursor = 8

    def _ensure_not_cancelled() -> None:
        """写入文件前检查取消状态，避免写入半文件。"""
        if should_cancel and should_cancel():
            raise TaskCancelledError()

    if progress_callback and log_callback is None:
        def _progress_log(message: str, kind: str = "info") -> None:
            nonlocal progress_cursor
            logs.append({
                "time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "kind": kind,
                "message": message,
            })
            del logs[:-160]
            progress_cursor = min(96, progress_cursor + 3)
            progress_callback(progress_cursor, message, {
                "logs": list(logs),
                "current_target": target.scrape_title or target.local_title,
                "total_targets": 1,
                "current_index": 1,
                "auto_scraped": 0,
                "review_queued": 0,
                "failed": 0,
            })
        log_callback = _progress_log

    try:
        # 获取详情。仅在完成后记录，避免同一步骤输出“开始/完成”两条日志。
        if tmdb_type == "tv":
            detail = client.get_tv_detail(tmdb_id)
        else:
            detail = client.get_movie_detail(tmdb_id)
        _ensure_not_cancelled()
        images = detail.get("images") or {}

        validation_issues = validate_scrape_metadata(
            target=target,
            detail=detail,
            tmdb_type=tmdb_type,
            tmdb_season_number=tmdb_season_number,
            tmdb_client=client,
            trusted_series_binding=trusted_series_binding,
        )
        _ensure_not_cancelled()
        for message in issue_messages(validation_issues):
            if message not in warnings:
                warnings.append(message)
                _emit_log(log_callback, f"元数据校验提示：{message}", "warn")

        title = detail.get("name") or detail.get("title") or target.scrape_title
        _emit_log(log_callback, f"TMDB 详情完成：{title}", "done")
        original_title = detail.get("original_name") or detail.get("original_title") or ""
        overview = detail.get("overview", "")
        rating = _as_float(detail.get("vote_average"))
        from app.core.config import load_config
        from app.scrape.certification import extract_certification
        region_config = load_config().tmdb_certification_regions
        certification = extract_certification(
            detail, tmdb_type,
            [part.strip() for part in region_config.split(",") if part.strip()],
        )
        genres = _extract_names(detail.get("genres", []))
        studios = _extract_names(detail.get("production_companies", []))
        cast = _extract_cast(detail, client)
        runtime = _extract_runtime(detail)
        year = None
        date_str = detail.get("first_air_date") or detail.get("release_date") or ""
        if date_str and len(date_str) >= 4:
            try:
                year = int(date_str[:4])
            except ValueError:
                pass

        # 目标目录
        target_dir = target.target_dir
        if not target_dir:
            from app.core.paths import get_mirror_root
            target_dir = str(
                get_mirror_root()
                / _source_namespace(target.source)
                / _sanitize(target.series_group or target.local_title)
            )
        asset_dir = _asset_dir_for_target(target, target_dir)

        # 图片处理。默认保存远程 URL，让首次刮削更快；需要离线/Kodi 兼容时可切到 local。
        poster_path = ""
        fanart_path = ""
        clearlogo_path = ""
        download_artwork = _should_download_artwork(target, artwork_mode)

        poster_file = _select_image(client, "poster", images) or detail.get("poster_path")
        if poster_file:
            if download_artwork:
                _ensure_not_cancelled()
                dest = Path(asset_dir) / "poster.jpg"
                if client.download_image(poster_file, dest):
                    poster_path = str(dest)
                    _emit_log(log_callback, f"海报完成：{title}", "done")
                else:
                    warnings.append("poster 下载失败")
                    _emit_log(log_callback, f"海报下载失败：{title}", "warn")
            else:
                poster_path = _tmdb_image_url(client, poster_file, "w780")
                _emit_log(log_callback, f"海报地址完成：{title}", "done")

        backdrop_file = _select_image(client, "backdrop", images) or detail.get("backdrop_path")
        if backdrop_file:
            if download_artwork:
                _ensure_not_cancelled()
                dest = Path(asset_dir) / "fanart.jpg"
                if client.download_image(backdrop_file, dest):
                    fanart_path = str(dest)
                    _emit_log(log_callback, f"背景图完成：{title}", "done")
                else:
                    warnings.append("fanart 下载失败")
                    _emit_log(log_callback, f"背景图下载失败：{title}", "warn")
            else:
                fanart_path = _tmdb_image_url(client, backdrop_file, "original")
                _emit_log(log_callback, f"背景图地址完成：{title}", "done")

        logo_file = _select_image(client, "logo", images)
        if logo_file:
            if download_artwork:
                _ensure_not_cancelled()
                logo_ext = Path(logo_file).suffix.lower()
                dest_name = "clearlogo.svg" if logo_ext == ".svg" else "clearlogo.png"
                dest = Path(asset_dir) / dest_name
                if client.download_image(logo_file, dest):
                    clearlogo_path = str(dest)
                    _emit_log(log_callback, f"Logo 完成：{title}", "done")
            else:
                clearlogo_path = _tmdb_image_url(client, logo_file, "original")
                _emit_log(log_callback, f"Logo 地址完成：{title}", "done")

        # 生成 NFO
        nfo_path = ""
        _ensure_not_cancelled()
        if tmdb_type == "tv":
            nfo_content = generate_tvshow_nfo(
                title=title, original_title=original_title,
                year=year, plot=overview, tmdb_id=tmdb_id,
                season=tmdb_season_number or target.local_season_number,
                rating=rating, genres=genres, studios=studios,
                premiered=detail.get("first_air_date", ""),
                runtime=runtime,
                certification=certification.value,
                certification_country=certification.country,
                cast=cast,
            )
            nfo_path = write_nfo(target_dir, "tvshow.nfo", nfo_content)
            _emit_log(log_callback, f"剧集 NFO 完成：{title}", "done")
        else:
            nfo_content = generate_movie_nfo(
                title=title, original_title=original_title,
                year=year, plot=overview, tmdb_id=tmdb_id,
                rating=rating, genres=genres, studios=studios,
                releasedate=detail.get("release_date", ""),
                runtime=runtime,
                certification=certification.value,
                certification_country=certification.country,
                cast=cast,
            )
            nfo_path = write_nfo(target_dir, "movie.nfo", nfo_content)
            _emit_log(log_callback, f"电影 NFO 完成：{title}", "done")

        _ensure_not_cancelled()
        # 写入 scrape_map
        map_item = ScrapeMapItem(
            scrape_target_id=target.scrape_target_id,
            work_id=target.work_id,
            canonical_work_id=target.canonical_work_id,
            source=target.source,
            import_plan_id=target.import_plan_id,
            card_type=target.card_type,
            media_type=target.media_type,
            series_group=target.series_group,
            local_title=target.local_title,
            original_title=target.original_title,
            source_subwork_dir=target.source_subwork_dir,
            local_year=target.local_year,
            local_season_number=target.local_season_number,
            scrape_title=target.scrape_title,
            scrape_year=target.scrape_year,
            search_query=_clean_saved_search_query(search_query),
            tmdb_id=tmdb_id,
            tmdb_type=tmdb_type,
            tmdb_season_number=tmdb_season_number or target.local_season_number,
            selected_by=selected_by,
            confidence="high" if year else "medium",
            identity_evidence=dict(identity_evidence or {}),
            scraped_at=now,
            nfo_path=nfo_path,
            poster_path=poster_path,
            fanart_path=fanart_path,
            clearlogo_path=clearlogo_path,
        )
        # Module 5：按 plan 代次分流写入（V3 → SQLite stable binding；legacy → JSON）
        from app.scrape.effective_store import upsert_effective_scrape_map_item

        upsert_effective_scrape_map_item(map_item)
        _emit_log(log_callback, f"刮削映射完成：{title}", "done")

        # Episode NFO 生成（TV 类型）
        episode_results = []
        if include_episode and tmdb_type == "tv" and target.item_ids:
            _ensure_not_cancelled()
            try:
                episode_results = _generate_episode_nfos(
                    target=target,
                    tmdb_id=tmdb_id,
                    tmdb_season_number=tmdb_season_number or target.local_season_number,
                    target_dir=target_dir,
                    client=client,
                    fallback_thumb_path=fanart_path or poster_path,
                    log_callback=log_callback,
                    series_detail=detail,
                    should_cancel=should_cancel,
                    download_artwork=download_artwork,
                )
            except TaskCancelledError:
                raise
            except Exception as e:
                warnings.append(f"分集信息生成跳过: {e}")
                _emit_log(log_callback, f"分集信息生成跳过：{title}，{e}", "warn")

        # Module 5：V3 刮削成功后登记实际产生的本地产物（NFO/图片/episode NFO），
        # 远程 artwork URL 不登记；legacy 路径不进入 artifact_records。
        from app.scrape.effective_store import register_scrape_artifacts

        register_scrape_artifacts(
            map_item,
            [str(entry.get("nfo_path") or "") for entry in episode_results if entry.get("nfo_path")],
        )

        if rescan_after:
            try:
                from app.library.service import refresh_library_for_scrape_target
                if library_work_id:
                    refresh_library_for_scrape_target(target, library_work_id=library_work_id)
                else:
                    refresh_library_for_scrape_target(target)
                _emit_log(log_callback, f"媒体库刷新完成：{title}", "done")
            except Exception as e:
                _emit_log(log_callback, f"媒体库索引刷新失败：{e}", "warn")
                raise RuntimeError(
                    f"刮削文件已生成，但媒体库索引刷新失败：{e}"
                ) from e

        return {
            "scrape_target_id": target.scrape_target_id,
            "tmdb_id": tmdb_id,
            "tmdb_type": tmdb_type,
            "nfo_path": nfo_path,
            "poster_path": poster_path,
            "fanart_path": fanart_path,
            "clearlogo_path": clearlogo_path,
            "scrape_map_path": str(_get_data_dir() / "scrape" / "scrape_map.json"),
            "episode_count": len(episode_results),
            "episode_nfos": episode_results,
            "warnings": warnings,
            "logs": logs,
            "current_target": target.scrape_title or target.local_title,
            "total_targets": 1,
            "current_index": 1,
            "auto_scraped": 1,
            "review_queued": 0,
            "failed": 0,
        }

    except TaskCancelledError:
        raise
    except Exception as e:
        save_failed_case({
            **build_failed_case(
                target=target,
                error=e,
                stage="execute_scrape",
                tmdb_id=tmdb_id,
                tmdb_type=tmdb_type,
                extra={"timestamp": now},
            ),
        }) 
        raise
    finally:
        if owns_client:
            client.close()


def _emit_log(log_callback: Optional[Callable[[str, str], None]], message: str, kind: str = "info") -> None:
    """向调用方写入面向用户的刮削日志。"""
    if log_callback:
        log_callback(message, kind)


def _source_namespace(source: str) -> str:
    mapping = {"pan115": "115", "baidu": "baidu", "local": "local", "openlist": "openlist"}
    return mapping.get(source, source)


def _asset_dir_for_target(target: ScrapeTarget, target_dir: str) -> str:
    """Return the directory used for poster/fanart/clearlogo assets.

    TV season targets share series-level artwork.  Keeping one copy at the
    series root avoids repeatedly downloading identical large images for every
    season while still letting all seasons reference the same paths.
    """
    directory = Path(target_dir)
    if target.media_type == "tv" and target.group_type in {"season", "special", "sps"}:
        if re.match(r"^Season\s+\d+$", directory.name, flags=re.IGNORECASE):
            return str(directory.parent)
    return str(directory)


def _should_download_artwork(target: ScrapeTarget, artwork_mode: Optional[str] = None) -> bool:
    """Return whether scrape should synchronously download artwork files."""
    if artwork_mode is None:
        from app.core.config import load_config

        artwork_mode = getattr(load_config(), "artwork_storage_mode", "remote")
    mode = str(artwork_mode or "remote").strip().lower()
    if mode == "local":
        return True
    if mode == "auto":
        return target.source == "local"
    return False


def _tmdb_image_url(client, file_path: str, size: str) -> str:
    if not file_path:
        return ""
    if re.match(r"^https?://", file_path, flags=re.IGNORECASE):
        # SSRF/信任边界：完整远程 URL 必须命中受信任 CDN（TMDB/AniList），
        # 否则不写入镜像/索引（与 assets /remote 代理同一校验规则）。
        from app.core.url_guard import validate_remote_asset_url

        try:
            validate_remote_asset_url(file_path)
        except ValueError:
            return ""
        return file_path
    builder = getattr(client, "build_image_url", None)
    if callable(builder):
        return builder(file_path, size)
    return f"https://image.tmdb.org/t/p/{size}{file_path}"


def _select_image(client, kind: str, images: dict) -> Optional[str]:
    """调用 TMDBClient 的图片选择算法，兼容测试 mock。"""
    method_map = {
        "poster": "select_best_poster",
        "backdrop": "select_best_backdrop",
        "logo": "select_best_logo",
        "still": "select_best_still",
    }
    method_name = method_map.get(kind, "")
    method = getattr(client, method_name, None)
    if callable(method):
        return method(images)

    key_map = {
        "poster": "posters",
        "backdrop": "backdrops",
        "logo": "logos",
        "still": "stills",
    }
    values = images.get(key_map.get(kind, ""), [])
    if values:
        return values[0].get("file_path")
    return None


def _extract_names(items: list) -> list:
    """从 TMDB genre/company 列表提取 name。"""
    names = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                names.append(str(name))
    return names


def _as_float(value) -> float:
    """宽松转换评分。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _extract_runtime(detail: dict) -> int:
    """提取 movie runtime 或 tv episode_run_time。"""
    runtime = detail.get("runtime")
    if isinstance(runtime, int):
        return runtime
    if isinstance(runtime, (float, str)):
        try:
            return int(float(runtime))
        except (TypeError, ValueError):
            return 0

    episode_times = detail.get("episode_run_time") or []
    for value in episode_times:
        try:
            minutes = int(float(value))
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            return minutes
    return 0


def _sanitize(name: str) -> str:
    if not name:
        return "unnamed"
    illegal = set('\\/:*?"<>|')
    cleaned = "".join("_" if c in illegal else c for c in name)
    return cleaned.strip(" .") or "unnamed"


def _get_data_dir() -> Path:
    from app.core.paths import get_data_dir
    return get_data_dir()


def _cache_candidates(candidates: List[ScrapeCandidate]) -> None:
    """将候选双写到 SQLite 缓存；失败不影响主流程。"""
    if not candidates:
        return
    try:
        from app.db.database import close_connection, init_db
        from app.db.candidates import save_candidates
        init_db()
        save_candidates(candidates)
    except Exception:
        logger.debug("写入 scrape candidate cache 失败", exc_info=True)
    finally:
        try:
            from app.db.database import close_connection
            close_connection()
        except Exception:
            pass


def _generate_episode_nfos(
    target: ScrapeTarget,
    tmdb_id: int,
    tmdb_season_number: int,
    target_dir: str,
    client: TMDBClient,
    fallback_thumb_path: str = "",
    log_callback: Optional[Callable[[str, str], None]] = None,
    series_detail: Optional[dict] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    download_artwork: bool = True,
) -> List[dict]:
    """为 TV 目标批量生成 episode NFO

    从 ImportPlan 的 item_ids 获取 episode 列表，
    调用 TMDB episode detail 生成 NFO 和下载剧照。

    单集失败不影响主作品刮削成功。
    """
    from app.import_plan.store import load_import_plan

    results = []

    def ensure_not_cancelled() -> None:
        if should_cancel and should_cancel():
            raise TaskCancelledError()

    ensure_not_cancelled()

    # 从 ImportPlan 获取 episode 条目
    plan = load_import_plan(plan_id=target.import_plan_id)
    if not plan:
        _emit_log(log_callback, "导入计划不存在，跳过分集信息", "warn")
        return results

    # 筛选属于该 target 的 season/special 条目。special 在 TMDB 中对应 Season 0，
    # 很多目录树会把 S00E05 这类条目放在 Season 0 下，但导入计划不会写
    # episode_number，需要从文件名补出来。
    item_ids = set(target.item_ids)
    episode_items_by_key: dict[tuple[int, int], tuple[ImportPlanItem, int]] = {}
    for item in plan.items:
        if item.id not in item_ids or item.group_type not in {"season", "special"}:
            continue
        ep_num = _local_episode_number(item)
        if item.season_number is None or ep_num is None:
            continue
        local_season = int(item.season_number or target.local_season_number or tmdb_season_number)
        key = (local_season, int(ep_num))
        existing = episode_items_by_key.get(key)
        if existing is None or (not existing[0].title and item.title):
            episode_items_by_key[key] = (item, ep_num)

    episode_items = sorted(
        episode_items_by_key.values(),
        key=lambda pair: (int(pair[0].season_number or 0), int(pair[1])),
    )

    if not episode_items:
        _emit_log(log_callback, "未找到可匹配的本地正片条目，跳过分集信息", "warn")
        return results
    episode_numbers = [int(ep_num) for _, ep_num in episode_items]
    first_episode = min(episode_numbers)
    last_episode = max(episode_numbers)
    episode_range = str(first_episode) if first_episode == last_episode else f"{first_episode}-{last_episode}"
    work_title = target.scrape_title or target.local_title or target.series_group or "未命名作品"
    _emit_log(
        log_callback,
        f"正在刮削《{work_title}》分集 {episode_range}（共 {len(episode_items)} 集）",
        "info",
    )

    flattened_absolute = _uses_flattened_local_absolute_numbering(
        target,
        tmdb_season_number,
        series_detail or {},
        episode_items,
    )
    episode_locations: list[tuple[ImportPlanItem, int, int, int]] = []
    for item, ep_num in episode_items:
        if flattened_absolute:
            mapped = _tmdb_season_episode_from_absolute(ep_num, series_detail or {})
            remote_season, remote_episode = mapped or (tmdb_season_number, ep_num)
        else:
            remote_season, remote_episode = tmdb_season_number, ep_num
        episode_locations.append((item, ep_num, remote_season, remote_episode))

    season_episodes_by_number: dict[int, dict[int, dict]] = {}
    failed_seasons: set[int] = set()
    for remote_season in sorted({location[2] for location in episode_locations}):
        ensure_not_cancelled()
        try:
            get_season = (
                getattr(client, "get_tv_season_episodes", None)
                or client.get_tv_season_detail
            )
            season_detail = get_season(tmdb_id, remote_season)
            ensure_not_cancelled()
            season_episodes_by_number[remote_season] = {
                ep.get("episode_number"): ep
                for ep in season_detail.get("episodes", [])
            }
        except TaskCancelledError:
            raise
        except Exception as e:
            logger.warning(f"获取 season {remote_season} detail 失败: {e}")
            _emit_log(
                log_callback,
                f"TMDB 第 {remote_season} 季分集读取失败，使用本地占位信息：{e}",
                "warn",
            )
            season_episodes_by_number[remote_season] = {}
            failed_seasons.add(remote_season)

    default_season_episodes = season_episodes_by_number.get(tmdb_season_number, {})
    absolute_episode_offset = 0 if flattened_absolute else _tmdb_absolute_episode_offset(
        target=target,
        plan=plan,
        tmdb_season_number=tmdb_season_number,
        season_episodes=default_season_episodes,
        episode_items=episode_items,
    )
    if flattened_absolute:
        boundaries = " + ".join(
            str(count)
            for _, count in _tmdb_non_special_season_counts(series_detail or {})
        )
        _emit_log(
            log_callback,
            f"检测到本地连续集数，按 TMDB 季度边界拆分：{boundaries}",
            "info",
        )
    if absolute_episode_offset:
        _emit_log(
            log_callback,
            f"检测到 TMDB 绝对集数映射：本地 Season {target.local_season_number} 偏移 {absolute_episode_offset} 集",
            "info",
        )

    for item, ep_num, mapped_season, mapped_episode in episode_locations:
        ensure_not_cancelled()
        local_season_number = item.season_number or target.local_season_number or tmdb_season_number
        remote_season_number = mapped_season
        tmdb_episode_number = mapped_episode + absolute_episode_offset
        try:
            # 优先使用 season detail 中的分集信息。TMDB season detail 已包含
            # name/overview/air_date/still_path，避免每集再请求一次，大幅提升自动刮削速度。
            ep_detail = season_episodes_by_number.get(remote_season_number, {}).get(tmdb_episode_number)
            if not ep_detail and remote_season_number not in failed_seasons:
                try:
                    ep_detail = client.get_tv_episode_detail(
                        tmdb_id, remote_season_number, tmdb_episode_number
                    )
                    ensure_not_cancelled()
                except TaskCancelledError:
                    raise
                except Exception:
                    ep_detail = None

            if not ep_detail:
                ensure_not_cancelled()
                local_episode_title = _useful_local_episode_title(
                    item.title, local_season_number, ep_num
                )
                placeholder_title = local_episode_title or f"第 {ep_num} 集"
                strm_dir = str(Path(item.target_strm_path).parent) if item.target_strm_path else target_dir
                nfo_path = write_nfo(
                    strm_dir,
                    f"S{local_season_number:02d}E{ep_num:02d}.nfo",
                    generate_episode_nfo(
                        title=placeholder_title,
                        season=local_season_number,
                        episode=ep_num,
                        tmdb_id=tmdb_id,
                        thumb=fallback_thumb_path,
                        metadata_pending=True,
                    ),
                )
                results.append({
                    "episode": f"S{local_season_number:02d}E{ep_num:02d}",
                    "tmdb_episode": f"S{remote_season_number:02d}E{tmdb_episode_number:02d}",
                    "status": "metadata_pending",
                    "reason": "在线元数据尚未收录",
                    "title": placeholder_title,
                    "nfo_path": nfo_path,
                    "thumb_path": fallback_thumb_path,
                })
                continue

            # 提取信息
            # 目录树已经包含明确集标题时，它比远端语言回退更贴近用户的
            # 本地命名；发行版只有集号时 item.title 为空，仍由 TMDB 补齐。
            local_episode_title = _useful_local_episode_title(
                item.title, local_season_number, ep_num
            )
            ep_title = local_episode_title or ep_detail.get("name") or ""
            ep_plot = ep_detail.get("overview") or ""
            ep_runtime = ep_detail.get("runtime") or 0
            ep_aired = ep_detail.get("air_date", "")
            ep_still = ep_detail.get("still_path", "")

            strm_dir = str(Path(item.target_strm_path).parent) if item.target_strm_path else target_dir
            thumb_path = fallback_thumb_path
            if ep_still:
                if download_artwork:
                    thumb_file = Path(strm_dir) / f"S{local_season_number:02d}E{ep_num:02d}-thumb.jpg"
                    if client.download_image(ep_still, thumb_file, size="w500"):
                        thumb_path = str(thumb_file)
                    ensure_not_cancelled()
                else:
                    thumb_path = _tmdb_image_url(client, ep_still, "w500")

            # 生成 episode NFO
            ensure_not_cancelled()
            nfo_filename = f"S{local_season_number:02d}E{ep_num:02d}.nfo"
            nfo_content = generate_episode_nfo(
                title=ep_title,
                season=local_season_number,
                episode=ep_num,
                plot=ep_plot,
                runtime=ep_runtime,
                aired=ep_aired,
                tmdb_id=tmdb_id,
                thumb=thumb_path,
            )

            # 找到对应的 strm 目录
            nfo_path = write_nfo(strm_dir, nfo_filename, nfo_content)

            results.append({
                "episode": f"S{local_season_number:02d}E{ep_num:02d}",
                "tmdb_episode": f"S{remote_season_number:02d}E{tmdb_episode_number:02d}",
                "status": "success",
                "nfo_path": nfo_path,
                "thumb_path": thumb_path,
                "title": ep_title,
            })

        except TaskCancelledError:
            raise
        except Exception as e:
            # 单集失败不影响整体
            logger.warning(f"Episode S{remote_season_number:02d}E{tmdb_episode_number:02d} NFO 生成失败: {e}")
            _emit_log(
                log_callback,
                f"分集刮削失败：S{local_season_number:02d}E{ep_num:02d}，{e}",
                "warn",
            )
            results.append({
                "episode": f"S{local_season_number:02d}E{ep_num:02d}",
                "tmdb_episode": f"S{remote_season_number:02d}E{tmdb_episode_number:02d}",
                "status": "failed",
                "error": str(e),
            })

    return results


def _useful_local_episode_title(title: str, season_number: int, episode_number: int) -> str:
    """返回可展示的本地集标题，过滤 SxxEyy 等结构占位符。"""
    from app.recognition.episode_title import is_release_metadata_title

    cleaned = " ".join((title or "").split()).strip(" ._-")
    if not cleaned:
        return ""
    generic_patterns = (
        rf"S0?{season_number}\s*E0?{episode_number}(?:\s*重复版本)?",
        rf"E0?{episode_number}(?:\s*重复版本)?",
        rf"EP(?:ISODE)?\s*0?{episode_number}(?:\s*重复版本)?",
        rf"第\s*0?{episode_number}\s*[集话話]",
    )
    if any(re.fullmatch(pattern, cleaned, flags=re.IGNORECASE) for pattern in generic_patterns):
        return ""
    if is_release_metadata_title(cleaned):
        return ""
    return cleaned


def _extract_cast(detail: dict, client, limit: int = 14) -> list[dict]:
    """Extract a compact cast list from the TMDB credits payload."""
    cast = []
    credits = detail.get("credits") or {}
    for person in (credits.get("cast") or [])[:limit]:
        name = str(person.get("name") or "").strip()
        if not name:
            continue
        profile_path = str(person.get("profile_path") or "").strip()
        if profile_path:
            profile_path = _tmdb_image_url(client, profile_path, "w185")
        cast.append({
            "name": name,
            "role": str(person.get("character") or "").strip(),
            "profile_path": profile_path,
        })
    return cast


def _tmdb_non_special_season_counts(series_detail: dict) -> list[tuple[int, int]]:
    seasons = sorted(
        (
            (int(season.get("season_number") or 0), int(season.get("episode_count") or 0))
            for season in series_detail.get("seasons") or []
            if int(season.get("season_number") or 0) > 0
            and int(season.get("episode_count") or 0) > 0
        ),
        key=lambda item: item[0],
    )
    if [season for season, _ in seasons] != list(range(1, len(seasons) + 1)):
        return []
    return seasons


def _uses_flattened_local_absolute_numbering(
    target: ScrapeTarget,
    tmdb_season_number: int,
    series_detail: dict,
    episode_items: list[tuple[ImportPlanItem, int]],
) -> bool:
    """判断本地 Season 1 是否实际使用跨季度连续集数。"""
    if (
        target.group_type != "season"
        or (target.local_season_number or 1) != 1
        or tmdb_season_number != 1
    ):
        return False
    seasons = _tmdb_non_special_season_counts(series_detail)
    if len(seasons) < 2:
        return False
    local_numbers = [int(number) for _, number in episode_items if number is not None]
    if not local_numbers:
        return False
    first_season_count = seasons[0][1]
    total_count = sum(count for _, count in seasons)
    return max(local_numbers) > first_season_count and max(local_numbers) <= total_count


def _tmdb_season_episode_from_absolute(
    absolute_episode_number: int,
    series_detail: dict,
) -> Optional[tuple[int, int]]:
    """按 TMDB 各季集数累计边界，把绝对集数换算为 Season/Episode。"""
    cumulative = 0
    for season_number, episode_count in _tmdb_non_special_season_counts(series_detail):
        if absolute_episode_number <= cumulative + episode_count:
            return season_number, absolute_episode_number - cumulative
        cumulative += episode_count
    return None


def _tmdb_absolute_episode_offset(
    target: ScrapeTarget,
    plan,
    tmdb_season_number: int,
    season_episodes: dict,
    episode_items: list[tuple[ImportPlanItem, int]],
) -> int:
    """Return local-season split offset for TMDB shows that use absolute numbering."""
    if target.group_type != "season" or tmdb_season_number != 1:
        return 0
    local_season_number = target.local_season_number or 0
    if local_season_number <= 1:
        return 0

    local_numbers = [ep_num for _, ep_num in episode_items if ep_num is not None]
    if not local_numbers:
        return 0

    season_counts: dict[int, set[int]] = {}
    item_ids = {item.id for item, _ in episode_items}
    for item in getattr(plan, "items", []) or []:
        if item.id in item_ids:
            continue
        if item.group_type != "season" or item.action != "generate_strm":
            continue
        if item.season_number is None or item.season_number >= local_season_number:
            continue
        if not _same_scrape_series(target, item):
            continue
        ep_num = _local_episode_number(item)
        if ep_num is None:
            continue
        season_counts.setdefault(int(item.season_number), set()).add(int(ep_num))

    prior_counts = {
        season: values for season, values in season_counts.items()
        if season < local_season_number
    }
    # 缺集媒体库不能用“当前保存文件数量”推导绝对集数偏移。只有每个前序季
    # 都从 1 连续到末集时，才保留旧版自动映射能力；否则等待明确人工映射。
    for values in prior_counts.values():
        if not values or values != set(range(1, max(values) + 1)):
            return 0
    offset = sum(max(values) for values in prior_counts.values())
    if offset <= 0:
        return 0

    # 如果本地本来就是 26、27、28 这样连续编号，就不要再叠加偏移。
    if min(local_numbers) > offset:
        return 0

    mapped_numbers = [ep_num + offset for ep_num in local_numbers]
    if not any(number in season_episodes for number in mapped_numbers):
        return 0
    return offset


def _same_scrape_series(target: ScrapeTarget, item: ImportPlanItem) -> bool:
    target_series = (target.series_group or target.local_title or "").strip()
    item_series = (item.series_group or item.work_title or "").strip()
    if target_series and item_series:
        return target_series == item_series
    if target.work_id and item.work_id:
        return target.work_id == item.work_id
    return False


def _local_episode_number(item: ImportPlanItem) -> Optional[int]:
    """Return a local episode/special number for NFO generation."""
    if item.episode_number is not None:
        return item.episode_number
    if item.special_number is not None:
        return item.special_number
    for text in (item.relative_path, item.original_title, item.title, item.target_strm_path):
        m = re.search(r"S00E(\d{1,3})", text or "", flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None
