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
    SourceEvidence,
)


def _normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip(" ._-·:：/\\()（）【】[]{}<>《》「」『』\"'")


def _is_placeholder_title(value: str) -> bool:
    return _normalize_title(value) in {
        "",
        "unknown",
        "untitled",
        "n/a",
        "na",
        "none",
        "null",
    }


def _work_key(facts: ParsedFacts) -> str:
    if facts.tmdb_hint_id and facts.tmdb_hint_type:
        return f"provider:{facts.tmdb_hint_type.casefold()}:{facts.tmdb_hint_id}"
    title = _normalize_title(facts.series_group or facts.work_title or (facts.title_candidates or ("",))[0])
    if _is_placeholder_title(title):
        return ""
    media_type = (facts.media_type or facts.group_type or "unknown").casefold()
    year = str(facts.year_candidate or "")
    return f"title:{title}:{year}:{media_type}"


def _edition_key(facts: ParsedFacts) -> str:
    tags = sorted({_normalize_title(tag) for tag in facts.edition_tags if _normalize_title(tag)})
    return "+".join(tags) or "default"


class MediaResolver:
    """Resolver 只负责聚合，不修改输入事实、不淘汰 Asset。"""

    def resolve(self, entries: list[tuple[SourceEvidence, ParsedFacts]]) -> ResolvedMediaGraph:
        work_rows: OrderedDict[str, dict] = OrderedDict()
        episode_rows: OrderedDict[tuple, dict] = OrderedDict()
        issues: list[ResolutionIssue] = []

        for evidence, facts in entries:
            key = _work_key(facts)
            if not key:
                issues.append(
                    ResolutionIssue(
                        code="work_identity_missing",
                        evidence_id=evidence.evidence_id,
                        message="缺少足够的作品标题或 Provider 身份，需人工确认",
                    )
                )
                continue

            work = work_rows.setdefault(
                key,
                {
                    "title": facts.work_title or (facts.title_candidates or ("",))[0],
                    "year": facts.year_candidate,
                    "media_type": facts.media_type or "unknown",
                    "evidence_ids": [],
                },
            )
            if evidence.evidence_id not in work["evidence_ids"]:
                work["evidence_ids"].append(evidence.evidence_id)

            if facts.group_type == "special" or facts.special_candidate:
                local_season = 0
                local_episode = None
                special_number = facts.special_number or facts.episode_candidate
                season_kind = "special"
                episode_kind = "special"
            else:
                local_season = facts.season_candidate
                local_episode = facts.episode_candidate
                special_number = None
                season_kind = "regular"
                episode_kind = "regular" if facts.group_type == "season" else facts.group_type or "unknown"

            edition_key = _edition_key(facts)
            # A local SxxExx identity is authoritative for grouping.  Absolute
            # numbering is retained as a fact and cannot split the same local
            # episode; when no local number exists, absolute numbering is the
            # only available episode key and therefore remains in the key.
            absolute_group_key = facts.absolute_episode_candidate if local_episode is None else None
            episode_key = "|".join(
                (
                    key,
                    str(local_season),
                    str(local_episode),
                    str(absolute_group_key),
                    str(special_number),
                    edition_key,
                )
            )
            episode_identity = (key, local_season, local_episode, absolute_group_key, special_number, edition_key)
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
                },
            )
            if evidence.evidence_id not in episode["asset_ids"]:
                episode["asset_ids"].append(evidence.evidence_id)

        works = tuple(
            ResolvedWork(
                work_key=key,
                preferred_title=row["title"],
                year=row["year"],
                media_type=row["media_type"],
                source_evidence_ids=tuple(row["evidence_ids"]),
            )
            for key, row in work_rows.items()
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
        return ResolvedMediaGraph(works=works, episodes=episodes, issues=tuple(issues))
