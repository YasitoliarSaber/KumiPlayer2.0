# -*- coding: utf-8 -*-
"""AniList GraphQL client for public anime metadata.

The public metadata endpoint does not require OAuth.  This client is kept
small on purpose: it only searches anime and reads public fields that can help
the existing TMDB-centered scrape flow choose better candidates.
"""

import logging
import re
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import load_config

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://graphql.anilist.co"


class AniListClientError(Exception):
    """AniList client base error."""


class AniListRateLimitError(AniListClientError):
    """AniList returned 429."""


class AniListClient:
    """Tiny synchronous AniList GraphQL client."""

    def __init__(
        self,
        rate_limit: Optional[float] = None,
        timeout: Optional[int] = None,
        _http_client: Optional[Any] = None,
    ):
        config = load_config()
        self._rate_limit = rate_limit if rate_limit is not None else config.anilist_rate_limit
        raw_timeout = timeout if timeout is not None else config.anilist_timeout
        self._timeout = max(3, min(int(raw_timeout or 15), 30))
        self._client = _http_client
        self._owned_client: Optional[httpx.Client] = None
        self._last_request_time = 0.0
        self._response_cache: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], dict] = {}

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._owned_client is None:
            config = load_config()
            proxy = config.proxy_url or None
            if proxy:
                self._owned_client = httpx.Client(timeout=self._timeout, proxy=proxy)
            else:
                self._owned_client = httpx.Client(timeout=self._timeout)
        return self._owned_client

    def close(self) -> None:
        if self._owned_client is not None:
            self._owned_client.close()
            self._owned_client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _rate_limit_wait(self) -> None:
        interval = max(0.0, float(self._rate_limit or 0.0))
        elapsed = time.time() - self._last_request_time
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_time = time.time()

    def _request(self, query: str, variables: Optional[dict] = None) -> dict:
        variables = variables or {}
        cache_key = (
            query,
            tuple(sorted((str(k), str(v)) for k, v in variables.items())),
        )
        if cache_key in self._response_cache:
            return deepcopy(self._response_cache[cache_key])

        self._rate_limit_wait()
        try:
            resp = self._get_client().post(
                _GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={"accept": "application/json", "content-type": "application/json"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as e:
            raise AniListClientError("AniList 请求超时") from e
        except Exception as e:
            raise AniListClientError(f"AniList 请求异常: {e}") from e

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            wait_time = _safe_int(retry_after, 5)
            wait_time = max(1, min(wait_time, 30))
            logger.warning("AniList 429 rate limited, wait %s seconds", wait_time)
            time.sleep(wait_time)
            raise AniListRateLimitError("AniList 速率限制")

        if resp.status_code >= 500:
            raise AniListClientError(f"AniList 服务端错误 ({resp.status_code})")

        if resp.status_code != 200:
            raise AniListClientError(f"AniList 请求失败 ({resp.status_code}): {resp.text[:200]}")

        data = resp.json()
        if data.get("errors"):
            message = data["errors"][0].get("message", "GraphQL 错误")
            raise AniListClientError(f"AniList GraphQL 错误: {message}")

        self._response_cache[cache_key] = deepcopy(data)
        return data

    def search_anime(self, query: str, year: Optional[int] = None, per_page: int = 10) -> List[dict]:
        """Search public anime metadata."""
        if not query:
            return []
        gql = """
        query ($search: String, $seasonYear: Int, $perPage: Int) {
          Page(page: 1, perPage: $perPage) {
            media(type: ANIME, search: $search, seasonYear: $seasonYear, sort: SEARCH_MATCH) {
              id
              idMal
              title {
                romaji
                english
                native
                userPreferred
              }
              synonyms
              format
              status
              seasonYear
              startDate { year month day }
              episodes
              duration
              averageScore
              popularity
              description(asHtml: false)
              coverImage { large extraLarge color }
              bannerImage
              genres
              externalLinks {
                site
                url
              }
            }
          }
        }
        """
        variables = {"search": query, "seasonYear": year, "perPage": max(1, min(per_page, 20))}
        data = self._request(gql, variables)
        results = data.get("data", {}).get("Page", {}).get("media", []) or []

        # If a year-constrained query returns nothing, retry without year.  This
        # mirrors the existing TMDB search behavior and helps sequel seasons.
        if not results and year:
            variables["seasonYear"] = None
            data = self._request(gql, variables)
            results = data.get("data", {}).get("Page", {}).get("media", []) or []
        return results


def extract_tmdb_link(media: dict) -> Tuple[Optional[int], str]:
    """Return (tmdb_id, tmdb_type) from AniList external links when present."""
    for link in media.get("externalLinks") or []:
        site = (link.get("site") or "").lower()
        url = link.get("url") or ""
        if "movie database" not in site and "tmdb" not in site and "themoviedb.org" not in url:
            continue
        match = re.search(r"themoviedb\.org/(tv|movie)/(\d+)", url, flags=re.IGNORECASE)
        if match:
            return int(match.group(2)), match.group(1).lower()
        match = re.search(r"(?:tmdb|themoviedb)[^0-9]*(\d+)", url, flags=re.IGNORECASE)
        if match:
            return int(match.group(1)), ""
    return None, ""


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
