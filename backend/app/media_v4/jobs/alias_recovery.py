"""confirmed Work 的后置名称恢复；所有身份裁决仍经 TMDB/既有 Ranker。"""

from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config import load_config
from app.media_v4.jobs.metadata import _target_titles, default_metadata_provider
from app.scrape.alias_contract import AliasEvidence, clean_aliases
from app.scrape.provider_budget import ProviderDeferred, acquire, cool_down

NAME_REASONS = frozenset({"no_candidates", "ambiguous_candidates"})
RETRY_REASONS = frozenset({"source_unavailable", "provider_unavailable", "provider_network_error", "provider_timeout"})


def anilist_names(target: dict) -> list[AliasEvidence]:
    from app.scrape.anilist_client import AniListClient

    if not load_config().anilist_enabled or target.get("show_type") not in {"anime_series", "anime_movie", "anime"}:
        return []
    evidence = []
    stamp = datetime.now(UTC).isoformat()
    with AniListClient(rate_limit=3, should_cancel=target.get('_should_cancel', lambda: False)) as client:
        for query in _target_titles(target)[:2]:
            for item in client.search_names(query, per_page=3):
                identifier = str(item.get("id") or "")
                if not identifier.isdigit():
                    continue
                titles = item.get("title") or {}
                for title in clean_aliases([titles.get("native"), titles.get("romaji"), titles.get("english"),
                                            *(item.get("synonyms") or [])]):
                    evidence.append(AliasEvidence(title, "", "anilist", identifier,
                                                  f"https://anilist.co/anime/{identifier}", stamp))
    return evidence[:6]


def bangumi_names(target: dict) -> list[AliasEvidence]:
    from app.integrations.bangumi import BangumiClient

    if target.get("show_type") not in {"anime_series", "anime_movie", "anime"}:
        return []
    client = BangumiClient(public_only=True)
    evidence = []
    stamp = datetime.now(UTC).isoformat()
    for query in _target_titles(target)[:2]:
        payload = _bangumi_request(target, lambda query=query: client.search_subjects(query, limit=1, subject_types=[2]))
        for item in (payload.get("data") or [])[:1]:
            identifier = str(item.get("id") or "")
            if not identifier.isdigit():
                continue
            detail = _bangumi_request(target, lambda identifier=identifier: client.get_subject(int(identifier)))
            values = [detail.get("name"), detail.get("name_cn")]
            for field in detail.get("infobox") or []:
                if field.get("key") not in {"别名", "中文名", "英文名", "日文名"}:
                    continue
                value = field.get("value")
                values.extend([v.get("v") for v in value if isinstance(v, dict)] if isinstance(value, list) else [value])
            for title in clean_aliases(values):
                evidence.append(AliasEvidence(title, "", "bangumi", identifier,
                                              f"https://bgm.tv/subject/{identifier}", stamp))
    return evidence[:6]


def _bangumi_request(target, request):
    acquire('bangumi_alias', 3, target.get('_should_cancel', lambda: False))
    try:
        return request()
    except Exception as exc:
        if getattr(exc, 'status_code', 0) == 429:
            try:
                delay = max(1, min(int(getattr(exc, 'retry_after', '') or 60), 86400))
            except (TypeError, ValueError):
                delay = 60
            cool_down('bangumi_alias', delay)
            raise ProviderDeferred(delay) from exc
        raise


def recover_metadata(target: dict, *, metadata_provider: Callable = default_metadata_provider,
                     name_providers=None, should_cancel: Callable[[], bool] = lambda: False, cache=None) -> dict:
    """先一次有界 TMDB 重试，名称仅在真实无安全候选且无绑定时检索。"""
    trace = []
    if should_cancel():
        return {"provider": "local", "metadata_state": "waiting_metadata", "reason_code": "cancelled"}
    result = metadata_provider(target)
    trace.append({"provider": "tmdb", "phase": "retry", "reason_code": result.get("reason_code", "")})
    if (result.get("metadata_state") == "ready" or target.get("provider_bindings")
            or result.get("reason_code") not in NAME_REASONS or should_cancel()):
        return {**result, "alias_recovery_trace": trace}
    for name_provider in name_providers if name_providers is not None else (anilist_names, bangumi_names):
        if should_cancel():
            break
        cached = cache.get(target, name_provider.__name__) if cache is not None else None
        cache_status = 'hit' if cached else 'miss'
        if cached and cached['status'] == 'cooldown':
            trace.append({'provider': name_provider.__name__, 'status': 'unavailable', 'cache_status': 'hit',
                          'reason_code': cached.get('reason_code', 'provider_unavailable')})
            return {**result, 'alias_recovery_trace': trace,
                    'alias_deferred_seconds': max(1, int(cached['expires_at'] - cache.now()))}
        try:
            evidence = ([AliasEvidence(**item) for item in cached['evidence']] if cached else
                        name_provider({**target, '_should_cancel': should_cancel}))
        except Exception as exc:
            if should_cancel():
                break
            # 记录错误类别，不记录 Provider 响应正文或用户凭据。
            reason = _provider_error_reason(exc)
            trace.append({"provider": name_provider.__name__, "status": "unavailable", "error": type(exc).__name__,
                          'reason_code': reason, 'cache_status': cache_status})
            retry_after = getattr(exc, 'retry_after', 0)
            if cache is not None:
                cache.put_error(target, name_provider.__name__, reason, int(retry_after or 60))
            if retry_after:
                return {**result, "alias_recovery_trace": trace, "alias_deferred_seconds": int(retry_after)}
            continue
        if should_cancel():
            break
        if cache is not None and not cached:
            cache.put(target, name_provider.__name__, evidence)
        queries = clean_aliases([item.title for item in evidence], limit=4)
        trace.append({"provider": name_provider.__name__, "status": "completed",
                      'cache_status': cache_status,
                      "evidence": [item.to_dict() for item in evidence if item.title in queries]})
        if not queries or should_cancel():
            continue
        result = metadata_provider({**target, "recovery_search_queries": queries})
        if result.get("metadata_state") == "ready" or result.get("reason_code") not in NAME_REASONS:
            break
    return {**result, "alias_recovery_trace": trace}


def _provider_error_reason(exc) -> str:
    if getattr(exc, 'retry_after', 0) or getattr(exc, 'status_code', 0) == 429:
        return 'provider_rate_limited'
    if getattr(exc, 'status_code', 0) in {401, 403}:
        return 'provider_unauthorized'
    code = getattr(exc, 'error_code', '')
    if code == 'timeout':
        return 'provider_timeout'
    if code in {'network_unavailable', 'proxy_unavailable'}:
        return 'provider_network_error'
    return getattr(exc, 'reason_code', '') or ('provider_timeout' if isinstance(exc, TimeoutError) else 'provider_unavailable')
