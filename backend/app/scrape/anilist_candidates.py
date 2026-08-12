# -*- coding: utf-8 -*-
"""AniList candidate resolution helpers.

AniList is used as an anime-specific search accelerator.  Execution still
resolves back to TMDB so NFO fields and artwork filenames keep the TMDB/Kodi
convention used by the rest of the scraper.
"""

import hashlib
import logging
import re
from dataclasses import replace
from typing import Callable, List, Optional

from app.scrape.anilist_client import AniListClient, extract_tmdb_link
from app.scrape.models import ScrapeCandidate, ScrapeTarget
from app.scrape.tmdb_client import TMDBClient

logger = logging.getLogger(__name__)

TmdbCandidateFactory = Callable[[dict, ScrapeTarget, str], ScrapeCandidate]


def search_anilist_candidates(
    target: ScrapeTarget,
    query: Optional[str],
    year: Optional[int],
    tmdb_client: TMDBClient,
    anilist_client: AniListClient,
    *,
    search_queries: List[str],
    tmdb_result_to_candidate: TmdbCandidateFactory,
) -> List[ScrapeCandidate]:
    """Search AniList and resolve results into executable TMDB candidates."""
    search_year = year or target.scrape_year
    candidates: List[ScrapeCandidate] = []
    seen: set[tuple[int, str]] = set()
    fallback_resolutions = 0

    for title_variant in search_queries:
        score_target = target if title_variant == (target.scrape_title or "") else replace(target, scrape_title=title_variant)
        results = anilist_client.search_anime(title_variant, search_year, per_page=8)
        for media in results[:8]:
            direct_tmdb_id, _ = extract_tmdb_link(media)
            if not direct_tmdb_id:
                if fallback_resolutions >= 2:
                    continue
                fallback_resolutions += 1
            candidate = anilist_media_to_candidate(
                media,
                score_target,
                target.scrape_type,
                tmdb_client,
                tmdb_result_to_candidate=tmdb_result_to_candidate,
            )
            if not candidate or candidate.tmdb_id <= 0:
                continue
            key = (candidate.tmdb_id, candidate.tmdb_type)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

        # AniList 的 search 已经会匹配别名；拿到可执行的高置信度 TMDB
        # 直连候选后继续遍历所有本地标题变体只会重复请求两边服务。
        if candidates and max(candidate.score for candidate in candidates) >= 70:
            break

    candidates.sort(key=lambda c: (c.score, c.popularity), reverse=True)
    return candidates


def anilist_media_to_candidate(
    media: dict,
    target: ScrapeTarget,
    target_type: str,
    tmdb_client: TMDBClient,
    *,
    tmdb_result_to_candidate: TmdbCandidateFactory,
) -> Optional[ScrapeCandidate]:
    """Convert one AniList media result to a TMDB-backed scrape candidate."""
    tmdb_id, tmdb_type = extract_tmdb_link(media)
    has_direct_tmdb_link = bool(tmdb_id)
    anilist_type = anilist_format_to_tmdb_type(media.get("format"))
    if not tmdb_type:
        tmdb_type = anilist_type or target_type
    if tmdb_type != target_type:
        return None

    if not tmdb_id:
        tmdb_id = resolve_tmdb_id_from_anilist(
            media,
            target,
            tmdb_type,
            tmdb_client,
            tmdb_result_to_candidate=tmdb_result_to_candidate,
        )
        if not tmdb_id:
            return None

    result = anilist_media_as_tmdb_result(media, tmdb_id, tmdb_type)
    candidate = tmdb_result_to_candidate(result, target, tmdb_type)
    candidate.provider = "anilist"
    candidate.candidate_id = hashlib.md5(
        f"{target.scrape_target_id}:anilist:{media.get('id')}:{tmdb_id}:{tmdb_type}".encode()
    ).hexdigest()[:12]
    candidate.raw = {
        "anilist": media,
        "tmdb_id": tmdb_id,
        "tmdb_type": tmdb_type,
        "provider_title_aliases": anilist_title_variants(media),
        "provider_tmdb_link": "direct" if has_direct_tmdb_link else "title_resolution",
        "canonical_assets": anilist_canonical_assets(media),
    }
    candidate.score += anilist_bonus(media, target, tmdb_type)
    candidate.score = round(candidate.score, 1)
    candidate.reasons.insert(0, f"AniList 命中({media.get('id')})")
    return candidate


