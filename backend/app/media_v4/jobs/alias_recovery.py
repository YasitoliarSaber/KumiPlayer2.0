"""confirmed Work 的后置名称恢复；所有身份裁决仍经 TMDB/既有 Ranker。"""

from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config import load_config
from app.media_v4.jobs.metadata import _target_titles, default_metadata_provider
from app.scrape.alias_contract import AliasEvidence, clean_aliases
from app.scrape.provider_budget import acquire, cool_down

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
            for item in client.search_anime(query, None, per_page=3):
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
    client = BangumiClient()
    evidence = []
    stamp = datetime.now(UTC).isoformat()
    for query in _target_titles(target)[:2]:
        acquire("bangumi_alias", 3, target.get('_should_cancel', lambda: False))
        try:
            payload = client.search_subjects(query, limit=1, subject_types=[2])
        except Exception as exc:
            if getattr(exc, 'status_code', 0) == 429:
                cool_down("bangumi_alias", int(getattr(exc, 'retry_after', '') or 60))
            raise
        for item in (payload.get("data") or [])[:1]:
            identifier = str(item.get("id") or "")
            if not identifier.isdigit():
                continue
            acquire("bangumi_alias", 3, target.get('_should_cancel', lambda: False))
            detail = client.get_subject(int(identifier))
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


def recover_metadata(target: dict, *, metadata_provider: Callable = default_metadata_provider,
                     name_providers=None, should_cancel: Callable[[], bool] = lambda: False) -> dict:
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
        try:
            evidence = name_provider({**target, '_should_cancel': should_cancel})
        except Exception as exc:
            # 记录错误类别，不记录 Provider 响应正文或用户凭据。
            trace.append({"provider": name_provider.__name__, "status": "unavailable", "error": type(exc).__name__})
            retry_after = getattr(exc, 'retry_after', 0)
            if retry_after:
                return {**result, "alias_recovery_trace": trace, "alias_deferred_seconds": int(retry_after)}
            continue
        queries = clean_aliases([item.title for item in evidence], limit=4)
        trace.append({"provider": name_provider.__name__, "status": "completed",
                      "evidence": [item.to_dict() for item in evidence if item.title in queries]})
        if not queries or should_cancel():
            continue
        result = metadata_provider({**target, "recovery_search_queries": queries})
        if result.get("metadata_state") == "ready" or result.get("reason_code") not in NAME_REASONS:
            break
    return {**result, "alias_recovery_trace": trace}
