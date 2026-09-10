"""确认前候选身份解析（P-001 7.7 R1）。

在 draft/preview 与 confirm 之间建立服务器权威的候选解析：查询输入来自
ParsedFacts title_candidates、Work 标题、显式 hint、既有别名；候选按
revision_id + draft_work_key 在确认前持久化；高置信唯一身份用于确认时
合并同一 provider identity 的 draft Work，歧义生成 issue 阻断确认。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from app.media_v4.domain.models import (
    ParsedFacts,
    ResolutionIssue,
    ResolvedEpisode,
    ResolvedMediaGraph,
    ResolvedWork,
    ResolvedWorkAsset,
    SourceEvidence,
)
from app.media_v4.parsing.parser import _normalize_filename_stem

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
    original_title: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


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
    """可解释候选评分：本地标题集与 provider primary/original/可信 alias
    完整规范化等值匹配且年份不冲突才 high；禁止前缀/模糊自动匹配。"""

    query_norms = {_normalize_title(q) for q in queries}
    query_norms.discard("")
    local_norms = {_normalize_title(work.preferred_title)} | query_norms
    local_norms.discard("")
    provider_norms = {
        _normalize_title(candidate.title),
        _normalize_title(candidate.original_title),
        *(_normalize_title(alias) for alias in candidate.aliases),
    }
    provider_norms.discard("")
    # 身份自动确认只接受规范化后的完整标题相等。前缀关系（Show/Showdown）
    # 不构成同一作品的证据，必须留给人工候选处理。
    exact_title = bool(provider_norms & local_norms)
    year_ok = candidate.year is None or work.year is None or candidate.year == work.year
    confidence = "high" if exact_title and year_ok else "medium"
    evidence = candidate.evidence
    if nfo_titles:
        evidence = f"{evidence};sidecar_nfo"
    return replace(candidate, confidence=confidence, evidence=evidence, status="proposed")


def supported_provider(provider: str) -> bool:
    return provider in _SUPPORTED_PROVIDERS


def build_query_inputs(
    work: ResolvedWork,
    entries: list[tuple[SourceEvidence, ParsedFacts]],
    related_facts: list[ParsedFacts] | None = None,
) -> list[str]:
    """构造候选查询输入，去重并过滤通用容器标题；含 sidecar NFO 标题。

    ``related_facts`` 允许调用方传入预构建的 Work 关联事实，避免大库在
    每部作品上重复遍历全部条目。
    """

    from app.media_v4.generic_container import is_generic_container_title

    queries: list[str] = []
    if related_facts is None:
        related = [
            facts
            for _evidence, facts in entries
            if facts.evidence_id in work.source_evidence_ids
        ]
    else:
        related = related_facts
    for facts in related:
        # series_group 只表达父系列关系，不能作为独立外传/电影子作品的
        # Provider 身份查询输入；否则 Heya Camp 会被 Yuru Camp 候选吸收。
        for value in (facts.work_title, facts.original_title, *facts.title_candidates):
            value = (value or "").strip()
            # title_candidates 由解析层提供给展示与诊断，也会包含父系列标题。
            # 当前 Work 已经是独立子作品时，父系列仅能建立 relation，绝不能
            # 参与自身 Provider 检索或精确评分，否则父系列别名会制造伪歧义。
            parent_norm = _normalize_title(facts.series_group)
            work_norm = _normalize_title(facts.work_title)
            if parent_norm and parent_norm != work_norm and _normalize_title(value) == parent_norm:
                continue
            if value and not is_generic_container_title(value) and value not in queries:
                queries.append(value)
    if work.preferred_title and work.preferred_title not in queries:
        queries.append(work.preferred_title)
    return queries[:8]


def compute_nfo_ownership(
    graph: ResolvedMediaGraph,
    entries: list[tuple[SourceEvidence, ParsedFacts]],
) -> tuple[dict[str, set[str]], list[str]]:
    """NFO→Work 确定性归属（R17）。

    规则：文件 stem 与 Work 标题精确规范化等值可关联；目录级
    tvshow.nfo / movie.nfo 仅在该目录恰好只有一个 Work 时可关联；
    其他多 Work 情形返回歧义 evidence_id，不得注入任一 Work。
    """

    work_dirs: dict[str, set[str]] = {}
    entries_by_id = {evidence.evidence_id: evidence for evidence, _facts in entries}
    for work in graph.works:
        dirs: set[str] = set()
        for evidence_id in work.source_evidence_ids:
            evidence = entries_by_id.get(evidence_id)
            if evidence is None:
                continue
            parts = PurePosixPath(evidence.relative_path).parts
            dirs.add(PurePosixPath(*parts[:-1]).as_posix() if len(parts) > 1 else "")
        work_dirs[work.work_key] = dirs

    def shares_directory_scope(nfo_parent: str, video_dirs: set[str]) -> bool:
        """NFO 所在目录须等于或是视频目录的祖先。

        允许 ``Show/tvshow.nfo`` 对应 ``Show/Season 1/*.mkv``，但拒绝
        ``Backup/Show.nfo`` 仅因文件名相同跨目录绑定媒体库中的 Show。
        """

        parent_parts = tuple(part.casefold() for part in PurePosixPath(nfo_parent).parts)
        for directory in video_dirs:
            directory_parts = tuple(part.casefold() for part in PurePosixPath(directory).parts)
            if directory_parts[:len(parent_parts)] == parent_parts:
                return True
        return False

    ownership: dict[str, set[str]] = {}
    ambiguous: list[str] = []
    for evidence, facts in entries:
        if evidence.entry_kind != "metadata" or facts.is_importable:
            continue
        parts = PurePosixPath(evidence.relative_path).parts
        parent = PurePosixPath(*parts[:-1]).as_posix() if len(parts) > 1 else ""
        stem = _normalize_filename_stem(PurePosixPath(evidence.relative_path).stem)
        if stem.casefold() in {"tvshow", "movie"}:
            owners = {
                work_key for work_key, video_dirs in work_dirs.items()
                if shares_directory_scope(parent, video_dirs)
            }
        else:
            stem_norm = _normalize_title(stem)
            owners = {
                work.work_key for work in graph.works
                if stem_norm
                and stem_norm == _normalize_title(work.preferred_title)
                and shares_directory_scope(parent, work_dirs.get(work.work_key, set()))
            }
        if len(owners) > 1:
            ambiguous.append(evidence.evidence_id)
        elif len(owners) == 1:
            ownership[evidence.evidence_id] = owners
    return ownership, ambiguous


def _nfo_related_titles(
    work: ResolvedWork,
    entries: list[tuple[SourceEvidence, ParsedFacts]],
    ownership: dict[str, set[str]],
    nfo_facts: list[tuple[SourceEvidence, ParsedFacts]] | None = None,
) -> tuple[list[str], bool]:
    """关联到该 Work 的 sidecar NFO 标题，作为候选查询证据。

    ``nfo_facts`` 允许传入按 owner 预分组的事实列表，避免大库逐 Work
    重复遍历全部条目。
    """

    from app.media_v4.generic_container import is_generic_container_title

    titles: list[str] = []
    found = False
    if nfo_facts is None:
        nfo_facts = [
            (evidence, facts)
            for evidence, facts in entries
            if evidence.entry_kind == "metadata"
            and not facts.is_importable
            and ownership.get(evidence.evidence_id) == {work.work_key}
        ]
    for _evidence, facts in nfo_facts:
        for value in (facts.work_title, facts.original_title, *facts.title_candidates):
            value = (value or "").strip()
            if not value or value.casefold() in {"tvshow", "movie"}:
                continue
            if is_generic_container_title(value):
                continue
            if value not in titles:
                titles.append(value)
            found = True
    return titles, found


def plan_work_candidates(
    graph: ResolvedMediaGraph,
    entries: list[tuple[SourceEvidence, ParsedFacts]],
    search: CandidateSearch,
    *,
    existing_bindings: dict[str, list[tuple[str, str, str]]] | None = None,
    preserve_candidate_status: bool = False,
) -> tuple[dict[str, list[WorkCandidate]], dict[str, str], list[ResolutionIssue]]:
    """为每个 draft Work 生成候选，并给出合并映射与歧义 issue。

    ``preserve_candidate_status`` 用于确认阶段复核已持久化的候选快照。
    此时不重新评分或把冲突候选降级，否则同一份草稿会在预览与确认之间改变
    issue 语义；新候选仍按默认路径评分。
    """

    bindings = existing_bindings or {}
    candidates_by_key: dict[str, list[WorkCandidate]] = {}
    merge_map: dict[str, str] = {}
    issues: list[ResolutionIssue] = []
    # R17：NFO→Work 确定性归属；多 Work 歧义写入 issue 并阻断注入。
    nfo_ownership, ambiguous_nfo = compute_nfo_ownership(graph, entries)
    for evidence_id in ambiguous_nfo:
        issues.append(ResolutionIssue(
            code="sidecar_nfo_ambiguous",
            evidence_id=evidence_id,
            message="目录级 NFO 同时对应多个作品，无法确定归属，需人工确认",
        ))

    # C8 性能索引：预建 evidence→entry、Work 成员与 NFO 归属映射，避免
    # 大库在每部作品上重复遍历全部条目（Work × Evidence 退化）。
    entries_by_id = {
        evidence.evidence_id: (evidence, facts)
        for evidence, facts in entries
    }
    nfo_facts_by_owner: dict[str, list[tuple[SourceEvidence, ParsedFacts]]] = defaultdict(list)
    for evidence, facts in entries:
        if evidence.entry_kind != "metadata" or facts.is_importable:
            continue
        owners = nfo_ownership.get(evidence.evidence_id)
        if owners and len(owners) == 1:
            nfo_facts_by_owner[next(iter(owners))].append((evidence, facts))

    for work in graph.works:
        related = [
            entries_by_id[evidence_id]
            for evidence_id in work.source_evidence_ids
            if evidence_id in entries_by_id
        ]
        # 1) 显式 hint：高置信候选。
        hint_ids: list[tuple[str, str, str]] = []
        for _evidence, facts in related:
            if facts.tmdb_hint_id and facts.tmdb_hint_type:
                media_type = facts.tmdb_hint_type.casefold()
                if media_type not in {"movie", "tv"}:
                    continue
                hint = ("tmdb", media_type, str(facts.tmdb_hint_id))
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
        # 重构前已经核验的路径身份属于只读事实库。它不参与纯文件名解析，
        # 但应在草稿候选层恢复为高置信 Provider 身份，从而把同一作品的
        # 中英文发布包合并，并保持电影/外传边界。
        from app.recognition.verified_titles import match_verified_tmdb_binding

        for evidence, _facts in related:
            binding = match_verified_tmdb_binding(evidence.relative_path)
            if binding is None or binding.tmdb_type != work.media_type:
                continue
            key = ("tmdb", binding.tmdb_type, str(binding.tmdb_id))
            confirmed.setdefault(
                key,
                WorkCandidate(
                    work_key=work.work_key,
                    provider="tmdb",
                    provider_id=str(binding.tmdb_id),
                    media_type=binding.tmdb_type,
                    title=binding.canonical_title,
                    year=work.year,
                    evidence="verified_path_binding",
                    confidence="high",
                    status="confirmed",
                ),
            )
        # 3) 在线/测试搜索候选（含 sidecar NFO 标题输入）。
        nfo_facts = nfo_facts_by_owner.get(work.work_key, [])
        queries = build_query_inputs(work, entries, related_facts=[facts for _e, facts in related])
        nfo_titles, has_nfo = _nfo_related_titles(work, entries, nfo_ownership, nfo_facts=nfo_facts)
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
        # D2 采用顺序：既有核验绑定是最高优先身份。在线候选与核验身份不同
        # 时必须降级（不得并列 high 制造假歧义——独立失败项 candidate_ambiguous
        # 的根因），相同身份则保持幂等。
        verified_keys = set(confirmed)
        for candidate in searched:
            if not supported_provider(candidate.provider):
                continue
            key = (candidate.provider, candidate.media_type, candidate.provider_id)
            scored = (
                candidate
                if preserve_candidate_status
                else _score_candidate(candidate, work, queries, nfo_titles)
            )
            if not preserve_candidate_status and verified_keys and key not in verified_keys:
                scored = replace(scored, confidence="medium", status="rejected")
            if key not in candidates or _confidence_rank(scored.confidence) > _confidence_rank(candidates[key].confidence):
                candidates[key] = scored

        # hint 未出现在候选里时补为高置信 proposed。
        for provider, media_type, provider_id in hint_ids:
            key = (provider, media_type, provider_id)
            if key not in candidates:
                candidates[key] = WorkCandidate(
                    work_key=work.work_key,
                    provider=provider,
                    provider_id=provider_id,
                    media_type=media_type,
                    title=work.preferred_title,
                    year=work.year,
                    evidence="parsed_tmdb_hint",
                    confidence="high",
                    status="proposed",
                )
        # sidecar NFO 内容解析出的 provider ID 是强候选证据（同目录/同 stem 关联）。
        for _evidence, facts in nfo_facts_by_owner.get(work.work_key, []):
            if not facts.tmdb_hint_id or not facts.tmdb_hint_type:
                continue
            key = ("tmdb", facts.tmdb_hint_type.casefold(), str(facts.tmdb_hint_id))
            if key not in candidates:
                candidates[key] = WorkCandidate(
                    work_key=work.work_key,
                    provider="tmdb",
                    provider_id=str(facts.tmdb_hint_id),
                    media_type=facts.tmdb_hint_type.casefold(),
                    title=facts.work_title or work.preferred_title,
                    original_title=facts.original_title,
                    year=facts.year_candidate or work.year,
                    evidence="sidecar_nfo_provider",
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
                evidence_id=next(
                    (evidence.evidence_id for evidence, _facts in related), ""
                ),
                message="该作品存在多个互不相同的可信 Provider 候选，需人工确认后才能导入",
            ))

    from app.media_v4.resolution.identity_policy import reject_conflicting_candidates

    candidates_by_key, identity_issues = reject_conflicting_candidates(graph, candidates_by_key)
    issues.extend(identity_issues)
    merge_map = merge_map_from_candidates(graph, candidates_by_key)
    return candidates_by_key, merge_map, issues


def merge_map_from_candidates(
    graph: ResolvedMediaGraph,
    candidates_by_key: dict[str, list[WorkCandidate]],
) -> dict[str, str]:
    """按候选标题确定性选择同一 Provider 身份的主 Work。"""

    works_by_key = {work.work_key: work for work in graph.works}
    identity_members: dict[tuple[str, str, str], list[tuple[str, WorkCandidate]]] = {}
    for work in graph.works:
        for item in candidates_by_key.get(work.work_key, []):
            if item.status != "confirmed":
                continue
            identity = (item.provider, item.media_type, item.provider_id)
            identity_members.setdefault(identity, []).append((work.work_key, item))

    merge_map: dict[str, str] = {}
    for members in identity_members.values():
        if len(members) < 2:
            continue

        from app.media_v4.resolution.identity_policy import can_merge_provider_identity

        if not can_merge_provider_identity(
            graph,
            {work_key for work_key, _item in members},
        ):
            continue

        def owner_rank(member: tuple[str, WorkCandidate]) -> tuple[int, int, str]:
            work_key, candidate = member
            work = works_by_key[work_key]
            work_title = _normalize_title(work.preferred_title)
            candidate_title = _normalize_title(candidate.title)
            return (
                0 if work_title and work_title == candidate_title else 1,
                len(work_title),
                work_key,
            )

        owner = min(members, key=owner_rank)[0]
        for work_key, _item in members:
            if work_key != owner:
                merge_map[work_key] = owner
    return merge_map


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
        source_evidence_ids = tuple(
            dict.fromkeys((*existing.source_evidence_ids, *work.source_evidence_ids))
        )
        # target 指向的 Work 是上一步按权威候选标题挑出的代表。即使它在
        # 原图中的遍历顺序晚于 child，也必须保留代表的标题、年份和类型。
        merged = replace(
            work if work.work_key == key else existing,
            work_key=key,
            source_evidence_ids=source_evidence_ids,
        )
        works_by_key[key] = merged

    # Work 身份合并后必须再次按逻辑剧集键收拢，否则同一集的中英文发布包
    # 会留下两个 Episode，确认时既可能触发唯一约束，也会在详情页重复显示。
    episodes_by_identity: dict[tuple, ResolvedEpisode] = {}
    for episode in graph.episodes:
        work_key = target(episode.work_key)
        absolute_identity = (
            episode.absolute_episode_number
            if episode.local_episode_number is None
            else None
        )
        identity = (
            work_key,
            episode.local_season_number,
            episode.local_episode_number,
            absolute_identity,
            episode.special_number,
            episode.edition_key,
        )
        existing = episodes_by_identity.get(identity)
        if existing is None:
            episode_key = "|".join(
                str(value)
                for value in (
                    work_key,
                    episode.local_season_number,
                    episode.local_episode_number,
                    absolute_identity,
                    episode.special_number,
                )
            )
            episodes_by_identity[identity] = replace(
                episode,
                episode_key=episode_key,
                work_key=work_key,
            )
            continue
        episodes_by_identity[identity] = replace(
            existing,
            absolute_episode_number=(
                existing.absolute_episode_number
                if existing.absolute_episode_number is not None
                else episode.absolute_episode_number
            ),
            display_title=existing.display_title or episode.display_title,
            asset_evidence_ids=tuple(
                dict.fromkeys((*existing.asset_evidence_ids, *episode.asset_evidence_ids))
            ),
            provider_season_number=(
                existing.provider_season_number
                if existing.provider_season_number is not None
                else episode.provider_season_number
            ),
            provider_episode_number=(
                existing.provider_episode_number
                if existing.provider_episode_number is not None
                else episode.provider_episode_number
            ),
        )

    work_assets_by_identity: dict[tuple[str, str], ResolvedWorkAsset] = {}
    for asset in graph.work_assets:
        work_key = target(asset.work_key)
        identity = (work_key, asset.edition_key)
        existing = work_assets_by_identity.get(identity)
        if existing is None:
            work_assets_by_identity[identity] = replace(asset, work_key=work_key)
        else:
            work_assets_by_identity[identity] = replace(
                existing,
                asset_evidence_ids=tuple(
                    dict.fromkeys((*existing.asset_evidence_ids, *asset.asset_evidence_ids))
                ),
            )

    relations_by_identity = {}
    for relation in graph.relations:
        parent = target(relation.parent_work_key)
        child = target(relation.child_work_key)
        if parent == child:
            continue
        identity = (parent, child, relation.relation_type)
        relations_by_identity.setdefault(
            identity,
            replace(relation, parent_work_key=parent, child_work_key=child),
        )
    return replace(
        graph,
        works=tuple(works_by_key.values()),
        episodes=tuple(episodes_by_identity.values()),
        work_assets=tuple(work_assets_by_identity.values()),
        relations=tuple(relations_by_identity.values()),
    )


def default_candidate_search(
    work_key: str,
    queries: list[str],
    year: int | None,
    media_type: str,
    *,
    detail_cache: dict | None = None,
    detail_budget: list[int] | None = None,
) -> list[WorkCandidate]:
    """生产默认搜索：配置 Token 时用 TMDB，并按需补全可信别名。

    置信度由 plan_work_candidates 按标题/年份评分；别名来自详情接口
    （alternative_titles/translations），有共享缓存与预算上限。
    """

    from app.core.config import load_config
    from app.media_v4.jobs.metadata import enrich_candidate_aliases, search_tmdb_candidates

    config = load_config()
    if not config.tmdb_bearer_token:
        return []
    raw: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for query in queries:
        for item in search_tmdb_candidates(query, media_type, year) or []:
            key = (str(item.get("media_type") or media_type), str(item.get("provider_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            raw.append(item)
    enriched = enrich_candidate_aliases(
        raw,
        queries,
        max_details=8,
        detail_cache=detail_cache,
        detail_budget=detail_budget,
    )
    results: list[WorkCandidate] = []
    for item in enriched:
        results.append(WorkCandidate(
            work_key=work_key,
            provider="tmdb",
            provider_id=str(item.get("provider_id") or ""),
            media_type=item.get("media_type") or media_type,
            title=item.get("title") or item.get("name") or "",
            original_title=item.get("original_title") or item.get("original_name") or "",
            aliases=tuple(str(alias) for alias in (item.get("aliases") or []) if alias),
            year=item.get("year"),
            evidence="online_search",
            confidence="medium",
            status="proposed",
        ))
    return results
