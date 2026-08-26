"""V4 纯媒体事实解析器。

现阶段复用项目已覆盖大量真实样本的识别规则，但只读取 ``MediaGuess``，
把它转换为 ParsedFacts；不读取/写入数据库，不生成 Work ID，也不淘汰重复文件。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.generic_container import is_generic_container_name
from app.media_v4.sources.adapters import provider_to_source
from app.recognition.media import (
    _extract_work_container,
    _is_bracket_heavy,
    _is_generic_category_name,
    _is_group_folder,
    _is_series_container,
    _looks_like_plain_season_dir,
    _parse_work_title_and_year,
    recognize_media,
)

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


def _normalize_filename_stem(stem: str) -> str:
    """NFO/metadata 文件名 stem 规范化（去掉 tmdb 提示与发布标签）。"""

    value = re.sub(r"[\{【\[]\s*tmdb-?\s*\d+\s*[\}】\]]", "", stem, flags=re.IGNORECASE).strip()
    return re.sub(r"[\s._-]+", " ", value).strip()


def _normalized_container(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").casefold())


def _effective_root_container(
    relative_path: str,
    requested_root_container: str,
) -> str:
    """只在路径能证明它是作品边界时采用扫描器传入的容器名。

    ``root_container`` 历史上既被用于 MediaUnit 的作品边界，也曾被误传为
    整个扫描范围（根目录/新番/01动画）。后者若进入作品身份，会把整库误合
    成一张卡。这里保留单作品根、显式系列合集根和路径同名容器三种可证实情形。
    """

    root = (requested_root_container or "").strip()
    if not root or _is_generic_category_name(root):
        return ""
    parts = [part for part in PurePosixPath(relative_path).parts if part]
    directories = parts[:-1]
    normalized_root = _normalized_container(root)
    if normalized_root and any(
        _normalized_container(part) == normalized_root for part in directories
    ):
        return root
    if _is_series_container(root) and not _is_bracket_heavy(root):
        return root
    if not directories or _is_root_internal_structure(directories[0]):
        return root
    return ""


def _is_root_internal_structure(value: str) -> bool:
    """首段是否是单作品根内部结构，而不是媒体分类目录。"""

    normalized = (value or "").strip()
    if not normalized:
        return False
    return bool(
        _is_group_folder(normalized)
        or _looks_like_plain_season_dir(normalized)
        or re.fullmatch(r"\d{1,3}", normalized)
    )


def _structural_series_group(relative_path: str, source: str) -> str:
    """从完整路径恢复旧版 MediaUnit 的显式系列合集身份。"""

    container = _extract_work_container(relative_path, source)
    explicit_collection = bool(
        re.search(r"(?i)(?:系列|合集|\bseries\b|\bcollection\b)\s*$", container)
    )
    parts = [part for part in PurePosixPath(relative_path).parts if part]
    directories = parts[:-1]
    try:
        container_index = directories.index(container)
    except ValueError:
        child = ""
    else:
        child = directories[container_index + 1] if container_index + 1 < len(directories) else ""
    explicit_season_child = bool(
        child
        and (
            _is_root_internal_structure(child)
            or re.search(
                r"(?i)(?:\[S\d{1,2}(?:\.\d+)?\]|S\d{1,2}\s*$|Season\s*\d+|第\s*\d+\s*季)",
                child,
            )
        )
    )
    if (
        not container
        or _is_bracket_heavy(container)
        or not (_is_series_container(container) or explicit_collection or explicit_season_child)
    ):
        return ""
    title, _ = _parse_work_title_and_year(container)
    return title.strip()


_NFO_MAX_BYTES = 256 * 1024


def _parse_sidecar_nfo(evidence: SourceEvidence) -> tuple[int | None, str, str, str] | None:
    """有界、严格编码、只读解析可达 sidecar NFO，提取 tmdb uniqueid 与标题。

    文件不可达、超限、解码失败或 XML 解析失败时返回 None（仅标题证据降级）。
    """

    import xml.etree.ElementTree as ET

    locator = (evidence.playback_locator or evidence.source_locator or "").strip()
    if not locator or locator.startswith(("http://", "https://", "local://")):
        return None
    try:
        path = Path(locator)
        # SourceEvidence 的相对路径只能用于目录结构事实，绝不能相对当前
        # 工作目录读取任意文件；sidecar 内容只允许来自明确的绝对本地定位符。
        if not path.is_absolute():
            return None
        if not path.is_file():
            return None
        with open(path, "rb") as handle:
            raw = handle.read(_NFO_MAX_BYTES + 1)
        if len(raw) > _NFO_MAX_BYTES:
            return None
        try:
            root = ET.fromstring(raw)
        except (ET.ParseError, ValueError):
            # 尝试严格 UTF-16 BOM 后再次解析。
            try:
                root = ET.fromstring(raw.decode("utf-8").encode("utf-8"))
            except (ET.ParseError, ValueError, UnicodeDecodeError):
                return None
    except (OSError, ValueError):
        return None

    tmdb_id: int | None = None
    for unique in root.findall(".//uniqueid"):
        if (unique.get("type") or "").casefold() == "tmdb":
            try:
                value = int((unique.text or "").strip())
            except ValueError:
                tmdb_id = None
            else:
                tmdb_id = value if value > 0 else None
            if tmdb_id is not None:
                break
    if tmdb_id is None:
        tmdb_node = root.find(".//tmdbid")
        if tmdb_node is not None:
            tmdb_text = tmdb_node.text or ""
            if tmdb_text.strip().isdigit():
                value = int(tmdb_text.strip())
                tmdb_id = value if value > 0 else None

    def _text(tag: str) -> str:
        node = root.find(f".//{tag}")
        if node is None or node.text is None:
            return ""
        return node.text.strip()

    title = _text("title")
    original = _text("originaltitle")
    root_tag = str(root.tag).rsplit("}", 1)[-1].casefold()
    media_type = "movie" if root_tag == "movie" else "tv" if root_tag == "tvshow" else ""
    return tmdb_id, title, original, media_type


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
            parsed = _parse_sidecar_nfo(evidence)
            if parsed is None:
                # 不可达/不可读的 NFO：只保留标题证据，绝不伪造 provider ID。
                return ParsedFacts(
                    parsed_fact_id="facts_" + evidence.evidence_id,
                    evidence_id=evidence.evidence_id,
                    parser_version=self.VERSION,
                    resource_type="metadata",
                    title_candidates=(stem,) if stem else (),
                    is_importable=False,
                    is_auxiliary=True,
                )
            tmdb_id, title, original_title, tmdb_media_type = parsed
            return ParsedFacts(
                parsed_fact_id="facts_" + evidence.evidence_id,
                evidence_id=evidence.evidence_id,
                parser_version=self.VERSION,
                resource_type="metadata",
                work_title=title or stem,
                original_title=original_title or "",
                title_candidates=tuple(
                    dict.fromkeys(filter(None, (title or stem, original_title)))
                ) or (stem,),
                tmdb_hint_id=tmdb_id if tmdb_media_type else None,
                tmdb_hint_type=tmdb_media_type if tmdb_id else "",
                is_importable=False,
                is_auxiliary=True,
            )
        # 相对路径首层若是通用结构容器（Season 1 / S01 / Specials / 分类目录），
        # 它不是作品身份：解析时先剥离该段，让识别器回退到文件名系列名，
        # 避免把「Season 1」直接当成作品名（P-001 7.2.3/7.3.A）。
        parts = PurePosixPath(evidence.relative_path).parts
        parse_relative = evidence.relative_path
        existing_title = existing_work_title
        if parts and _is_root_internal_structure(parts[0]):
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
        effective_root_container = _effective_root_container(
            parse_relative,
            root_container,
        )
        guess = recognize_media(
            filename,
            parse_relative,
            source=source,
            existing_work_title=existing_title,
            root_container=effective_root_container,
            # verified_titles 是旧版“确认后学习结果”，不能反向成为 V4
            # 首次解析事实。V4 的可信身份由显式 hint、NFO、既有 binding
            # 与候选解析阶段统一处理。
            allow_verified_titles=False,
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
        structural_series_group = _structural_series_group(parse_relative, source)
        resolved_series_group = guess.series_group
        resolved_relation_type = guess.relation_type
        if structural_series_group and guess.card_type != "standalone":
            # 子季度可有独立本地标题（After Story / 第三季副标题），但作品归属
            # 必须服从路径已经明确声明的系列合集边界；电影/外传独立卡不吸收。
            resolved_series_group = structural_series_group
            resolved_relation_type = resolved_relation_type or "main"
        title_candidates = _unique_non_empty(
            (guess.work_title, resolved_series_group, guess.original_title)
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
            series_group=resolved_series_group,
            card_type=guess.card_type,
            relation_type=resolved_relation_type,
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
