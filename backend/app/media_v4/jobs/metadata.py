"""V4 Work 级元数据提供器；只做在线映射，不修改本地媒体身份。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from app.core.config import load_config
from app.media_v4.resolution.ranker import CandidateRanker, RankedCandidate
from app.scrape.tmdb_client import (
    TMDBAuthError,
    TMDBClient,
    TMDBClientError,
    TMDBRateLimitError,
)

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
    identity_status: str = "unresolved",
    work_metadata_status: str = "unavailable",
    episode_mapping_status: str = "not_applicable",
    failure_stage: str = "",
    retryable: bool | None = None,
) -> dict:
    """等待/失败类结果：不再把本地 Work ID 伪装成外部 provider identity。"""

    result: dict = {
        "provider": "local",
        "provider_id": "",
        "metadata_state": state,
        "reason": reason,
        "attempted_queries": attempted_queries or [],
        "identity_status": identity_status,
        "work_metadata_status": work_metadata_status,
        "episode_mapping_status": episode_mapping_status,
        "retryable": bool(
            state in {"waiting_metadata", "source_unavailable"}
            if retryable is None
            else retryable
        ),
    }
    if failure_stage:
        result["failure_stage"] = failure_stage
    if candidates:
        result["candidates"] = candidates
    if reason_code:
        result["reason_code"] = reason_code
    if candidate_decision is not None:
        result["candidate_decision"] = candidate_decision
    return result


def _tmdb_reason_code(error: TMDBClientError) -> str:
    """把提供方异常归一化为可恢复的业务原因码。"""

    explicit = str(getattr(error, "reason_code", "") or "").strip()
    if explicit:
        return explicit
    if isinstance(error, TMDBAuthError):
        return "provider_auth_required"
    if isinstance(error, TMDBRateLimitError):
        return "provider_rate_limited"
    status_code = int(getattr(error, "status_code", 0) or 0)
    if status_code == 404:
        return "provider_resource_missing"
    if status_code == 422:
        return "invalid_response"
    return "source_unavailable"


def _tmdb_failure_stage(error: TMDBClientError, fallback: str) -> str:
    stage = str(getattr(error, "failure_stage", "") or "").strip()
    return stage or fallback


def _tmdb_retryable(error: TMDBClientError, default: bool = True) -> bool:
    value = getattr(error, "retryable", None)
    if value is not None:
        return bool(value)
    if isinstance(error, TMDBAuthError):
        return False
    status_code = int(getattr(error, "status_code", 0) or 0)
    if status_code in {404, 422}:
        return False
    return default


def _normalize_title(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)


def _title_tokens(value: object) -> set[str]:
    """提取用于特别篇语义比对的最小词集合。

    特别篇名称常见中英文混排；先保留拉丁词，再把 CJK 字符作为单字词，
    既能处理 ``Mystery Camp`` 这类名称，也不会把两个仅共享“Camp”的
    不同标题误认为同一集。
    """

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return {
        token
        for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalized)
        if token
    }


def _special_display_title(episode: dict) -> str:
    """返回特别篇自身语义标题；纯 SP 编号和通用“特别篇”不是证据。"""

    from app.media_v4.parsing.episode_titles import is_generic_special_title, is_special_marker_only

    for raw in (episode.get("display_title"), episode.get("episode_title"), episode.get("title")):
        value = str(raw or "").strip()
        if value and not is_generic_special_title(value) and not is_special_marker_only(value):
            return value
    return ""


def _special_title_score(local_title: str, remote: dict) -> int:
    """计算本地特别篇标题与在线条目的保守语义分数。"""

    local_normalized = _normalize_title(local_title)
    if not local_normalized:
        return 0
    remote_titles = [
        str(remote.get("name") or "").strip(),
        str(remote.get("original_name") or "").strip(),
        str(remote.get("title") or "").strip(),
        str(remote.get("original_title") or "").strip(),
    ]
    best = 0
    local_tokens = _title_tokens(local_title)
    for remote_title in remote_titles:
        remote_normalized = _normalize_title(remote_title)
        if not remote_normalized:
            continue
        if local_normalized == remote_normalized:
            best = max(best, 100)
            continue
        remote_tokens = _title_tokens(remote_title)
        overlap = local_tokens & remote_tokens
        if len(overlap) >= 2:
            # 两个以上完整词重合时允许标题带副标题或标点差异；单词
            # 重合不足的标题必须经过更高的整体相似度门槛。
            ratio = SequenceMatcher(None, local_normalized, remote_normalized).ratio()
            if ratio >= 0.72:
                best = max(best, 82)
        elif len(local_normalized) >= 6 and len(remote_normalized) >= 6:
            ratio = SequenceMatcher(None, local_normalized, remote_normalized).ratio()
            if ratio >= 0.86:
                best = max(best, 72)
    return best


def _match_special_episode(
    episode: dict,
    remote_by_number: dict[int, dict],
    *,
    used_remote_numbers: set[int] | None = None,
) -> tuple[int, dict] | None:
    """按特别篇自身名称匹配在线条目，绝不按本地展示序号盲配。"""

    used = used_remote_numbers if used_remote_numbers is not None else set()
    local_title = _special_display_title(episode)
    if local_title:
        scored = sorted(
            (
                (_special_title_score(local_title, remote), number, remote)
                for number, remote in remote_by_number.items()
                if number not in used
            ),
            key=lambda item: (item[0], -item[1]),
            reverse=True,
        )
        if scored and scored[0][0] >= 82:
            best = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0
            # 相同或近似标题不能静默选第一个，避免再次制造错误绑定。
            exact_matches = [item for item in scored if item[0] == 100]
            if len(exact_matches) == 1 or (
                not exact_matches and best[0] - second_score >= 10
            ):
                return best[1], best[2]
        return None

    # 本地只有 SP01/SP02 或“特别篇”时没有足够语义证据；旧的
    # provider_episode_number 可能正是历史错误的顺序映射，不能把它当成
    # 显式确认再次复用。成功读取 Season 0 后，这类旧映射会由诊断清理。
    return None


def _candidate_summary(item: dict) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "provider": "tmdb",
        "provider_id": str(item.get("id") or ""),
        "title": item.get("name") or item.get("title") or "",
        "original_title": item.get("original_name") or item.get("original_title") or "",
        "year": int(str(item.get("first_air_date") or item.get("release_date") or "")[:4] or 0) or None,
        "media_type": "tv" if "first_air_date" in item or "name" in item else "movie",
        "popularity": _safe_float(item.get("popularity")),
    }
    if item.get("genre_ids") is not None:
        summary["genre_ids"] = list(item.get("genre_ids") or [])
    if item.get("genres") is not None:
        summary["genres"] = list(item.get("genres") or [])
    return summary


def _safe_float(value: object) -> float:
    try:
        return float(str(value or 0.0))
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


def _candidate_decision_payload(
    ranked: list[RankedCandidate],
    adopted: RankedCandidate | None,
    reason: str,
) -> dict:
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
) -> tuple[list[RankedCandidate], RankedCandidate | None, str]:
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
            "show_type": target.get("show_type") or "",
        },
        candidates,
    )
    adopted, reason = ranker.auto_adopt(ranked)
    return ranked, adopted, reason


def _target_titles(target: dict) -> list[str]:
    """返回已经由本地解析确认、可用于精确匹配的标题事实。

    这里不能从目录名重新猜测，也不接纳父系列名；只使用 Work 已持久化的
    首选标题、原文标题与本次 confirmed 成员提供的自身标题。这样本地化标题没有被 TMDB 搜到时，仍可用同一 Work
    的原文标题安全回退，而不会把外传吸收到父系列。
    """

    titles: list[str] = []
    seen: set[str] = set()
    for raw in (
        target.get("preferred_title"), target.get("original_title"),
        *(target.get("identity_titles") or []),
    ):
        value = str(raw or "").strip()
        normalized = _normalize_title(value)
        if value and normalized and normalized not in seen:
            titles.append(value)
            seen.add(normalized)
    return titles


def _has_local_episodes(target: dict) -> bool:
    return any(
        str(item.get("episode_id") or "").strip()
        and str(item.get("episode_kind") or "regular") != "auxiliary"
        for item in target.get("episodes") or []
        if isinstance(item, dict)
    )


def _is_special_episode(episode: dict) -> bool:
    return (
        str(episode.get("season_kind") or "").strip().casefold() == "special"
        or str(episode.get("episode_kind") or "").strip().casefold() == "special"
        or _positive_int(episode.get("local_season_number")) == 0
    )


def _season_result_is_optional_special(item: dict, target: dict) -> bool:
    """判断季度错误是否只影响可选的特别篇。"""

    reason_code = str(item.get("reason_code") or "").strip().casefold()
    # 只有“在线条目缺失”是可选补全；认证、限流、网络或响应异常必须
    # 保持原有的可恢复故障状态，不能因为本地恰好是 Season 0 就被吞掉。
    if reason_code not in {"episode_not_found", "provider_resource_missing"}:
        return False
    if _positive_int(item.get("local_season_number")) == 0:
        return True
    season_id = str(item.get("season_id") or "").strip()
    members = [
        episode for episode in target.get("episodes") or []
        if isinstance(episode, dict)
        and (not season_id or str(episode.get("season_id") or "") == season_id)
        and _positive_int(episode.get("local_season_number")) == _positive_int(item.get("local_season_number"))
    ]
    return bool(members) and all(_is_special_episode(episode) for episode in members)


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
    detail_cache: dict[tuple[str, str], dict[str, Any] | None] | None = None,
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
                    assert config is not None
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
        enriched_item = {**item, "aliases": aliases}
        if detail:
            detail_genres = list(detail.get("genres") or [])
            if detail_genres:
                enriched_item["genres"] = detail_genres
                enriched_item["genre_ids"] = [
                    genre.get("id")
                    for genre in detail_genres
                    if isinstance(genre, dict) and genre.get("id") is not None
                ]
        enriched.append(enriched_item)
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
    bindings = target.get("provider_bindings") or []
    tmdb_binding = next((item for item in bindings if item.get("provider") == "tmdb"), None)
    media_type = str((tmdb_binding or {}).get("media_type") or target.get("work_type") or "tv")
    media_type = "tv" if media_type in {"tv", "series"} else "movie"
    if not config.tmdb_bearer_token:
        saved_provider_id = str((tmdb_binding or {}).get("provider_id") or "").strip()
        if saved_provider_id:
            return {
                "provider": "tmdb",
                "provider_id": saved_provider_id,
                "media_type": media_type,
                "title": str(target.get("preferred_title") or ""),
                "metadata_state": "waiting_metadata",
                "reason": "未配置 TMDB API Token，无法获取在线元数据；请先在设置页配置后重新刮削",
                "reason_code": "provider_auth_required",
                "episode_mappings": [],
                "identity_status": "confirmed",
                "work_metadata_status": "unavailable",
                "episode_mapping_status": "not_applicable",
                "failure_stage": "configuration",
                "retryable": True,
            }
        return _local_state(
            "waiting_metadata",
            "未配置 TMDB API Token，无法获取在线元数据；请先在设置页配置后重新刮削",
            reason_code="provider_auth_required",
            failure_stage="configuration",
            retryable=True,
        )

    provider_id: int | None = None
    candidate_decision: dict[str, Any]

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
                        reason_code=_tmdb_reason_code(search_errors[-1]),
                        failure_stage=_tmdb_failure_stage(search_errors[-1], "search"),
                        retryable=_tmdb_retryable(search_errors[-1]),
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
                        failure_stage="search",
                        retryable=False,
                    )
                provider_id = int(adopted.provider_id)
            detail = client.get_tv_detail(provider_id) if media_type == "tv" else client.get_movie_detail(provider_id)
            images = detail.get("images") or {}
            poster = client.select_best_poster(images) or detail.get("poster_path") or ""
            fanart = client.select_best_backdrop(images) or detail.get("backdrop_path") or ""
            clearlogo = client.select_best_logo(images) or ""
            date = detail.get("first_air_date") if media_type == "tv" else detail.get("release_date")
            runtimes = detail.get("episode_run_time") or [detail.get("runtime") or 0]
            result: dict[str, Any] = {
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
                season_results: list[dict] = []
                mapping_diagnostics: dict[str, list[str]] = {
                    "unmatched_special_episode_ids": [],
                }
                result["episode_mappings"] = _build_tv_episode_mappings(
                    client,
                    provider_id,
                    target,
                    season_results=season_results,
                    diagnostics=mapping_diagnostics,
                    work_detail=detail,
                )
                mapped_episode_ids = {
                    str(item.get("episode_id") or "")
                    for item in result["episode_mappings"]
                    if str(item.get("episode_id") or "").strip()
                }
                unmapped_by_season: dict[tuple[str, int | None], list[dict]] = {}
                for episode in target.get("episodes") or []:
                    if not isinstance(episode, dict) or str(episode.get("episode_kind") or "regular") == "auxiliary":
                        continue
                    episode_id = str(episode.get("episode_id") or "")
                    if not episode_id or episode_id in mapped_episode_ids:
                        continue
                    key = (
                        str(episode.get("season_id") or ""),
                        _positive_int(episode.get("local_season_number")),
                    )
                    unmapped_by_season.setdefault(key, []).append(episode)
                recorded_seasons = {
                    (
                        str(item.get("season_id") or ""),
                        _positive_int(item.get("local_season_number")),
                    )
                    for item in season_results
                }
                for (season_id, local_season_number), _episodes in unmapped_by_season.items():
                    if (season_id, local_season_number) in recorded_seasons:
                        continue
                    provider_season_number = _positive_int(_episodes[0].get("provider_season_number"))
                    if provider_season_number is None:
                        provider_season_number = local_season_number
                    season_results.append({
                        "season_id": season_id,
                        "local_season_number": local_season_number,
                        "provider_season_number": provider_season_number,
                        "status": "partial",
                        "reason_code": "episode_not_found",
                        "failure_stage": "season_detail",
                        "retryable": False,
                    })
                critical_season_results = [
                    item for item in season_results
                    if not _season_result_is_optional_special(item, target)
                ]
                critical_unmapped_episodes = [
                    episode for episodes in unmapped_by_season.values()
                    for episode in episodes
                    if not _is_special_episode(episode)
                ]
                if season_results and not critical_season_results and not critical_unmapped_episodes:
                    # 作品身份和详情已经成功，特别篇缺少线上条目只是可选
                    # 补全，不得把整部作品降级成“服务不可用”。
                    result.update({
                        "metadata_state": "ready",
                        "metadata_warning": "部分特别篇没有对应的在线资料，已保留本地文件名称，不影响播放。",
                        "reason_code": "special_episode_metadata_incomplete",
                        "season_results": season_results,
                        "identity_status": "confirmed",
                        "work_metadata_status": "ready",
                        "episode_mapping_status": "partial",
                        "failure_stage": "season_detail",
                        "retryable": False,
                    })
                elif season_results:
                    result.update({
                        "metadata_state": "source_unavailable",
                        "reason": "部分剧集资料暂不可用，已保留作品信息，可稍后重试",
                        "reason_code": "episode_mapping_incomplete",
                        "season_results": season_results,
                        "identity_status": "confirmed",
                        "work_metadata_status": "ready",
                        "episode_mapping_status": "partial",
                        "failure_stage": "season_detail",
                        "retryable": any(item.get("retryable") for item in season_results),
                    })
                else:
                    result.update({
                        "identity_status": "confirmed",
                        "work_metadata_status": "ready",
                        "episode_mapping_status": "complete" if _has_local_episodes(target) else "not_applicable",
                        "retryable": False,
                    })
                if mapping_diagnostics["unmatched_special_episode_ids"]:
                    # 包含成功读取但名称不匹配，以及明确不存在的特别篇季度。
                    # scrape 阶段据此清掉旧顺序映射，临时网络失败不误删。
                    result["clear_episode_mapping_ids"] = list(dict.fromkeys(
                        mapping_diagnostics["unmatched_special_episode_ids"]
                    ))
            else:
                result.update({
                    "identity_status": "confirmed",
                    "work_metadata_status": "ready",
                    "episode_mapping_status": "not_applicable",
                    "retryable": False,
                })
            return result
    except TMDBClientError as exc:
        if provider_id is not None:
            # 已经确认过的 Provider 身份不能因为一次详情/季度请求失败
            # 被降级成 local；保留标题和 ID，下一次重试可直接复用。
            return {
                "provider": "tmdb",
                "provider_id": str(provider_id),
                "media_type": media_type,
                "title": str(target.get("preferred_title") or ""),
                "original_title": str(target.get("original_title") or ""),
                "metadata_state": "source_unavailable",
                "reason": f"在线资料服务暂不可用，请稍后重试（{type(exc).__name__}）",
                "reason_code": _tmdb_reason_code(exc),
                "episode_mappings": [],
                "identity_status": "confirmed",
                "work_metadata_status": "unavailable",
                "episode_mapping_status": "not_applicable",
                "failure_stage": _tmdb_failure_stage(exc, "work_detail"),
                "retryable": _tmdb_retryable(exc),
            }
        return _local_state(
            "source_unavailable",
            f"在线资料服务暂不可用，请稍后重试（{type(exc).__name__}）",
            reason_code=_tmdb_reason_code(exc),
            failure_stage=_tmdb_failure_stage(exc, "search"),
            retryable=_tmdb_retryable(exc),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if provider_id is not None:
            return {
                "provider": "tmdb",
                "provider_id": str(provider_id),
                "media_type": media_type,
                "title": str(target.get("preferred_title") or ""),
                "original_title": str(target.get("original_title") or ""),
                "metadata_state": "failed",
                "reason": f"在线资料返回内容无效，请稍后重试（{type(exc).__name__}）",
                "reason_code": "invalid_response",
                "episode_mappings": [],
                "identity_status": "confirmed",
                "work_metadata_status": "unavailable",
                "episode_mapping_status": "not_applicable",
                "failure_stage": "work_detail",
                "retryable": False,
            }
        return _local_state(
            "failed",
            f"获取在线元数据失败: {type(exc).__name__}",
            reason_code="invalid_response",
            failure_stage="work_detail",
            retryable=False,
        )


def _build_tv_episode_mappings(
    client: TMDBClient,
    provider_id: int,
    target: dict,
    *,
    season_results: list[dict] | None = None,
    diagnostics: dict[str, list[str]] | None = None,
    work_detail: dict | None = None,
) -> list[dict]:
    """把已确认的本地 Episode 映射到 TMDB 的标题与剧照。

    本地季度和集号永远不在这里改写；这里只保存 provider 的映射和可展示
    的远端字段。特别篇没有远端映射时可保留本地标题；但常规季请求失败时
    不能伪装成完整刮削成功，否则详情页会永久缺失标题与剧照。
    """

    local_episodes = [
        item for item in target.get("episodes") or []
        if str(item.get("episode_id") or "") and str(item.get("episode_kind") or "regular") != "auxiliary"
    ]
    season_cache: dict[int, dict] = {}
    online_seasons = {
        _positive_int(item.get("season_number"))
        for item in (work_detail or {}).get("seasons") or []
        if _positive_int(item.get("season_number")) not in {None, 0}
    }
    if online_seasons == {1} and any(
        not _is_special_episode(item) and (_positive_int(item.get("local_season_number")) or 0) > 1
        for item in local_episodes
    ):
        try:
            season_cache[1] = client.get_tv_season_episodes(provider_id, 1)
        except TMDBClientError:
            # 不凭一次请求失败猜测映射；下面的正常请求路径保留错误分类。
            pass
        else:
            local_episodes = _map_continuous_season(local_episodes, season_cache[1])
    by_provider_season: dict[int, list[dict]] = {}
    for episode in local_episodes:
        # TMDB 的特别篇身份固定在 Season 0。本地旧映射可能把 SP 的
        # provider season 写成正片季号，但它不能继续决定本次请求的季度。
        if _is_special_episode(episode):
            provider_season = 0
        else:
            provider_season = _positive_int(episode.get("provider_season_number"))
            if provider_season is None:
                provider_season = _positive_int(episode.get("local_season_number"))
        if provider_season is None:
            continue
        by_provider_season.setdefault(provider_season, []).append(episode)

    result: list[dict] = []
    for provider_season, episodes in sorted(by_provider_season.items()):
        season_mapping_verified = False
        used_remote_numbers: set[int] = set()
        try:
            season = season_cache.get(provider_season)
            if season is None:
                season = client.get_tv_season_episodes(provider_id, provider_season)
            remote_by_number = {
                int(item["episode_number"]): item
                for item in season.get("episodes") or []
                if _positive_int(item.get("episode_number")) is not None
            }
            season_mapping_verified = True
        except TMDBClientError as exc:
            # 404 已明确该季度没有在线资料，与网络/认证故障不同。
            # 特别篇继续使用本地名称，并撤销旧映射，防止重试再次补回错名。
            season_mapping_verified = _tmdb_reason_code(exc) == "provider_resource_missing"
            if season_results is not None:
                season_results.append({
                    "season_id": str(episodes[0].get("season_id") or ""),
                    "local_season_number": _positive_int(episodes[0].get("local_season_number")),
                    "provider_season_number": provider_season,
                    "status": "source_unavailable",
                    "reason_code": _tmdb_reason_code(exc),
                    "failure_stage": _tmdb_failure_stage(exc, "season_detail"),
                    "retryable": _tmdb_retryable(exc),
                })
            remote_by_number = {}

        for episode in episodes:
            if _is_special_episode(episode):
                matched = _match_special_episode(
                    episode,
                    remote_by_number,
                    used_remote_numbers=used_remote_numbers,
                )
                if matched is None:
                    if season_mapping_verified and diagnostics is not None:
                        diagnostics.setdefault("unmatched_special_episode_ids", []).append(
                            str(episode["episode_id"])
                        )
                    continue
                provider_episode, remote = matched
                used_remote_numbers.add(provider_episode)
            else:
                provider_episode = _positive_int(episode.get("provider_episode_number"))
                if provider_episode is None:
                    provider_episode = _positive_int(episode.get("local_episode_number"))
                if provider_episode is None:
                    continue
                remote = remote_by_number.get(provider_episode) or {}
            if not str(remote.get("id") or "").strip():
                # 没有远端 Episode ID 时不能把空映射写成“已映射”；保留
                # 本地剧集，待下次按缺失季度重试。
                if _is_special_episode(episode) and season_mapping_verified and diagnostics is not None:
                    diagnostics.setdefault("unmatched_special_episode_ids", []).append(
                        str(episode["episode_id"])
                    )
                continue
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


def _map_continuous_season(episodes: list[dict], online_season: dict) -> list[dict]:
    """只生成线上编号副本，不改本地分季；缺集不按文件数量计算偏移。

    线上仅一季时，用前季最大集号推导连续编号，且每个分界必须得到
    线上播出日期长间隔的佐证。缺少前季/季末证据时不盲猜 S1E1。
    """
    remote = {
        int(item["episode_number"]): item
        for item in online_season.get("episodes") or []
        if _positive_int(item.get("episode_number")) is not None
    }
    maxima: dict[int, int] = {}
    season_episodes: dict[int, list[dict]] = {}
    for episode in episodes:
        season = _positive_int(episode.get("local_season_number")) or 0
        number = _positive_int(episode.get("local_episode_number")) or 0
        if season and number and not _is_special_episode(episode):
            maxima[season] = max(maxima.get(season, 0), number)
            season_episodes.setdefault(season, []).append(episode)
    offsets = {1: 0}
    for season in sorted(maxima):
        if season <= 1:
            continue
        local = season_episodes[season]
        if all((_positive_int(item.get("absolute_episode_number")) or 0) > 0 for item in local):
            explicit_offsets = {
                int(item["absolute_episode_number"]) - int(item["local_episode_number"])
                for item in local
            }
            if len(explicit_offsets) == 1 and min(explicit_offsets) >= 0:
                offsets[season] = next(iter(explicit_offsets))
                continue
        if season - 1 not in offsets:
            continue
        boundary = offsets[season - 1] + maxima[season - 1]
        # 可能已经使用全系列集号，也可能是缺集；没有绝对编号证据不能重复加偏移。
        if min(int(item["local_episode_number"]) for item in local) > boundary:
            continue
        try:
            before = date.fromisoformat(str(remote[boundary].get("air_date") or ""))
            after = date.fromisoformat(str(remote[boundary + 1].get("air_date") or ""))
        except (KeyError, ValueError):
            continue
        if (after - before).days >= 60:
            offsets[season] = boundary
    mapped = []
    for episode in episodes:
        season = _positive_int(episode.get("local_season_number")) or 0
        number = _positive_int(episode.get("local_episode_number")) or 0
        existing_season = _positive_int(episode.get("provider_season_number"))
        offset = offsets.get(season)
        provider_number = _positive_int(episode.get("absolute_episode_number")) or (
            offset + number if offset is not None else None
        )
        if (
            season > 1 and number and provider_number is not None
            and not _is_special_episode(episode)
            and existing_season in {None, 1, season}
            and episode.get("provider_episode_number") is None
            and str(remote.get(provider_number, {}).get("id") or "")
        ):
            mapped.append({**episode, "provider_season_number": 1, "provider_episode_number": provider_number})
        else:
            mapped.append(episode)
    return mapped


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
