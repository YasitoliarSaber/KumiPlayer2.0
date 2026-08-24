"""V4 纯媒体事实解析器。

现阶段复用项目已覆盖大量真实样本的识别规则，但只读取 ``MediaGuess``，
把它转换为 ParsedFacts；不读取/写入数据库，不生成 Work ID，也不淘汰重复文件。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.generic_container import is_generic_container_name
from app.media_v4.sources.adapters import provider_to_source
from app.recognition.media import recognize_media

_SEASON_TOKEN = re.compile(r"(?i)(S\d{1,2})")
_EPISODE_TOKEN = re.compile(r"(?i)(E\d{1,3})(?:\s*[-~]\s*E?(\d{1,3}))?")
_ABSOLUTE_TOKEN = re.compile(
    r"(?ix)(?:"
    r"(?<![a-z0-9])EP\s*(?P<ep>\d{1,3})(?!\d)"
    r"|[\[【(]\s*(?P<bracket>\d{1,3})\s*[\]】)]"
    r"|\s[-–—]\s*(?P<dash>\d{1,3})(?!\d)"
    r"|\s(?P<trailing>\d{1,3})\s*$"
    r")"
)
_QUALITY_TOKENS = re.compile(
    r"(?i)(?<![a-z0-9])(2160p|1080p|1080i|720p|4k|uhd|hdr10\+?|dolby[ ._-]?vision)(?![a-z0-9])"
)
_EDITION_TOKENS = (
    (re.compile(r"(?i)(?:director(?:'s)?[ ._-]?cut|导演剪辑版)"), "director-cut"),
    (re.compile(r"(?i)(?:extended(?:[ ._-]?edition)?|加长版)"), "extended"),
    (re.compile(r"(?i)(?:remaster(?:ed)?|重制版)"), "remastered"),
    (re.compile(r"(?i)(?:theatrical(?:[ ._-]?cut)?|院线版)"), "theatrical"),
)
_RELEASE_GROUP = re.compile(r"-([A-Za-z0-9][A-Za-z0-9_.-]{1,40})$")


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
        # 目录树 NFO 等 metadata 证据：只保留为只读候选身份证据，不参与
        # Work/Season/Episode 解析，不覆盖本地编号。
        if evidence.entry_kind == "metadata" or PurePosixPath(evidence.relative_path).suffix.casefold() == ".nfo":
            stem = PurePosixPath(evidence.relative_path).stem
            return ParsedFacts(
                parsed_fact_id="facts_" + evidence.evidence_id,
                evidence_id=evidence.evidence_id,
                parser_version=self.VERSION,
                resource_type="metadata",
                title_candidates=(stem,) if stem else (),
                is_importable=False,
                is_auxiliary=True,
            )
        # 相对路径首层若是通用结构容器（Season 1 / S01 / Specials / 分类目录），
        # 它不是作品身份：解析时先剥离该段，让识别器回退到文件名系列名，
        # 避免把「Season 1」直接当成作品名（P-001 7.2.3/7.3.A）。
        parts = PurePosixPath(evidence.relative_path).parts
        parse_relative = evidence.relative_path
        existing_title = existing_work_title
        if parts and is_generic_container_name(parts[0]):
            parse_relative = PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else ""
            if not existing_title:
                # 首层是通用容器时，从文件名提取稳定系列名作为权威作品名。
                from app.recognition.media import _extract_series_name_from_filename

                filename_title = _extract_series_name_from_filename(
                    PurePosixPath(parse_relative).name or PurePosixPath(evidence.relative_path).name
                )
                if filename_title and not is_generic_container_name(filename_title):
                    existing_title = filename_title
        filename = PurePosixPath(parse_relative).name
        source = provider_to_source(evidence.provider)
        guess = recognize_media(
            filename,
            parse_relative,
            source=source,
            existing_work_title=existing_title,
            root_container=root_container,
        )

        season_match = _SEASON_TOKEN.search(filename)
        episode_match = _EPISODE_TOKEN.search(filename)
        season_token = season_match.group(1).upper() if season_match else ""
        episode_token = episode_match.group(0).upper().replace(" ", "") if episode_match else ""
        episode_range = None
        if episode_match and episode_match.group(2):
            episode_range = (int(episode_match.group(1)[1:]), int(episode_match.group(2)))

        absolute_candidate = None
        if not episode_match:
            absolute_match = _ABSOLUTE_TOKEN.search(PurePosixPath(filename).stem)
            if absolute_match:
                raw_absolute = next(value for value in absolute_match.groupdict().values() if value)
                absolute_candidate = int(raw_absolute)
                episode_token = absolute_match.group(0).strip()

        group_type = guess.group_type or "unknown"
        is_auxiliary = group_type in {"auxiliary", "ignored"}
        stem = PurePosixPath(filename).stem
        quality_tags = tuple(
            dict.fromkeys(match.group(1).lower() for match in _QUALITY_TOKENS.finditer(stem))
        )
        edition_tags = tuple(tag for pattern, tag in _EDITION_TOKENS if pattern.search(stem))
        release_match = _RELEASE_GROUP.search(stem)
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
            release_group=release_match.group(1) if release_match else "",
            edition_tags=edition_tags,
            quality_tags=quality_tags,
            confidence=guess.confidence,
            needs_review=guess.needs_review,
            is_importable=not is_auxiliary,
            is_auxiliary=is_auxiliary,
            reasons=tuple(guess.reasons),
            warnings=tuple(guess.warnings),
        )
