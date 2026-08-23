"""V4 Work 级元数据提供器；只做在线映射，不修改本地媒体身份。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.core.config import load_config
from app.scrape.tmdb_client import TMDBClient


def _names(items: list[dict[str, Any]] | None) -> list[str]:
    return [str(item.get("name") or "").strip() for item in (items or []) if item.get("name")]


def _local_metadata(target: dict) -> dict:
    return {
        "provider": "local",
        "provider_id": str(target.get("work_id") or "local"),
        "title": str(target.get("preferred_title") or ""),
        "original_title": str(target.get("original_title") or ""),
        "year": target.get("year"),
        "metadata_state": "local_only",
    }


def _normalize_title(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)


def _select_search_result(results: list[dict], target: dict, media_type: str) -> dict | None:
    target_title = _normalize_title(target.get("preferred_title"))
    exact = [
        item
        for item in results
        if target_title
        and target_title
        in {
            _normalize_title(item.get("name") or item.get("title")),
            _normalize_title(item.get("original_name") or item.get("original_title")),
        }
    ]
    target_year = int(target["year"]) if target.get("year") else None
    if target_year is not None:
        date_key = "first_air_date" if media_type == "tv" else "release_date"
        exact = [
            item
            for item in exact
            if str(item.get(date_key) or "")[:4] == str(target_year)
        ]
    unique_ids = {str(item.get("id") or "") for item in exact if item.get("id")}
    return exact[0] if len(unique_ids) == 1 else None


def default_metadata_provider(target: dict) -> dict:
    """优先使用已确认 TMDB 绑定；未配置 Token 时保留可用的本地元数据。"""

    config = load_config()
    if not config.tmdb_bearer_token:
        return _local_metadata(target)

    bindings = target.get("provider_bindings") or []
    tmdb_binding = next((item for item in bindings if item.get("provider") == "tmdb"), None)
    media_type = str((tmdb_binding or {}).get("media_type") or target.get("work_type") or "tv")
    media_type = "tv" if media_type in {"tv", "series"} else "movie"

    with TMDBClient(bearer_token=config.tmdb_bearer_token) as client:
        if tmdb_binding:
            provider_id = int(tmdb_binding["provider_id"])
        else:
            title = str(target.get("preferred_title") or "").strip()
            results = (
                client.search_tv(title, target.get("year"))
                if media_type == "tv"
                else client.search_movie(title, target.get("year"))
            )
            selected = _select_search_result(results, target, media_type)
            if selected is None:
                return _local_metadata(target)
            provider_id = int(selected["id"])
        detail = client.get_tv_detail(provider_id) if media_type == "tv" else client.get_movie_detail(provider_id)
        images = detail.get("images") or {}
        poster = client.select_best_poster(images) or detail.get("poster_path") or ""
        fanart = client.select_best_backdrop(images) or detail.get("backdrop_path") or ""
        date = detail.get("first_air_date") if media_type == "tv" else detail.get("release_date")
        runtimes = detail.get("episode_run_time") or [detail.get("runtime") or 0]
        return {
            "provider": "tmdb",
            "provider_id": str(provider_id),
            "media_type": media_type,
            "title": detail.get("name") or detail.get("title") or target.get("preferred_title") or "",
            "original_title": detail.get("original_name") or detail.get("original_title") or "",
            "year": int(str(date)[:4]) if date and str(date)[:4].isdigit() else target.get("year"),
            "plot": detail.get("overview") or "",
            "rating": float(detail.get("vote_average") or 0),
            "genres": _names(detail.get("genres")),
            "studios": _names(detail.get("networks") or detail.get("production_companies")),
            "premiered": date or "",
            "runtime": int(runtimes[0] or 0),
            "poster_url": client.build_image_url(poster, "w780") if poster else "",
            "fanart_url": client.build_image_url(fanart, "original") if fanart else "",
            "poster_file_path": poster,
            "fanart_file_path": fanart,
            "metadata_state": "ready",
        }
