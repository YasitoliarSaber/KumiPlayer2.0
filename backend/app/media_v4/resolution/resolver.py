"""把 ParsedFacts 批量收口为 Work/Season/Episode/Asset 图。"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict

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


def _work_key(facts: ParsedFacts) -> str:
    if facts.tmdb_hint_id and facts.tmdb_hint_type:
        return f"provider:{facts.tmdb_hint_type.casefold()}:{facts.tmdb_hint_id}"
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
    year = str(facts.year_candidate or "")
    return f"title:{_normalize_title(facts.series_group)}:{year}:{media_type}"


def _relation_work_key_from_row(row: dict) -> str:
    """由 work 行数据推导父系列 Work key，供 relations 构建使用。"""

    series_group = str(row.get("series_group") or "")
    if not series_group or is_generic_container_title(series_group):
        return ""
    # 子作品是电影时，父系列仍常是 TV；关系键保留目录解析到的父系列类型。
    media_type = str(row.get("relation_media_type") or row.get("media_type") or "unknown").casefold()
    year = str(row.get("year") or "")
    return f"title:{_normalize_title(series_group)}:{year}:{media_type}"


def _edition_key(facts: ParsedFacts) -> str:
    tags = sorted({_normalize_title(tag) for tag in facts.edition_tags if _normalize_title(tag)})
    return "+".join(tags) or "default"


class MediaResolver:
    """Resolver 只负责聚合，不修改输入事实、不淘汰 Asset。"""

    def resolve(self, entries: list[tuple[SourceEvidence, ParsedFacts]]) -> ResolvedMediaGraph:
        work_rows: OrderedDict[str, dict] = OrderedDict()
        episode_rows: OrderedDict[tuple, dict] = OrderedDict()
        work_asset_rows: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        issues: list[ResolutionIssue] = []

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
            key = _work_key(facts)
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
            if facts.group_type == "special" or facts.special_candidate:
                local_season = 0
                local_episodes = (None,)
                special_number = facts.special_number or facts.episode_candidate
                season_kind = "special"
                episode_kind = "special"
            else:
                local_season = facts.season_candidate
                if facts.episode_range and facts.episode_range[0] <= facts.episode_range[1]:
                    local_episodes = tuple(range(facts.episode_range[0], facts.episode_range[1] + 1))
                else:
                    local_episodes = (facts.episode_candidate,)
                special_number = None
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
                        str(special_number),
                    )
                )
                episode_identity = (
                    key,
                    local_season,
                    local_episode,
                    absolute_group_key,
                    special_number,
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
                        "special_number": special_number,
                        "edition_key": edition_key,
                        "asset_ids": [],
                        "title_norms": {_normalize_title(identity_title)},
                        "has_provider_identity": bool(facts.tmdb_hint_id and facts.tmdb_hint_type),
                    },
                )
                # Episode → Asset 合并的 Work 身份防线：同一 Episode 若出现
                # 不同非通用作品标题且没有可信 provider 身份串接，必须形成
                # resolution issue，绝不能静默当成多版本（P-001 7.3.C）。
                title_norm = _normalize_title(identity_title)
                if (
                    title_norm
                    and title_norm not in episode["title_norms"]
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