def resolve_tmdb_id_from_anilist(
    media: dict,
    target: ScrapeTarget,
    tmdb_type: str,
    tmdb_client: TMDBClient,
    *,
    tmdb_result_to_candidate: TmdbCandidateFactory,
) -> Optional[int]:
    """Resolve AniList media back to TMDB using AniList title variants."""
    year = anilist_year(media) or target.scrape_year or target.local_year
    for title in anilist_title_variants(media)[:2]:
        try:
            results = (
                tmdb_client.search_movie(title, year)
                if tmdb_type == "movie"
                else tmdb_client.search_tv(title, year)
            )
            if not results and year:
                results = (
                    tmdb_client.search_movie(title, None)
                    if tmdb_type == "movie"
                    else tmdb_client.search_tv(title, None)
                )
        except Exception:
            logger.debug("resolve AniList -> TMDB failed", exc_info=True)
            continue

        scored: List[ScrapeCandidate] = []
        score_target = replace(target, scrape_title=title)
        for result in results[:5]:
            scored.append(tmdb_result_to_candidate(result, score_target, tmdb_type))
        scored.sort(key=lambda c: (c.score, c.popularity), reverse=True)
        if scored and scored[0].score >= 30:
            return scored[0].tmdb_id
    return None


def anilist_media_as_tmdb_result(media: dict, tmdb_id: int, tmdb_type: str) -> dict:
    """Shape AniList public fields like a TMDB search result for scoring only."""
    titles = media.get("title") or {}
    year = anilist_year(media)
    title = titles.get("userPreferred") or titles.get("native") or titles.get("english") or titles.get("romaji") or ""
    original = titles.get("native") or titles.get("romaji") or titles.get("english") or title
    result = {
        "id": tmdb_id,
        "name": title if tmdb_type == "tv" else None,
        "title": title if tmdb_type == "movie" else None,
        "original_name": original if tmdb_type == "tv" else None,
        "original_title": original if tmdb_type == "movie" else None,
        "first_air_date": f"{year}-01-01" if year and tmdb_type == "tv" else "",
        "release_date": f"{year}-01-01" if year and tmdb_type == "movie" else "",
        "overview": strip_html(media.get("description") or ""),
        "poster_path": (media.get("coverImage") or {}).get("extraLarge")
        or (media.get("coverImage") or {}).get("large")
        or "",
        "popularity": media.get("popularity") or 0,
        "vote_average": anilist_average_score(media),
        "media_type": tmdb_type,
    }
    return {k: v for k, v in result.items() if v is not None}


def anilist_canonical_assets(media: dict) -> dict[str, str]:
    """Map AniList image URLs to TMDB/Kodi-compatible local asset names."""
    cover = (media.get("coverImage") or {}).get("extraLarge") or (media.get("coverImage") or {}).get("large")
    banner = media.get("bannerImage")
    assets: dict[str, str] = {}
    if cover:
        assets["poster.jpg"] = cover
    if banner:
        assets["fanart.jpg"] = banner
    return assets


def anilist_bonus(media: dict, target: ScrapeTarget, tmdb_type: str) -> float:
    score = 8.0
    anilist_format = (media.get("format") or "").upper()
    if tmdb_type == "movie" and anilist_format == "MOVIE":
        score += 15
    elif tmdb_type == "tv" and anilist_format in {"TV", "TV_SHORT", "ONA", "OVA"}:
        score += 8
    if target.scrape_type == "tv" and target.item_ids and media.get("episodes"):
        local_count = len(target.item_ids)
        episode_count = media.get("episodes") or 0
        if abs(local_count - episode_count) <= max(1, int(local_count * 0.15)):
            score += 12
    return score


def anilist_format_to_tmdb_type(value: Optional[str]) -> str:
    if (value or "").upper() == "MOVIE":
        return "movie"
    return "tv"


def anilist_year(media: dict) -> Optional[int]:
    start = media.get("startDate") or {}
    return media.get("seasonYear") or start.get("year")


def anilist_average_score(media: dict) -> float:
    try:
        return round(float(media.get("averageScore") or 0) / 10, 1)
    except (TypeError, ValueError):
        return 0.0


def anilist_title_variants(media: dict) -> List[str]:
    titles = media.get("title") or {}
    values = [
        titles.get("native"),
        titles.get("english"),
        titles.get("romaji"),
        titles.get("userPreferred"),
        *(media.get("synonyms") or []),
    ]
    variants: List[str] = []
    for value in values:
        cleaned = " ".join(str(value or "").split()).strip()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)
    return variants


def should_use_anilist(target: ScrapeTarget) -> bool:
    """Return whether AniList should join candidate search for this target."""
    from app.core.config import load_config

    config = load_config()
    if not getattr(config, "anilist_enabled", True):
        return False
    show_type = (getattr(target, "show_type", "") or "").lower()
    if show_type.startswith("anime"):
        return True
    text = " ".join([
        target.series_group or "",
        target.local_title or "",
        target.original_title or "",
        target.source_subwork_dir or "",
        target.target_dir or "",
    ])
    return any(keyword in text for keyword in ("动画", "番剧", "anime", "Anime"))


def strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()
