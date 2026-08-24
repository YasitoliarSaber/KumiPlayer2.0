"""确认前候选身份解析（P-001 7.7 R1）。

在 draft/preview 与 confirm 之间建立服务器权威的候选解析：查询输入来自
ParsedFacts title_candidates、Work 标题、显式 hint、既有别名；候选按
revision_id + draft_work_key 在确认前持久化；高置信唯一身份用于确认时
合并同一 provider identity 的 draft Work，歧义生成 issue 阻断确认。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from app.media_v4.domain.models import (
    ParsedFacts,
    ResolutionIssue,
    ResolvedMediaGraph,
    ResolvedWork,
    SourceEvidence,
)

_SUPPORTED_PROVIDERS = frozenset({"tmdb", "anilist", "bangumi"})


@dataclass(frozen=True, slots=True)
class WorkCandidate:
    work_key: str
    provider: str
    provider_id: str
    media_type: str
    title: str
    year: int | None
    evidence: str
    confidence: str
    status: str = "proposed"


CandidateSearch = Callable[[str, list[str], int | None, str], list[WorkCandidate]]


def _normalize_title(value: str) -> str:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip(" ._-·:：/\\()（）【】[]{}<>《》「」『』\"'")


def _confidence_rank(confidence: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(confidence, 0)


def _score_candidate(
    candidate: WorkCandidate,
    work: ResolvedWork,
    queries: list[str],
    nfo_titles: list[str],
) -> WorkCandidate:
    """可解释候选评分：标题/原文精确匹配 + 年份一致 → high；否则 medium。"""

    query_norms = {_normalize_title(q) for q in queries}
    candidate_norm = _normalize_title(candidate.title)
    # 身份自动确认只接受规范化后的完整标题相等。前缀关系（Show/Showdown）
    # 不构成同一作品的证据，必须留给人工候选处理。
    exact_title = any(candidate_norm == norm for norm in query_norms if norm)
    year_ok = candidate.year is None or work.year is None or candidate.year == work.year
    confidence = "high" if exact_title and year_ok else "medium"
    evidence = candidate.evidence
    if nfo_titles:
        evidence = f"{evidence};sidecar_nfo"
    return replace(candidate, confidence=confidence, evidence=evidence, status="proposed")


def supported_provider(provider: str) -> bool:
    return provider in _SUPPORTED_PROVIDERS


def build_query_inputs(work: ResolvedWork, entries: list[tuple[SourceEvidence, ParsedFacts]]) -> list[str]:
    """构造候选查询输入，去重并过滤通用容器标题；含 sidecar NFO 标题。"""

    from app.media_v4.generic_container import is_generic_container_title

    queries: list[str] = []
    related = [
        facts
        for _evidence, facts in entries
        if facts.evidence_id in work.source_evidence_ids
    ]
    for facts in related:
        # series_group 只表达父系列关系，不能作为独立外传/电影子作品的
        # Provider 身份查询输入；否则 Heya Camp 会被 Yuru Camp 候选吸收。
        for value in (facts.work_title, facts.original_title, *facts.title_candidates):
            value = (value or "").strip()
            if value and not is_generic_container_title(value) and value not in queries:
                queries.append(value)
    if work.preferred_title and work.preferred_title not in queries:
        queries.append(work.preferred_title)
    return queries[:8]


def _nfo_related_titles(
    work: ResolvedWork,
    entries: list[tuple[SourceEvidence, ParsedFacts]],
) -> tuple[list[str], bool]:
    """关联同目录/同 stem 的 sidecar NFO 标题，作为候选输入证据。"""

    from app.media_v4.generic_container import is_generic_container_title
    from app.media_v4.parsing.parser import _normalize_filename_stem

    video_dirs: set[str] = set()
    for evidence, _facts in entries:
        if evidence.evidence_id in work.source_evidence_ids:
            parts = PurePosixPath(evidence.relative_path).parts
            video_dirs.add(PurePosixPath(*parts[:-1]).as_posix() if len(parts) > 1 else "")
    titles: list[str] = []
    found = False
    for evidence, facts in entries:
        if evidence.entry_kind != "metadata" or facts.is_importable:
            continue
        parts = PurePosixPath(evidence.relative_path).parts
        parent = PurePosixPath(*parts[:-1]).as_posix() if len(parts) > 1 else ""
        stem = _normalize_filename_stem(PurePosixPath(evidence.relative_path).stem)
        if not stem or is_generic_container_title(stem):
            continue
        if parent in video_dirs or _normalize_title(stem) == _normalize_title(work.preferred_title):
            if stem not in titles:
                titles.append(stem)
            found = True
    return titles, found


def plan_work_candidates(
    graph: ResolvedMediaGraph,
    entries: list[tuple[SourceEvidence, ParsedFacts]],
    search: CandidateSearch,
    *,
    existing_bindings: dict[str, list[tuple[str, str, str]]] | None = None,
) -> tuple[dict[str, list[WorkCandidate]], dict[str, str], list[ResolutionIssue]]:
    """为每个 draft Work 生成候选，并给出合并映射与歧义 issue。"""

    bindings = existing_bindings or {}
    candidates_by_key: dict[str, list[WorkCandidate]] = {}
    merge_map: dict[str, str] = {}
    issues: list[ResolutionIssue] = []

    for work in graph.works:
        evidence_ids = set(work.source_evidence_ids)
        related = [
            (evidence, facts)
            for evidence, facts in entries
            if evidence.evidence_id in evidence_ids
        ]
        # 1) 显式 hint：高置信候选。
        hint_ids: list[tuple[str, str]] = []
        for _evidence, facts in related:
            if facts.tmdb_hint_id and facts.tmdb_hint_type:
                hint = (facts.tmdb_hint_type.casefold(), str(facts.tmdb_hint_id))
                if hint not in hint_ids:
                    hint_ids.append(hint)
        # 2) 既有 provider binding。
        existing = bindings.get(work.work_key, [])
        confirmed: dict[tuple[str, str, str], WorkCandidate] = {}
        for provider, media_type, provider_id in existing:
            if not supported_provider(provider):
                continue
            candidate = WorkCandidate(
                work_key=work.work_key,
                provider=provider,
                provider_id=provider_id,
                media_type=media_type,
                title=work.preferred_title,
                year=work.year,
                evidence="existing_provider_binding",
                confidence="high",
                status="confirmed",
            )
            confirmed[(provider, media_type, provider_id)] = candidate
        # 3) 在线/测试搜索候选（含 sidecar NFO 标题输入）。
        queries = build_query_inputs(work, entries)
        nfo_titles, has_nfo = _nfo_related_titles(work, entries)
        for title in nfo_titles:
            if title not in queries:
                queries.append(title)
        searched: list[WorkCandidate] = []
        if queries:
            try:
                searched = search(work.work_key, queries, work.year, work.media_type) or []
            except Exception:
                searched = []
        candidates: dict[tuple[str, str, str], WorkCandidate] = {**confirmed}
        for candidate in searched:
            if not supported_provider(candidate.provider):
                continue
            key = (candidate.provider, candidate.media_type, candidate.provider_id)
            scored = _score_candidate(candidate, work, queries, nfo_titles)
            if key not in candidates or _confidence_rank(scored.confidence) > _confidence_rank(candidates[key].confidence):
                candidates[key] = scored

        # hint 未出现在候选里时补为高置信 proposed。
        for provider, provider_id in hint_ids:
            key = (provider, "tv", provider_id)
            if key not in candidates:
                candidates[key] = WorkCandidate(
                    work_key=work.work_key,
                    provider=provider,
                    provider_id=provider_id,
                    media_type="tv",
                    title=work.preferred_title,
                    year=work.year,
                    evidence="parsed_tmdb_hint",
                    confidence="high",
                    status="proposed",
                )

        candidate_list = list(candidates.values())
        candidates_by_key[work.work_key] = candidate_list

        # 唯一高置信身份 → 确认时合并到同一 provider identity；歧义 → issue。
        high_confidence = [
            item for item in candidate_list
            if item.confidence == "high" and item.status in {"confirmed", "proposed"}
        ]
        unique_identities = {(item.provider, item.media_type, item.provider_id) for item in high_confidence}
        if len(unique_identities) == 1 and high_confidence:
            chosen = high_confidence[0]
            if chosen.status != "confirmed":
                candidates_by_key[work.work_key] = [
                    replace(item, status="confirmed" if item is chosen or (
                        item.provider == chosen.provider and item.media_type == chosen.media_type
                        and item.provider_id == chosen.provider_id
                    ) else "rejected")
                    for item in candidate_list
                ]
        elif len(unique_identities) > 1:
            issues.append(ResolutionIssue(
                code="candidate_ambiguous",
                evidence_id=next(iter(evidence_ids), ""),
                message="该作品存在多个互不相同的可信 Provider 候选，需人工确认后才能导入",
            ))

    # 合并映射：同一 provider identity 的多个 draft Work → 首个 work_key。
    identity_owner: dict[tuple[str, str, str], str] = {}
    for work_key in graph.works:
        for item in candidates_by_key.get(work_key.work_key, []):
            if item.status != "confirmed":
                continue
            identity = (item.provider, item.media_type, item.provider_id)
            owner = identity_owner.setdefault(identity, work_key.work_key)
            if owner != work_key.work_key:
                merge_map[work_key.work_key] = owner
    return candidates_by_key, merge_map, issues


def merge_graph(graph: ResolvedMediaGraph, merge_map: dict[str, str]) -> ResolvedMediaGraph:
    """把 merge_map 中 child work_key 的剧集/资产/关系合并到 parent work_key。"""

    if not merge_map:
        return graph

    def target(key: str) -> str:
        seen: set[str] = set()
        while key in merge_map and key not in seen:
            seen.add(key)
            key = merge_map[key]
        return key

    works_by_key: dict[str, ResolvedWork] = {}
    for work in graph.works:
        key = target(work.work_key)
        existing = works_by_key.get(key)
        if existing is None:
            works_by_key[key] = replace(work, work_key=key)
            continue
        merged = replace(
            existing,
            source_evidence_ids=tuple(
                dict.fromkeys((*existing.source_evidence_ids, *work.source_evidence_ids))
            ),
        )
        works_by_key[key] = merged

    episodes = tuple(
        replace(episode, work_key=target(episode.work_key))
        for episode in graph.episodes
    )
    work_assets = tuple(
        replace(asset, work_key=target(asset.work_key))
        for asset in graph.work_assets
    )
    relations = tuple(
        replace(
            relation,
            parent_work_key=target(relation.parent_work_key),
            child_work_key=target(relation.child_work_key),
        )
        for relation in graph.relations
    )
    return replace(
        graph,
        works=tuple(works_by_key.values()),
        episodes=episodes,
        work_assets=work_assets,
        relations=relations,
    )


def default_candidate_search(work_key: str, queries: list[str], year: int | None, media_type: str) -> list[WorkCandidate]:
    """生产默认搜索：配置 Token 时用 TMDB；否则返回空（离线场景走 hint/既有绑定）。

    置信度由 plan_work_candidates 按标题/年份评分，这里只返回原始候选（medium）。
    """

    from app.core.config import load_config
    from app.media_v4.jobs.metadata import search_tmdb_candidates

    config = load_config()
    if not config.tmdb_bearer_token:
        return []
    results: list[WorkCandidate] = []
    for query in queries:
        for item in search_tmdb_candidates(query, media_type, year) or []:
            results.append(WorkCandidate(
                work_key=work_key,
                provider="tmdb",
                provider_id=str(item.get("provider_id") or ""),
                media_type=item.get("media_type") or media_type,
                title=item.get("title") or query,
                year=item.get("year"),
                evidence="online_search",
                confidence="medium",
                status="proposed",
            ))
    return results
