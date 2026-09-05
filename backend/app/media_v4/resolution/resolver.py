"""把 ParsedFacts 批量收口为 Work/Season/Episode/Asset 图。"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict, defaultdict

from app.media_v4.domain.models import (
    ParsedFacts,
    ResolutionIssue,
    ResolvedEpisode,
    ResolvedMediaGraph,
    ResolvedWork,
    ResolvedWorkAsset,
    ResolvedWorkRelation,
    SourceEvidence,
)
from app.media_v4.generic_container import is_generic_container_title
from app.media_v4.parsing.episode_titles import ensure_special_title_number
from app.media_v4.sources.adapters import provider_to_source
from app.recognition.media import (
    _extract_work_container,
    _is_bracket_heavy,
    _is_series_container,
    _parse_work_title_and_year,
)


def _normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" ._-·:：/\\()（）【】[]{}<>《》「」『』\"'")


def _is_placeholder_title(value: str) -> bool:
    return is_generic_container_title(value)


def _effective_media_type(facts: ParsedFacts) -> str:
    """解析图的作品类型以显式 Provider 身份为准，不改写原始事实。

    目录里的 SP、PV、菜单只描述文件在本地的组织方式。若来源已经给出
    TMDB movie/tv hint，它才是 Work 的权威媒体类型；这能避免电影的花絮
    被聚合成 TV 特别篇。
    """

    hinted = facts.tmdb_hint_type.casefold()
    if facts.tmdb_hint_id and hinted in {"movie", "tv"}:
        return hinted
    return (facts.media_type or facts.group_type or "unknown").casefold()


def _main_series_identity_title(facts: ParsedFacts) -> str:
    """返回整批目录结构已经明确提供的主系列身份。

    旧版会先按作品边界聚合，再把季度/特别篇挂到边界代表的系列；V4
    逐文件事实仍保留了同一语义的 ``series_group``，Resolver 必须消费它，
    不能继续用每个子季度自己的 ``work_title + year`` 拆卡。独立电影和
    外传由 ``card_type=standalone`` 隔离，不会被父系列吸收。
    """

    if facts.card_type == "standalone":
        return ""
    series = (facts.series_group or "").strip()
    if not series or is_generic_container_title(series):
        return ""
    if facts.relation_type == "main":
        return series
    work = (facts.work_title or "").strip()
    if _normalize_title(series) != _normalize_title(work):
        return series
    original = facts.original_title or ""
    # 明确的多季度/合集容器即使第一季标题与系列名相同，也仍是系列身份。
    if re.search(
        r"(?i)(?:\.S\d+\s*[-~]\s*S\d+|\[S\d+\].*\[S\d+\]|\+(?:SP|OVA|OAD|剧场版|电影|特别篇)|(?:系列|合集|\bseries\b|\bcollection\b)\s*$)",
        original,
    ):
        return series
    return ""


def _boundary_work_key(evidence: SourceEvidence, facts: ParsedFacts) -> str:
    """恢复旧版 MediaUnit 的作品边界身份，但不生成最终 Work ID。

    边界只约束同一次来源树里的主系列条目；独立电影/外传不继承父边界。
    最终跨来源身份仍由 confirmed candidate/provider binding 负责。
    """

    if facts.card_type == "standalone":
        return ""
    source = provider_to_source(evidence.provider)
    container = _extract_work_container(evidence.relative_path, source)
    if not container or is_generic_container_title(container):
        return ""
    title, year = _parse_work_title_and_year(container)
    normalized = _normalize_title(title)
    if not normalized or _is_placeholder_title(normalized):
        return ""
    media_type = _effective_media_type(facts)
    resolved_series = _normalize_title(facts.series_group)
    if (
        facts.group_type == "special"
        and facts.card_type != "standalone"
        and resolved_series
        and not _is_placeholder_title(resolved_series)
        and resolved_series != normalized
    ):
        # 特别篇副标题可能成为独立目录名；当识别器已经通过通用副标题规则
        # 收口到主系列时，边界键必须消费该事实，不能再用原目录全名拆卡。
        return f"title:{resolved_series}:{facts.year_candidate or year or ''}:{media_type}"
    explicit_collection = bool(
        (_is_series_container(container) and not _is_bracket_heavy(container))
        or re.search(r"(?i)(?:系列|合集|\bseries\b|\bcollection\b)\s*$", container)
    )
    if explicit_collection:
        return f"series:{normalized}:{media_type}"
    return f"title:{normalized}:{year or facts.year_candidate or ''}:{media_type}"


def _work_key(facts: ParsedFacts, evidence: SourceEvidence | None = None) -> str:
    if facts.tmdb_hint_id and facts.tmdb_hint_type:
        return f"provider:{facts.tmdb_hint_type.casefold()}:{facts.tmdb_hint_id}"
    if evidence is not None:
        boundary_key = _boundary_work_key(evidence, facts)
        if boundary_key:
            return boundary_key
    series_title = _main_series_identity_title(facts)
    if series_title:
        return f"series:{_normalize_title(series_title)}:{_effective_media_type(facts)}"
    # 当前作品身份优先：独立/外传/电影子作品用 work_title；只有 work_title
    # 是通用容器/占位时才用稳定的 series_group 聚合（P-001 7.3.C）。
    identity_title = facts.work_title or (facts.title_candidates or ("",))[0]
    if is_generic_container_title(identity_title):
        for candidate in (facts.series_group, *facts.title_candidates):
            if candidate and not is_generic_container_title(candidate):
                identity_title = candidate
                break
    title = _normalize_title(identity_title)
    if _is_placeholder_title(title):
        return ""
    media_type = _effective_media_type(facts)
    year = str(facts.year_candidate or "")
    return f"title:{title}:{year}:{media_type}"


def _relation_work_key(facts: ParsedFacts) -> str:
    """由 series_group 推导父系列 Work key；与 _work_key 的 title 规则一致。"""

    if not facts.series_group or is_generic_container_title(facts.series_group):
        return ""
    media_type = (facts.media_type or facts.group_type or "unknown").casefold()
    return f"series:{_normalize_title(facts.series_group)}:{media_type}"


def _relation_work_key_from_row(row: dict) -> str:
    """由 work 行数据推导父系列 Work key，供 relations 构建使用。"""

    series_group = str(row.get("series_group") or "")
    if not series_group or is_generic_container_title(series_group):
        return ""
    # 子作品是电影时，父系列仍常是 TV；关系键保留目录解析到的父系列类型。
    media_type = str(row.get("relation_media_type") or row.get("media_type") or "unknown").casefold()
    return f"series:{_normalize_title(series_group)}:{media_type}"


def _edition_key(facts: ParsedFacts) -> str:
    tags = sorted({_normalize_title(tag) for tag in facts.edition_tags if _normalize_title(tag)})
    return "+".join(tags) or "default"


def _resolved_entry_work_key(
    evidence: SourceEvidence,
    facts: ParsedFacts,
    structural_series_identities: set[tuple[str, str]],
) -> str:
    key = _work_key(facts, evidence)
    if facts.card_type == "standalone":
        return key
    media_type = _effective_media_type(facts)
    # 结构折叠只接受作品名本身命中主系列身份。series_group 只是父系列
    # 线索：外传/电影条目的 series_group 指向主系列，若允许它参与折叠，
    # 外传会被主系列容器吞并（与主系列季集号冲突、共享错误 Logo），
    # 其与主系列的关系只能以 relation 表达，不能合并身份。
    matching_series = next(
        (
            normalized
            for normalized in (_normalize_title(facts.work_title),)
            if (normalized, media_type) in structural_series_identities
        ),
        "",
    )
    return f"series:{matching_series}:{media_type}" if matching_series else key


class MediaResolver:
    """Resolver 只负责聚合，不修改输入事实、不淘汰 Asset。"""

    def resolve(self, entries: list[tuple[SourceEvidence, ParsedFacts]]) -> ResolvedMediaGraph:
        work_rows: OrderedDict[str, dict] = OrderedDict()
        episode_rows: OrderedDict[tuple, dict] = OrderedDict()
        work_asset_rows: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        issues: list[ResolutionIssue] = []
        # 单文件解析只能看到当前路径；整批图谱则能看到同一目录边界中由
        # Season/Special 条目声明的主系列身份。只采用 relation_type=main
        # 的结构事实，外传/电影的父系列关系不能反向吞并当前作品。
        structural_series_identities = {
            (_normalize_title(facts.series_group), _effective_media_type(facts))
            for _evidence, facts in entries
            if facts.card_type != "standalone"
            and facts.relation_type == "main"
            and facts.series_group
            and not is_generic_container_title(facts.series_group)
        }
        explicit_special_numbers: dict[str, set[int]] = defaultdict(set)
        for evidence, facts in entries:
            if facts.group_type != "special" and not facts.special_candidate:
                continue
            key = _resolved_entry_work_key(evidence, facts, structural_series_identities)
            number = facts.special_number or facts.episode_candidate
            if key and number is not None and number > 0:
                explicit_special_numbers[key].add(number)
        next_special_number = {
            key: max(numbers, default=0) + 1
            for key, numbers in explicit_special_numbers.items()
        }
        allocated_unnumbered_specials: dict[tuple[str, str], int] = {}
        for evidence, facts in sorted(
            entries,
            key=lambda item: (item[0].relative_path.casefold(), item[0].evidence_id),
        ):
            if facts.group_type != "special" and not facts.special_candidate:
                continue
            if (facts.special_number or facts.episode_candidate or 0) > 0:
                continue
            key = _resolved_entry_work_key(evidence, facts, structural_series_identities)
            if not key:
                continue
            title_identity = _normalize_title(facts.episode_title) or _normalize_title(
                evidence.relative_path
            )
            allocation_key = (key, title_identity)
            if allocation_key in allocated_unnumbered_specials:
                continue
            special_number = next_special_number.get(key, 1)
            while special_number in explicit_special_numbers.get(key, set()):
                special_number += 1
            allocated_unnumbered_specials[allocation_key] = special_number
            explicit_special_numbers[key].add(special_number)
            next_special_number[key] = special_number + 1

        for evidence, facts in entries:
            if not facts.is_importable or facts.is_auxiliary:
                continue
            if facts.needs_review:
                issues.append(
                    ResolutionIssue(
                        code="parsed_facts_need_review",
                        evidence_id=evidence.evidence_id,
                        message="解析结果标记为需要人工复核，确认前必须处理",
                    )
                )
            identity_title = facts.work_title or (facts.title_candidates or ("",))[0]
            key = _resolved_entry_work_key(evidence, facts, structural_series_identities)
            if not key:
                generic = bool(identity_title.strip()) and is_generic_container_title(identity_title)
                issues.append(
                    ResolutionIssue(
                        code="generic_container_title" if generic else "work_identity_missing",
                        evidence_id=evidence.evidence_id,
                        message=(
                            "目录/文件只能提供通用容器标题（Season/S01/Specials/分类/纯数字），"
                            "无法确定作品身份，需人工确认"
                            if generic
                            else "缺少足够的作品标题或 Provider 身份，需人工确认"
                        ),
                    )
                )
                continue

            work = work_rows.setdefault(
                key,
                {
                    "title": identity_title,
                    "year": facts.year_candidate,
                    "media_type": _effective_media_type(facts),
                    "relation_media_type": (facts.media_type or facts.group_type or "unknown").casefold(),
                    "card_type": facts.card_type,
                    "show_type": facts.show_type,
                    "series_group": facts.series_group,
                    "relation_type": facts.relation_type,
                    "evidence_ids": [],
                },
            )
            if evidence.evidence_id not in work["evidence_ids"]:
                work["evidence_ids"].append(evidence.evidence_id)

            edition_key = _edition_key(facts)
            if _effective_media_type(facts) == "movie":
                movie_assets = work_asset_rows.setdefault((key, edition_key), [])
                if evidence.evidence_id not in movie_assets:
                    movie_assets.append(evidence.evidence_id)
                continue

            local_season: int | None
            local_episodes: tuple[int | None, ...]
            resolved_special_number: int | None
            if facts.group_type == "special" or facts.special_candidate:
                local_season = 0
                local_episodes = (None,)
                resolved_special_number = facts.special_number or facts.episode_candidate
                if resolved_special_number is None or resolved_special_number <= 0:
                    title_identity = _normalize_title(facts.episode_title) or _normalize_title(
                        evidence.relative_path
                    )
                    resolved_special_number = allocated_unnumbered_specials[(key, title_identity)]
                display_title = ensure_special_title_number(
                    facts.episode_title,
                    resolved_special_number,
                )
                season_kind = "special"
                episode_kind = "special"
            else:
                local_season = facts.season_candidate
                if facts.episode_range and facts.episode_range[0] <= facts.episode_range[1]:
                    local_episodes = tuple(range(facts.episode_range[0], facts.episode_range[1] + 1))
                else:
                    local_episodes = (facts.episode_candidate,)
                resolved_special_number = None
                display_title = facts.episode_title
                season_kind = "regular"
                episode_kind = "regular" if facts.group_type == "season" else facts.group_type or "unknown"

            for local_episode in local_episodes:
                # Local numbering is authoritative. Absolute numbering only
                # participates in identity when no local episode number exists.
                absolute_group_key = facts.absolute_episode_candidate if local_episode is None else None
                episode_key = "|".join(
                    (
                        key,
                        str(local_season),
                        str(local_episode),
                        str(absolute_group_key),
                        str(resolved_special_number),
                    )
                )
                episode_identity = (
                    key,
                    local_season,
                    local_episode,
                    absolute_group_key,
                    resolved_special_number,
                    edition_key,
                )
                existing_episode = episode_rows.get(episode_identity)
                if (
                    existing_episode is not None
                    and existing_episode["absolute_episode_number"] is not None
                    and facts.absolute_episode_candidate is not None
                    and existing_episode["absolute_episode_number"] != facts.absolute_episode_candidate
                ):
                    issues.append(
                        ResolutionIssue(
                            code="absolute_episode_conflict",
                            evidence_id=evidence.evidence_id,
                            message="同一 Local Episode 出现冲突的绝对集号，已保留为独立事实并需要复核",
                        )
                    )
                if (
                    existing_episode is not None
                    and existing_episode["absolute_episode_number"] is None
                    and facts.absolute_episode_candidate is not None
                ):
                    existing_episode["absolute_episode_number"] = facts.absolute_episode_candidate
                episode = episode_rows.setdefault(
                    episode_identity,
                    {
                        "episode_key": episode_key,
                        "work_key": key,
                        "local_season_number": local_season,
                        "local_episode_number": local_episode,
                        "absolute_episode_number": facts.absolute_episode_candidate,
                        "season_kind": season_kind,
                        "episode_kind": episode_kind,
                        "special_number": resolved_special_number,
                        "display_title": display_title,
                        "edition_key": edition_key,
                        "asset_ids": [],
                        "title_norms": {_normalize_title(identity_title)},
                        "has_provider_identity": bool(facts.tmdb_hint_id and facts.tmdb_hint_type),
                        "has_structural_series_identity": bool(_main_series_identity_title(facts)),
                        "has_boundary_identity": bool(_boundary_work_key(evidence, facts)),
                    },
                )
                # Episode → Asset 合并的 Work 身份防线：同一 Episode 若出现
                # 不同非通用作品标题且没有可信 provider 身份串接，必须形成
                # resolution issue，绝不能静默当成多版本（P-001 7.3.C）。
                title_norm = _normalize_title(identity_title)
                if (
                    title_norm
                    and title_norm not in episode["title_norms"]
                    and not (
                        episode["has_structural_series_identity"]
                        or episode["has_boundary_identity"]
                    )
                    and not (episode["has_provider_identity"] and bool(facts.tmdb_hint_id and facts.tmdb_hint_type))
                ):
                    episode["title_norms"].add(title_norm)
                    issues.append(
                        ResolutionIssue(
                            code="ambiguous_work_identity",
                            evidence_id=evidence.evidence_id,
                            message="同一集出现指向不同独立作品的 Asset，不能自动合并为多版本",
                        )
                    )
                elif title_norm:
                    episode["title_norms"].add(title_norm)
                if evidence.evidence_id not in episode["asset_ids"]:
                    episode["asset_ids"].append(evidence.evidence_id)

        works = tuple(
            ResolvedWork(
                work_key=key,
                preferred_title=row["title"],
                year=row["year"],
                media_type=row["media_type"],
                source_evidence_ids=tuple(row["evidence_ids"]),
                card_type=row["card_type"],
                show_type=row["show_type"],
                series_group=row["series_group"],
                relation_type=row["relation_type"],
            )
            for key, row in work_rows.items()
        )
        relations: list[ResolvedWorkRelation] = []
        for key, row in work_rows.items():
            parent_key = _relation_work_key_from_row(row)
            if not parent_key or parent_key == key:
                continue
            # 父 Work 可能只存在于已确认数据库；关系始终保留，由持久化层解析。
            relation_type = row["relation_type"] or "related"
            relations.append(
                ResolvedWorkRelation(
                    parent_work_key=parent_key,
                    child_work_key=key,
                    relation_type=relation_type,
                )
            )
        episodes = tuple(
            ResolvedEpisode(
                episode_key=row["episode_key"],
                work_key=row["work_key"],
                local_season_number=row["local_season_number"],
                local_episode_number=row["local_episode_number"],
                absolute_episode_number=row["absolute_episode_number"],
                season_kind=row["season_kind"],
                episode_kind=row["episode_kind"],
                special_number=row["special_number"],
                display_title=row["display_title"],
                edition_key=row["edition_key"],
                asset_evidence_ids=tuple(row["asset_ids"]),
            )
            for row in episode_rows.values()
        )
        work_assets = tuple(
            ResolvedWorkAsset(
                work_key=work_key,
                edition_key=edition_key,
                asset_evidence_ids=tuple(asset_ids),
            )
            for (work_key, edition_key), asset_ids in work_asset_rows.items()
        )
        return ResolvedMediaGraph(
            works=works,
            episodes=episodes,
            work_assets=work_assets,
            relations=tuple(relations),
            issues=tuple(issues),
        )
