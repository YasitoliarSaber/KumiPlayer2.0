"""V4 Work 级元数据提供器；只做在线映射，不修改本地媒体身份。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.core.config import load_config
from app.media_v4.resolution.ranker import CandidateRanker
from app.scrape.tmdb_client import TMDBClient, TMDBClientError

_METADATA_STATES = frozenset({"ready", "waiting_metadata", "waiting_review", "source_unavailable", "failed"})


def _names(items: list[dict[str, Any]] | None) -> list[str]:
    return [str(item.get("name") or "").strip() for item in (items or []) if item.get("name")]


def _local_state(
    state: str,
    reason: str,
    *,
    attempted_queries: list | None = None,
    candidates: list | None = None,
    reason_code: str | None = None,
    candidate_decision: dict | None = None,
) -> dict:
    """等待/失败类结果：不再把本地 Work ID 伪装成外部 provider identity。"""

    result: dict = {
        "provider": "local",
        "provider_id": "",
        "metadata_state": state,
        "reason": reason,
        "attempted_queries": attempted_queries or [],
    }
    if candidates:
        result["candidates"] = candidates
    if reason_code:
        result["reason_code"] = reason_code
    if candidate_decision is not None:
        result["candidate_decision"] = candidate_decision
    return result


def _normalize_title(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)


def _candidate_summary(item: dict) -> dict:
    return {
        "provider": "tmdb",
        "provider_id": str(item.get("id") or ""),
        "title": item.get("name") or item.get("title") or "",
        "original_title": item.get("original_name") or item.get("original_title") or "",
        "year": int(str(item.get("first_air_date") or item.get("release_date") or "")[:4] or 0) or None,
        "media_type": "tv" if "first_air_date" in item or "name" in item else "movie",
        "popularity": _safe_float(item.get("popularity")),
    }


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _merge_search_result(results_by_id: dict[str, dict], item: dict) -> None:
    """按 Provider ID 合并多次标题查询的摘要，保留最完整的字段。"""

    provider_id = str(item.get("id") or "").strip()
    if not provider_id:
        return
    existing = results_by_id.get(provider_id)
    if existing is None:
        results_by_id[provider_id] = dict(item)
        return

    merged = dict(existing)
    for key, value in item.items():
        if key == "popularity":
            merged[key] = max(_safe_float(merged.get(key)), _safe_float(value))
        elif not merged.get(key) and value:
            merged[key] = value
    results_by_id[provider_id] = merged


def _ranked_candidate_payload(ranked: list) -> list[dict]:
    return [
        {
            "provider": item.provider,
            "provider_id": item.provider_id,
            "media_type": item.media_type,
            "title": item.title,
            "original_title": item.original_title,
            "aliases": list(item.aliases),
            "year": item.year,
            "popularity": item.popularity,
            "score": item.score,
            "reasons": list(item.reasons),
            "recommended": item.recommended,
            "identity_safe": item.identity_safe,
            "blocked": item.blocked,
        }
        for item in ranked
    ]


def _candidate_decision_payload(ranked: list, adopted, reason: str) -> dict:
    return {
        "decision": "auto_adopted" if adopted is not None else "waiting_review",
        "reason": reason,
        "selected_provider": adopted.provider if adopted is not None else "",
        "selected_provider_id": adopted.provider_id if adopted is not None else "",
        "selected_score": adopted.score if adopted is not None else None,
        "ranked_candidates": _ranked_candidate_payload(ranked),
    }


def _search_with_year_fallback(
    client: TMDBClient,
    query: str,
    media_type: str,
    year: int | None,
) -> list[dict]:
    """先按年份检索，空结果时回退到纯标题检索。"""

    search = client.search_tv if media_type == "tv" else client.search_movie
    results = search(query, year)
    if not results and year is not None:
        results = search(query, None)
    return results


def _rank_metadata_candidates(
    target: dict,
    media_type: str,
    titles: list[str],
    results_by_id: dict[str, dict],
    client: TMDBClient,
) -> tuple[list, object | None, str]:
    """把多次搜索结果统一交给 CandidateRanker 决策。"""

    candidates = enrich_candidate_aliases(
        [_candidate_summary(item) for item in results_by_id.values()],
        titles,
        max_details=8,
        client=client,
    )
    ranker = CandidateRanker()
    ranked = ranker.rank(
        {
            "preferred_title": target.get("preferred_title") or "",
            "queries": titles,
            "media_type": media_type,
            "year": target.get("year"),
        },
        candidates,
    )
    adopted, reason = ranker.auto_adopt(ranked)
    return ranked, adopted, reason


def _target_titles(target: dict) -> list[str]:
    """返回已经由本地解析确认、可用于精确匹配的标题事实。

    这里不能从目录名重新猜测，也不接纳父系列名；只使用 Work 已持久化的
    首选标题和原文标题。这样本地化标题没有被 TMDB 搜到时，仍可用同一 Work
    的原文标题安全回退，而不会把外传吸收到父系列。
    """

    titles: list[str] = []
    seen: set[str] = set()
    for raw in (target.get("preferred_title"), target.get("original_title")):
        value = str(raw or "").strip()
        normalized = _normalize_title(value)
        if value and normalized and normalized not in seen:
            titles.append(value)
            seen.add(normalized)
    return titles


def _select_search_result(results: list[dict], target: dict, media_type: str) -> dict | None:
    target_titles = {_normalize_title(value) for value in _target_titles(target)}
    target_titles.discard("")
    exact = [
        item
        for item in results
        if target_titles
        and target_titles & {
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


def _select_enriched_candidate(candidates: list[dict], target: dict, media_type: str) -> dict | None:
    """从补全别名后的候选中只接受唯一等值匹配。

    首轮搜索摘要通常只带主标题和原文标题。对于目录名使用了可信中文、日文或
    发行别名的作品，只有补充详情中的 alternative_titles / translations 后才能
    安全判定。这里仍坚持“唯一 + 等值 + 年份不冲突”，绝不把近似标题静默绑定。
    """

    target_titles = {_normalize_title(value) for value in _target_titles(target)}
    target_titles.discard("")
    if not target_titles:
        return None
    target_year = int(target["year"]) if target.get("year") else None
    matched: list[dict] = []
    for item in candidates:
        names = [
            item.get("title"),
            item.get("original_title"),
            *(item.get("aliases") or []),
        ]
        if not target_titles & {_normalize_title(name) for name in names}:
            continue
        item_year = item.get("year")
        if target_year is not None and item_year is not None and int(item_year) != target_year:
            continue
        if str(item.get("media_type") or media_type) != media_type:
            continue
        if str(item.get("provider_id") or ""):
            matched.append(item)
    unique_ids = {str(item["provider_id"]) for item in matched}
    return matched[0] if len(unique_ids) == 1 else None


def search_tmdb_candidates(query: str, media_type: str, year: int | None = None) -> list[dict] | None:
    """手动元数据恢复：按标题搜索 TMDB 候选；未配置 Token 时返回 None。"""

    config = load_config()
    if not config.tmdb_bearer_token:
        return None
    media_type = "tv" if media_type in {"tv", "series"} else "movie"
    with TMDBClient(bearer_token=config.tmdb_bearer_token) as client:
        # 本地年份可能来自压制目录、季度目录或发行年份，并不一定等于
        # Provider 的首播/上映年份。年份过滤无结果时必须回退到纯标题搜索，
        # 这是旧版候选链的关键兜底，否则正确作品会被误报为“搜索不到”。
        results = _search_with_year_fallback(client, query, media_type, year)
    return [_candidate_summary(item) for item in results[:8]]


def _extract_aliases(detail: dict, media_type: str) -> list[str]:
    """从详情 alternative_titles / translations 提取可信本地化标题别名。"""

    aliases: list[str] = []
    alternative = (detail.get("alternative_titles") or {}).get("results") or []
    for item in alternative:
        title = str(item.get("title") or "").strip()
        if title:
            aliases.append(title)
    translations = (detail.get("translations") or {}).get("translations") or []
    for item in translations:
        data = item.get("data") or {}
        for raw_title in (data.get("title"), data.get("name")):
            value = str(raw_title or "").strip()
            if value:
                aliases.append(value)
    seen: list[str] = []
    for value in aliases:
        if value not in seen:
            seen.append(value)
    return seen[:12]


def enrich_candidate_aliases(
    results: list[dict],
    local_queries: list[str],
    *,
    max_details: int = 8,
    detail_cache: dict | None = None,
    detail_budget: list[int] | None = None,
    client: TMDBClient | None = None,
) -> list[dict]:
    """为候选补全可信别名：primary/original 已精确匹配的候选不请求详情；
    其余在预算内调用详情接口，失败/超预算保持无别名（不猜测 high）。"""

    cache = detail_cache if detail_cache is not None else {}
    budget = detail_budget if detail_budget is not None else [0]
    local_norms = {_normalize_title(q) for q in local_queries}
    local_norms.discard("")
    config = load_config() if client is None else None
    enriched: list[dict] = []
    for item in results:
        primary = _normalize_title(item.get("title") or item.get("name"))
        original = _normalize_title(item.get("original_title") or item.get("original_name"))
        if primary in local_norms or original in local_norms:
            enriched.append({**item, "aliases": []})
            continue
        media_type = str(item.get("media_type") or "tv")
        media_type = "tv" if media_type in {"tv", "series"} else "movie"
        provider_id = str(item.get("provider_id") or "")
        cache_key = (media_type, provider_id)
        # None 也是一次已完成请求的缓存结果：详情失败不能让同一 Provider
        # 身份在同一 draft 的其他 Work 上反复请求。
        if cache_key not in cache and budget[0] < max_details:
            budget[0] += 1
            try:
                if client is not None:
                    detail = (
                        client.get_tv_detail(int(provider_id))
                        if media_type == "tv"
                        else client.get_movie_detail(int(provider_id))
                    )
                else:
                    with TMDBClient(bearer_token=config.tmdb_bearer_token) as detail_client:
                        detail = (
                            detail_client.get_tv_detail(int(provider_id))
                            if media_type == "tv"
                            else detail_client.get_movie_detail(int(provider_id))
                        )
            except Exception:
                detail = None
            cache[cache_key] = detail
        detail = cache.get(cache_key)
        aliases = _extract_aliases(detail, media_type) if detail else []
        enriched.append({**item, "aliases": aliases})
    return enriched


def default_metadata_provider(target: dict) -> dict:
    """返回带真实状态的元数据结果。

    状态语义：
    - ready              在线元数据完整可用（外部 provider binding）；
    - waiting_metadata   未配置 Token 等提供方缺配；
    - waiting_review     搜索无唯一结果/候选歧义，需人工确认；
    - source_unavailable 提供方网络/服务不可用；
    - failed             其他确定性失败。
    本地 Work ID 不再被包装成外部 provider identity。
    """

    config = load_config()
    if not config.tmdb_bearer_token:
        return _local_state(
            "waiting_metadata",
            "未配置 TMDB API Token，无法获取在线元数据；请先在设置页配置后重新刮削",
        )

    bindings = target.get("provider_bindings") or []
    tmdb_binding = next((item for item in bindings if item.get("provider") == "tmdb"), None)
    media_type = str((tmdb_binding or {}).get("media_type") or target.get("work_type") or "tv")
    media_type = "tv" if media_type in {"tv", "series"} else "movie"

    try:
        with TMDBClient(bearer_token=config.tmdb_bearer_token) as client:
            if tmdb_binding:
                provider_id = int(tmdb_binding["provider_id"])
                candidate_decision = {
                    "decision": "trusted_binding",
                    "reason": "复用已确认的 TMDB 作品身份",
                    "selected_provider": "tmdb",
                    "selected_provider_id": str(provider_id),
                    "selected_score": None,
                    "ranked_candidates": [],
                }
            else:
                titles = _target_titles(target)
                attempted_queries: list[dict] = []
                results_by_id: dict[str, dict] = {}
                search_errors: list[TMDBClientError] = []
                successful_queries = 0
                for title in titles:
                    query_record = {"query": title, "year": target.get("year")}
                    try:
                        results = _search_with_year_fallback(
                            client,
                            title,
                            media_type,
                            target.get("year"),
                        )
                    except TMDBClientError as exc:
                        search_errors.append(exc)
                        query_record.update({
                            "status": "source_unavailable",
                            "error": type(exc).__name__,
                        })
                        attempted_queries.append(query_record)
                        continue
                    successful_queries += 1
                    query_record.update({"status": "completed", "result_count": len(results)})
                    attempted_queries.append(query_record)
                    for item in results:
                        _merge_search_result(results_by_id, item)

                if search_errors and successful_queries == 0 and titles:
                    return _local_state(
                        "source_unavailable",
                        f"在线资料服务暂不可用，请稍后重试（{type(search_errors[-1]).__name__}）",
                        attempted_queries=attempted_queries,
                        reason_code="source_unavailable",
                    )

                # 主标题、原文标题及别名查询的所有摘要统一进入评分器。即使
                # 首个查询已经出现完整标题，也不能提前采用，否则会丢失后续
                # 查询的更可信候选及热度排序信号。
                ranked, adopted, adopt_reason = _rank_metadata_candidates(
                    target,
                    media_type,
                    titles,
                    results_by_id,
                    client,
                )
                candidate_decision = _candidate_decision_payload(ranked, adopted, adopt_reason)
                if adopted is None:
                    ranked_payload = _ranked_candidate_payload(ranked)
                    review_reason = (
                        "在线资料中没有找到候选作品，请检查作品标题后重试"
                        if not ranked
                        else "在线作品候选不足以自动确认，需要人工确认后再继续"
                    )
                    return _local_state(
                        "waiting_review",
                        review_reason,
                        attempted_queries=attempted_queries,
                        candidates=ranked_payload,
                        reason_code="no_candidates" if not ranked else "ambiguous_candidates",
                        candidate_decision=candidate_decision,
                    )
                provider_id = int(adopted.provider_id)
            detail = client.get_tv_detail(provider_id) if media_type == "tv" else client.get_movie_detail(provider_id)
            images = detail.get("images") or {}
            poster = client.select_best_poster(images) or detail.get("poster_path") or ""
            fanart = client.select_best_backdrop(images) or detail.get("backdrop_path") or ""
            clearlogo = client.select_best_logo(images) or ""
            date = detail.get("first_air_date") if media_type == "tv" else detail.get("release_date")
            runtimes = detail.get("episode_run_time") or [detail.get("runtime") or 0]
            result = {
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
                "clearlogo_url": client.build_image_url(clearlogo, "original") if clearlogo else "",
                "clearlogo_file_path": clearlogo,
                "metadata_state": "ready",
                "candidate_decision": candidate_decision,
            }
            if media_type == "tv":
                result["episode_mappings"] = _build_tv_episode_mappings(client, provider_id, target)
            return result
    except TMDBClientError as exc:
        return _local_state(
            "source_unavailable",
            f"在线资料服务暂不可用，请稍后重试（{type(exc).__name__}）",
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _local_state("failed", f"获取在线元数据失败: {type(exc).__name__}")


def _build_tv_episode_mappings(client: TMDBClient, provider_id: int, target: dict) -> list[dict]:
    """把已确认的本地 Episode 映射到 TMDB 的标题与剧照。

    本地季度和集号永远不在这里改写；这里只保存 provider 的映射和可展示
    的远端字段。特别篇没有远端映射时可保留本地标题；但常规季请求失败时
    不能伪装成完整刮削成功，否则详情页会永久缺失标题与剧照。
    """

    local_episodes = [
        item for item in target.get("episodes") or []
        if str(item.get("episode_id") or "") and str(item.get("episode_kind") or "regular") != "auxiliary"
    ]
    by_provider_season: dict[int, list[dict]] = {}
    for episode in local_episodes:
        provider_season = _positive_int(episode.get("provider_season_number"))
        if provider_season is None:
            provider_season = _positive_int(episode.get("local_season_number"))
        if provider_season is None:
            continue
        by_provider_season.setdefault(provider_season, []).append(episode)

    result: list[dict] = []
    for provider_season, episodes in sorted(by_provider_season.items()):
        try:
            season = client.get_tv_season_episodes(provider_id, provider_season)
            remote_by_number = {
                int(item["episode_number"]): item
                for item in season.get("episodes") or []
                if _positive_int(item.get("episode_number")) is not None
            }
        except TMDBClientError:
            if any(str(item.get("season_kind") or "") != "special" for item in episodes):
                raise
            remote_by_number = {}

        for episode in episodes:
            provider_episode = _positive_int(episode.get("provider_episode_number"))
            if provider_episode is None:
                if str(episode.get("season_kind") or "") == "special":
                    provider_episode = _positive_int(episode.get("special_number"))
                else:
                    provider_episode = _positive_int(episode.get("local_episode_number"))
            if provider_episode is None:
                continue
            remote = remote_by_number.get(provider_episode) or {}
            still = str(remote.get("still_path") or "")
            mapping = {
                "episode_id": str(episode["episode_id"]),
                "provider_season_number": provider_season,
                "provider_episode_number": provider_episode,
                "provider_episode_id": str(remote.get("id") or ""),
                "title": str(remote.get("name") or ""),
                "plot": str(remote.get("overview") or ""),
                "runtime": _positive_int(remote.get("runtime")),
                # 详情页横向卡片至少需要 w500；w300 在高 DPI 下既模糊又会被前端再次改写。
                "still_url": client.build_image_url(still, "w500") if still else "",
            }
            result.append(mapping)
    return result


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
