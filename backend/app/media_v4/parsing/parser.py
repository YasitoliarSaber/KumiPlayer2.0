"""V4 纯媒体事实解析器。

现阶段复用项目已覆盖大量真实样本的识别规则，但只读取 ``MediaGuess``，
把它转换为 ParsedFacts；不读取/写入数据库，不生成 Work ID，也不淘汰重复文件。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.recognition.evidence import provider_to_source
from app.recognition.media import recognize_media

_SEASON_TOKEN = re.compile(r"(?i)(S\d{1,2})")
_EPISODE_TOKEN = re.compile(r"(?i)(E\d{1,3})(?:\s*[-~]\s*E?(\d{1,3}))?")
_ABSOLUTE_TOKEN = re.compile(r"(?i)(?:EP?\s*)?(\d{1,3})(?:\s*[-~]\s*(\d{1,3}))?")


def _unique_non_empty(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        value = (value or "").strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


class V4Parser:
    """从一个 SourceEvidence 生成一个不可变 ParsedFacts。"""

    VERSION = "v4-parser-1"

    def parse(
        self,
        evidence: SourceEvidence,
        *,
        existing_work_title: str = "",
        root_container: str = "",
    ) -> ParsedFacts:
        filename = PurePosixPath(evidence.relative_path).name
        source = provider_to_source(evidence.provider)
        guess = recognize_media(
            filename,
            evidence.relative_path,
            source=source,
            existing_work_title=existing_work_title,
            root_container=root_container,
        )

        season_match = _SEASON_TOKEN.search(filename)
        episode_match = _EPISODE_TOKEN.search(filename)
        season_token = season_match.group(1).upper() if season_match else ""
        episode_token = episode_match.group(0).upper().replace(" ", "") if episode_match else ""
        episode_range = None
        if episode_match and episode_match.group(2):
            episode_range = (int(episode_match.group(1)[1:]), int(episode_match.group(2)))

        absolute_candidate = guess.episode_number
        if not episode_match:
            absolute_match = _ABSOLUTE_TOKEN.search(filename)
            if absolute_match and absolute_match.group(1):
                absolute_candidate = int(absolute_match.group(1))

        group_type = guess.group_type or "unknown"
        is_auxiliary = group_type in {"auxiliary", "ignored"}
        title_candidates = _unique_non_empty(
            (guess.work_title, guess.series_group, guess.original_title)
        )
        return ParsedFacts(
            parsed_fact_id="facts_" + evidence.evidence_id,
            evidence_id=evidence.evidence_id,
            parser_version=self.VERSION,
            resource_type=evidence.entry_kind,
            media_type=guess.media_type,
            group_type=group_type,
            work_title=guess.work_title,
            original_title=guess.original_title,
            series_group=guess.series_group,
            card_type=guess.card_type,
            relation_type=guess.relation_type,
            show_type="",
            title_candidates=title_candidates,
            year_candidate=guess.year,
            season_token_raw=season_token,
            episode_token_raw=episode_token,
            season_candidate=guess.season_number,
            episode_candidate=guess.episode_number,
            absolute_episode_candidate=absolute_candidate,
            special_candidate=group_type == "special",
            episode_range=episode_range,
            special_number=guess.special_number,
            tmdb_hint_id=guess.tmdb_hint_id,
            tmdb_hint_type=guess.tmdb_hint_type,
            release_group="",
            edition_tags=(),
            quality_tags=(),
            confidence=guess.confidence,
            needs_review=guess.needs_review,
            is_importable=not is_auxiliary,
            is_auxiliary=is_auxiliary,
            reasons=tuple(guess.reasons),
            warnings=tuple(guess.warnings),
        )
