"""C-002/C-004 单文件证据仲裁：内容分类、标题、结构化提示与编号保真。

这里是**唯一的**词法词表与仲裁入口：``recognition/media.py`` 继续提供成熟标题
清洗与发布参数词法，但 V4 的 ``ParsedFacts`` 由本模块的判定收口，不再保留
"按 provider 选容器层级"或"按批量成员数改写编号"的隐藏分支。

规则要点：

- 特别篇/附属内容只按**整段类别 token**或**明确的文件级标记**排除；
  ``OV A`` 这类发行形式、``SPY×FAMILY``、``Happy Ending``、``H264.50fps``
  都不构成排除依据。
- 独立 OVA/OAD、电影、正片外传不被发行形式排除；只有附属关系被明确声明时才排除。
- 季/集/绝对编号各自记录来源；缺证据用 null + unknown，绝不用 0 兜底，
  也绝不取"当前最小集号"重排。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.media_v4.domain.identity import (
    CLASSIFICATION_CONFLICT,
    CLASSIFICATION_EXPLICIT,
    CLASSIFICATION_INFERRED,
    CLASSIFICATION_UNKNOWN,
    CONTENT_CLASS_ATTACHED_SPECIAL,
    CONTENT_CLASS_AUXILIARY,
    CONTENT_CLASS_MOVIE,
    CONTENT_CLASS_REGULAR,
    CONTENT_CLASS_STANDALONE,
    CONTENT_CLASS_UNKNOWN,
    MEDIA_TYPE_MOVIE,
    MEDIA_TYPE_TV,
    MEDIA_TYPE_UNKNOWN,
    NON_IMPORTABLE_CONTENT_CLASSES,
    ORIGIN_DIRECTORY,
    ORIGIN_EXPLICIT_ABSOLUTE,
    ORIGIN_EXPLICIT_DIRECTORY,
    ORIGIN_EXPLICIT_FILENAME,
    ORIGIN_FILENAME,
    ORIGIN_LOCAL_UNSCOPED,
    ORIGIN_PARSER_RULE,
    ORIGIN_STRUCTURED_HINT,
    ORIGIN_UNKNOWN,
    DecisionTrace,
    NumberingEvidence,
)

# --- 词法词表 ---------------------------------------------------------------

#: 技术参数区：这些 token 不参与标题、类别与编号判断（`264.50fps` 不能变成集号）。
_TECHNICAL_TOKEN_RE = re.compile(
    r"(?i)^(?:"
    r"\d{3,4}[pi]|2160p|1080p|720p|480p|4k|uhd|8k|hdr10\+?|hdr|dv|dolby[ ._-]?vision|"
    r"x26[45]|h26[45]|hevc|avc|vp9|av1|ma10p|hi10p|10bit|8bit|"
    r"flac|aac|ac3|eac3|dts(?:-hd)?|truehd|opus|mp3|"
    r"(?:h|a)?264\.\d+fps|\d+(?:\.\d+)?fps|\d+bit|"
    r"5\.1|7\.1|2\.0|stereo|dual[ ._-]?audio|"
    r"chs|cht|jpsc|jpn|eng|sc|tc|gb|big5|简|繁|简体|繁体|简繁|内封|内嵌|外挂|中字|字幕|"
    r"web[ ._-]?dl|webrip|web|bdrip|bd|bluray|blu-ray|dvdrip|hdtv|remux|"
    r"mkv|mp4|avi|mov|ts|m2ts|ass|srt|sub|"
    r"repack|proper|v\d|rev\d"
    r")$"
)

#: 附属内容文件级标记（有边界的 OP/ED/PV/CM/MENU 等）。这些**不是** TMDB Special。
#: 只保留有边界、有编号或有括号前缀的形态：旧词表里的裸 `OPENING` / `ENDING`
#: 会误伤正片标题（实测 ``Happy Ending.S01E01.mkv``），属于 F-007 的根因。
_AUXILIARY_FILE_RES = (
    re.compile(r"(?i)(?<![A-Za-z])NCOP\d*(?![A-Za-z])"),
    re.compile(r"(?i)(?<![A-Za-z])NCED\d*(?![A-Za-z])"),
    re.compile(r"(?i)(?<![A-Za-z])NON[-\s]?CREDIT\s*(?:OP|ED)"),
    re.compile(r"(?i)\[[^\]\[]*\b(?:OP|ED)\s*\d*\b[^\]\[]*\]"),
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:OP|ED)\d*(?=$|[^A-Za-z0-9])"),
    re.compile(r"(?i)(?<![A-Za-z])(?:PV|CM|MV|MENU|TRAILER|TEASER|EYECATCH|PREVIEW)\s*\d*(?=$|[^A-Za-z])"),
    re.compile(r"(?i)\bTV\s*SPOT\b"),
    re.compile(r"(?i)\b(?:NON[-\s]?TELOP|CREDITLESS|TEXTLESS)\b"),
    re.compile(r"菜单|预告|花絮|无字幕\s*(?:OP|ED)"),
    # 光盘 SPs/Extras 里混放的制作素材：文件名语义比父目录 "SPs" 更强。
    re.compile(r"\b(?:OPED|BGM)\s+RECORDING\s*\d*\b", re.IGNORECASE),
    re.compile(r"\bLOCATION\s+HUNTING\s*\d*\b", re.IGNORECASE),
    re.compile(r"[\[\u3010]\s*EVENT\s*\d+\s*[\]\u3011]", re.IGNORECASE),
    re.compile(r"[\[\u3010][^\]\u3010]*\bNC\s+VER\.?[^\]\u3010]*[\]\u3011]", re.IGNORECASE),
    re.compile(r"\bPREVIEW\s+COLLECTION\b", re.IGNORECASE),
    re.compile(r"[\[\u3010]\s*(?:DIGEST|PROGRAM|MOVIE\s+MANNER)\s*[\]\u3011]", re.IGNORECASE),
    re.compile(r"\bANNOUNCEMENT\b", re.IGNORECASE),
    re.compile(r"[\[\u3010][^\]\u3010]*\b(?:MUSIC\s+)?CONCERT\b[^\]\u3010]*[\]\u3011]", re.IGNORECASE),
    re.compile(r"[\[\u3010]\s*(?:TEASER|CM)(?:\s+COLLECTION)?\s*\d*\s*[\]\u3011]", re.IGNORECASE),
    re.compile(r"[\[\u3010]\s*(?:MENU|MV)\d*\s*[\]\u3011]", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z])MENU\d+", re.IGNORECASE),
)

#: 附属内容的**整段目录名**类别（精确整段匹配，不做前缀/子串）。
_AUXILIARY_DIR_TOKENS = frozenset(
    {
        "op", "ed", "op&ed", "op＆ed", "oped", "op_ed",
        "pv", "pvs", "cm", "cms", "mv", "mvs", "mv合集", "menu", "menus",
        "trailer", "trailers", "eyecatch", "preview", "previews",
        "ncop", "nced", "预告", "花絮", "菜单",
    }
)

#: 附属字幕/番外的**整段目录名**类别：这些才是"明确声明的附属关系"。
_ATTACHED_DIR_TOKENS = frozenset(
    {
        "特别篇", "特別篇", "特典", "特典映像", "映像特典", "番外", "番外篇", "短片", "短篇集",
        "special", "specials", "sps", "sp", "extra", "extras", "bonus",
        "oad", "oads", "ova", "ovas",
    }
)

#: 文件级"明确附属关系"标记。
_ATTACHED_FILE_RES = (
    re.compile(r"(?i)[\[\u3010]\s*(?:SP|LITE)\s*\d*\s*[\]\u3011]"),
    re.compile(r"(?i)(?<![A-Za-z])SP\d+(?:$|[^A-Za-z])"),
    re.compile(r"(?i)\bS00(?:E\d+)?\b"),
    re.compile(r"\u6620\u50cf\u7279\u5178|\u7279\u5178"),
    re.compile(r"(?<![A-Za-z])\u756a\u5916"),
    re.compile(r"\u5c0f\u5267\u573a|(?<![A-Za-z])\u77ed\u7bc7(?![A-Za-z])"),
)

#: 发行形式（不构成排除依据）：单独出现时保留可播放身份。
_RELEASE_FORM_RE = re.compile(r"(?i)(?<![A-Za-z])(?:OVA|OAD)(?![A-Za-z])")

#: 电影证据。
_MOVIE_FILE_RES = (
    re.compile(r"剧场版|劇場版|映画|总集篇|總集篇"),
    re.compile(r"(?i)(?<![A-Za-z])MOVIE(?![A-Za-z])"),
    re.compile(r"(?i)(?<![A-Za-z])(?:THE\s+)?MOVIE\b"),
)
_MOVIE_DIR_TOKENS = frozenset({"剧场版", "劇場版", "映画", "动画电影", "电影", "movie", "movies", "总集篇", "總集篇"})

#: 独立作品/外传证据。
_STANDALONE_FILE_RE = re.compile(r"(?i)外传|外傳|(?<![A-Za-z])SPIN[-\s]?OFF(?![A-Za-z])")
_STANDALONE_DIR_TOKENS = frozenset({"外传", "外傳", "spin-off", "spin off", "独立作品"})

_SEASON_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z])(?:S|SEASON\s*)(\d{1,2})(?![A-Za-z0-9])")
_SEASON_EPISODE_RE = re.compile(r"(?i)(?<![A-Za-z])S(\d{1,2})\s*E(\d{1,3})(?!\d)")
_EPISODE_ONLY_RE = re.compile(r"(?i)(?<![A-Za-z0-9])E(?:P)?(\d{1,3})(?!\d)")
_CJK_EPISODE_RE = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
_CJK_SEASON_RE = re.compile(r"第\s*(\d{1,2})\s*季")
_ABS_MARKER_RE = re.compile(r"(?i)(?<![A-Za-z])(?:ABS|ABSOLUTE)(\d{1,4})(?!\d)")
_BARE_NUMBER_RES = (
    # 完整编号位置的 [13] / 【13】 / (13)
    re.compile(r"[\[【(]\s*(\d{1,3})\s*[\]】)]"),
    # `part-13` / `part13` / `ep13`
    re.compile(r"(?i)(?:^|[\s._-])(?:part|ep|episode)[\s._-]*(\d{1,3})(?!\d)"),
    # `- 13` / `~ 13` / 尾随 ` 13`；必须真的由分隔符引出，
    # 避免 `86-不存在的战区-` 这类作品名里的数字被当成集号。
    re.compile(r"(?<=[\s._~-])(\d{1,3})(?=$|[\s._~]|\[|【|\.)"),
)
_RANGE_RE = re.compile(r"(?i)(?<![A-Za-z])E(\d{1,3})\s*[-~]\s*E?(\d{1,3})(?![\w%])")

_MAX_RANGE_LENGTH = 1000


@dataclass(frozen=True, slots=True)
class FilenameTokens:
    """文件名词法：原 token、语义 token、技术参数 token。"""

    stem: str
    raw_tokens: tuple[str, ...] = ()
    semantic_tokens: tuple[str, ...] = ()
    technical_tokens: tuple[str, ...] = ()
    bracket_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DirectoryToken:
    """目录段与其相对文件的深度（0 = 最靠近文件）。"""

    name: str
    depth: int
    semantic: bool = True


@dataclass(frozen=True, slots=True)
class NumberingFacts:
    season: int | None = None
    episode: int | None = None
    absolute: int | None = None
    episode_range: tuple[int, int] | None = None
    episode_origin: str = ORIGIN_UNKNOWN
    conflicts: tuple[str, ...] = ()
    numbering: NumberingEvidence = field(default_factory=NumberingEvidence)


@dataclass(frozen=True, slots=True)
class ContentClassification:
    content_class: str = CONTENT_CLASS_UNKNOWN
    classification_state: str = CLASSIFICATION_UNKNOWN
    media_type: str = MEDIA_TYPE_UNKNOWN
    is_importable: bool = True
    is_auxiliary: bool = False
    reasons: tuple[str, ...] = ()
    traces: tuple[DecisionTrace, ...] = ()


@dataclass(frozen=True, slots=True)
class HintDecision:
    tmdb_id: int | None = None
    tmdb_type: str = ""
    conflict: bool = False
    invalid: bool = False
    reasons: tuple[str, ...] = ()
    traces: tuple[DecisionTrace, ...] = ()


@dataclass(frozen=True, slots=True)
class TitleDecision:
    work_title: str = ""
    original_title: str = ""
    series_group: str = ""
    only_from_directory_hint: bool = False
    traces: tuple[DecisionTrace, ...] = ()


# --- token 化 ---------------------------------------------------------------


def tokenize_filename(relative_path: str) -> FilenameTokens:
    """把文件名拆成原 token、语义 token 与技术参数 token；保留原 token 顺序。"""

    base = (relative_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    bracket_tokens = tuple(
        token.strip()
        for token in re.findall(r"[\[【]([^\]】]*)[\]】]", stem)
        if token.strip()
    )
    raw_tokens = tuple(token for token in re.split(r"[\s._\-]+", stem) if token)
    technical: list[str] = []
    semantic: list[str] = []
    for token in raw_tokens:
        if _TECHNICAL_TOKEN_RE.match(token):
            technical.append(token)
        else:
            semantic.append(token)
    return FilenameTokens(
        stem=stem,
        raw_tokens=raw_tokens,
        semantic_tokens=tuple(semantic),
        technical_tokens=tuple(technical),
        bracket_tokens=bracket_tokens,
    )


_STRUCTURAL_DIR_RE = re.compile(
    r"(?i)^(?:s\d{1,2}|season\s*\d{1,2}|第\s*\d{1,2}\s*季|specials?|sp|sps|"
    r"特别篇|特別篇|特典|映像特典|番外|番外篇|短片|ova|oad|oads|op&ed|oped)$"
)


def collect_nearest_semantic_directory_tokens(relative_path: str) -> tuple[DirectoryToken, ...]:
    """由近到远返回目录段；结构目录标记 semantic=False，调用方按需跳过。"""

    parts = [part for part in (relative_path or "").replace("\\", "/").split("/")[:-1] if part]
    result: list[DirectoryToken] = []
    for depth, name in enumerate(reversed(parts)):
        cleaned = _normalize_segment(name)
        result.append(
            DirectoryToken(
                name=name,
                depth=depth,
                semantic=not _STRUCTURAL_DIR_RE.match(cleaned),
            )
        )
    return tuple(result)


def _normalize_segment(value: str) -> str:
    return re.sub(r"[\s._\-·:：/\\()（）【】\[\]]+", "", value or "").casefold()


def _segment_token(value: str) -> str:
    """目录段的比较键：去括号/分隔符后小写，`[OVA]` 与 `OVA` 视为同类。"""

    return _normalize_segment(value).replace("＆", "&")


def _nearest_semantic_directory(directory: tuple[DirectoryToken, ...]) -> DirectoryToken | None:
    for token in directory:
        if token.semantic:
            return token
    return None


def _first_semantic_token(tokens: FilenameTokens) -> str:
    return tokens.semantic_tokens[0] if tokens.semantic_tokens else tokens.stem


# --- 编号（C-004） ----------------------------------------------------------


def parse_numbering(
    tokens: FilenameTokens,
    directory: tuple[DirectoryToken, ...] = (),
    *,
    cjk_episode: int | None = None,
    provisional_season: int | None = None,
) -> NumberingFacts:
    """按固定词法赋值季/集/绝对编号；目录只在文件缺季号时补季。

    ``cjk_episode`` 由调用方已有的成熟中文集号识别器提供（涵盖 `第062第` 这类
    手误形态），保证中文编号只有一份实现。
    """

    stem = tokens.stem
    season: int | None = None
    episode: int | None = None
    absolute: int | None = None
    episode_range: tuple[int, int] | None = None
    conflicts: list[str] = []
    season_origin = ORIGIN_UNKNOWN
    episode_origin = ORIGIN_UNKNOWN
    absolute_origin = ORIGIN_UNKNOWN

    season_episode = _SEASON_EPISODE_RE.search(stem)
    if season_episode is not None:
        season = int(season_episode.group(1))
        episode = int(season_episode.group(2))
        season_origin = ORIGIN_EXPLICIT_FILENAME
        episode_origin = ORIGIN_EXPLICIT_FILENAME
    else:
        cjk_season = _CJK_SEASON_RE.search(stem)
        if cjk_season is not None:
            season = int(cjk_season.group(1))
            season_origin = ORIGIN_EXPLICIT_FILENAME
        cjk_episode_match = _CJK_EPISODE_RE.search(stem)
        if cjk_episode_match is not None:
            episode = int(cjk_episode_match.group(1))
            episode_origin = ORIGIN_EXPLICIT_FILENAME
        elif cjk_episode is not None:
            episode = int(cjk_episode)
            episode_origin = ORIGIN_EXPLICIT_FILENAME
        else:
            episode_only = _EPISODE_ONLY_RE.search(stem)
            if episode_only is not None and (season is not None or _has_e_token(stem)):
                episode = int(episode_only.group(1))
                episode_origin = ORIGIN_EXPLICIT_FILENAME
            elif season is None:
                absolute_marker = _ABS_MARKER_RE.search(stem)
                if absolute_marker is not None:
                    absolute = int(absolute_marker.group(1))
                    absolute_origin = ORIGIN_EXPLICIT_ABSOLUTE
                else:
                    bare = _bare_number(tokens)
                    if bare is not None:
                        episode = bare
                        episode_origin = ORIGIN_LOCAL_UNSCOPED

    explicit_season = _SEASON_TOKEN_RE.search(stem)
    if explicit_season is not None:
        value = int(explicit_season.group(1))
        if season is None:
            season = value
            season_origin = ORIGIN_EXPLICIT_FILENAME
        elif season != value:
            conflicts.append("season_evidence_conflict")

    range_match = _RANGE_RE.search(stem)
    if range_match is not None:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if end < start or (end - start) > _MAX_RANGE_LENGTH:
            conflicts.append("episode_range_invalid")
        else:
            episode_range = (start, end)
            if episode is None:
                episode = start
                episode_origin = ORIGIN_EXPLICIT_FILENAME

    directory_season = _explicit_season_from_directory(directory)
    if directory_season is not None:
        if season is None:
            season = directory_season
            season_origin = ORIGIN_EXPLICIT_DIRECTORY
        elif season != directory_season:
            # 文件级显式编号胜出，目录不得在批量阶段二次覆盖。
            conflicts.append("season_evidence_conflict")

    if season is None and provisional_season is not None:
        # 既有识别器的父目录季号启发式是成熟能力，只在没有更强证据时兜底；
        # 文件级显式季号与目录显式季号仍然优先。
        season = int(provisional_season)
        season_origin = ORIGIN_PARSER_RULE

    scope_key = "work" if absolute_origin == ORIGIN_EXPLICIT_ABSOLUTE else "local"
    evidence = NumberingEvidence(
        season_origin=season_origin,
        episode_origin=episode_origin,
        absolute_origin=absolute_origin,
        scope_key=scope_key,
        basis=tuple(sorted(set(conflicts))),
    )
    return NumberingFacts(
        season=season,
        episode=episode,
        absolute=absolute,
        episode_range=episode_range,
        episode_origin=episode_origin,
        conflicts=tuple(conflicts),
        numbering=evidence,
    )


def _has_e_token(stem: str) -> bool:
    return bool(re.search(r"(?i)(?<![A-Za-z0-9])E", stem))


_CJK_SEASON_NUMERALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}

_DIR_SEASON_RES = (
    re.compile(r"\[\s*S0?(\d{1,2})(?:\.\d+)?\s*\]", re.IGNORECASE),
    re.compile(r"(?:^|[\s._-])Season\s*0?(\d{1,2})(?:[\s._-]|$)", re.IGNORECASE),
    re.compile(r"第\s*(一|二|两|三|四|五|六|七|八|九|十|十一|十二|\d{1,2})\s*季"),
    re.compile(r"(?:^|[\s._-])S0?(\d{1,2})(?:[\s._-]|$)", re.IGNORECASE),
)


def _season_number_from_match(value: str) -> int | None:
    text = (value or "").strip()
    if text.isdigit():
        return int(text)
    return _CJK_SEASON_NUMERALS.get(text)


def _explicit_season_from_directory(directory: tuple[DirectoryToken, ...]) -> int | None:
    """从最近的目录段读取明确季号；`[S1]`、`Season 2`、`第1季`、`第二季` 都算显式。"""

    for token in directory:
        for pattern in _DIR_SEASON_RES:
            match = pattern.search(token.name)
            if match is not None:
                value = _season_number_from_match(match.group(1))
                if value is not None:
                    return value
    return None


def _bare_number(tokens: FilenameTokens) -> int | None:
    """只在"完整编号位置"取裸编号：`[13]`、`- 13`、`part-13`。"""

    stem = tokens.stem
    candidate: int | None = None
    for pattern in _BARE_NUMBER_RES:
        match = pattern.search(stem)
        if match is not None:
            candidate = int(match.group(1))
            break
    if candidate is None:
        return None
    # 年份/分辨率/版本号不能进入编号字段。
    if candidate > 1900 and candidate < 2100:
        return None
    return candidate


# --- 分类（C-002） ----------------------------------------------------------


def classify_content(
    tokens: FilenameTokens,
    directory: tuple[DirectoryToken, ...] = (),
    numbering: NumberingFacts | None = None,
    *,
    provisional_movie: bool = False,
    provisional_special: bool = False,
) -> ContentClassification:
    """按 C-002 的优先级给出内容类别、媒体类型与准入判定。

    ``provisional_movie`` / ``provisional_special`` 传入既有识别器已经给出明确
    证据的判定（电影关键词与分类目录、已核验特别篇副标题、SPS 目录等）。
    它们不是"没有集号就当电影/特别篇"的兜底：单发 OVA/OAD 发行形式仍然保留为
    unknown 可播放内容，不被反向授权排除。
    """

    numbering = numbering or NumberingFacts()
    stem = tokens.stem
    reasons: list[str] = []
    traces: list[DecisionTrace] = []

    auxiliary_hit = next((p.pattern for p in _AUXILIARY_FILE_RES if p.search(stem)), "")
    auxiliary_dir = next(
        (token for token in directory if _segment_token(token.name) in _AUXILIARY_DIR_TOKENS),
        None,
    )
    attached_hit = next((p.pattern for p in _ATTACHED_FILE_RES if p.search(stem)), "")
    attached_dir = next(
        (token for token in directory if _segment_token(token.name) in _ATTACHED_DIR_TOKENS),
        None,
    )
    movie_hit = next((p.pattern for p in _MOVIE_FILE_RES if p.search(stem)), "")
    movie_dir = next(
        (token for token in directory if _segment_token(token.name) in _MOVIE_DIR_TOKENS),
        None,
    )
    standalone_hit = _STANDALONE_FILE_RE.search(stem)
    standalone_dir = next(
        (token for token in directory if _segment_token(token.name) in _STANDALONE_DIR_TOKENS),
        None,
    )
    release_form = _RELEASE_FORM_RE.search(stem)
    # 单发 OVA/OAD 发行形式不构成附属声明；但若文件名里还带了别的附属标记
    # （已在上面的 attached_hit 里），仍然按附属内容处理。
    modified_before_attached = bool(attached_hit) or attached_dir is not None
    has_numbering = numbering.episode is not None or numbering.absolute is not None

    def trace(value: str, origin: str, rule_id: str, alternatives: tuple[str, ...] = ()) -> None:
        traces.append(
            DecisionTrace(
                field="content_class",
                value=value,
                origin=origin,
                scope="file",
                rule_id=rule_id,
                alternatives=alternatives,
            )
        )

    # 1. 附属内容（OP/ED/PV/CM/MENU…）：明确附属关系，不生成媒体实体。
    if auxiliary_hit or auxiliary_dir is not None:
        origin = ORIGIN_FILENAME if auxiliary_hit else ORIGIN_DIRECTORY
        reasons.append("附属内容（OP/ED/PV/CM/MENU 等）不进入媒体库")
        trace(CONTENT_CLASS_AUXILIARY, origin, "auxiliary_marker")
        return ContentClassification(
            content_class=CONTENT_CLASS_AUXILIARY,
            classification_state=CLASSIFICATION_EXPLICIT,
            media_type=MEDIA_TYPE_TV,
            is_importable=False,
            is_auxiliary=True,
            reasons=tuple(reasons),
            traces=tuple(traces),
        )

    # 2. 明确附属关系（SP/S00/映像特典，或整段附属目录名）。
    release_form_only = release_form is not None and not modified_before_attached
    if attached_hit or attached_dir is not None or (provisional_special and not release_form_only):
        origin = (
            ORIGIN_FILENAME
            if attached_hit
            else ORIGIN_DIRECTORY
            if attached_dir is not None
            else ORIGIN_PARSER_RULE
        )
        state = CLASSIFICATION_EXPLICIT
        if has_numbering and numbering.episode is not None:
            # 文件写 S01E01 但附属关系明确：附属证据胜出，并记录冲突。
            reasons.append("classification_conflict：文件集号与明确附属关系冲突，按附属关系排除")
            numbering.conflicts  # noqa: B018 - 冲突已在 numbering 中记录
            state = CLASSIFICATION_CONFLICT
        else:
            reasons.append("明确附属关系（特别篇/特典/SP/S00）不生成媒体实体")
        trace(CONTENT_CLASS_ATTACHED_SPECIAL, origin, "attached_special_marker")
        return ContentClassification(
            content_class=CONTENT_CLASS_ATTACHED_SPECIAL,
            classification_state=state,
            media_type=MEDIA_TYPE_TV,
            is_importable=False,
            is_auxiliary=True,
            reasons=tuple(reasons),
            traces=tuple(traces),
        )

    # 3. 电影证据：显式电影/剧场版/映画/电影分类目录，或既有识别器的电影判定。
    if movie_hit or movie_dir is not None or provisional_movie:
        origin = (
            ORIGIN_FILENAME
            if movie_hit
            else ORIGIN_DIRECTORY
            if movie_dir is not None
            else ORIGIN_PARSER_RULE
        )
        reasons.append("存在显式电影证据")
        trace(CONTENT_CLASS_MOVIE, origin, "movie_evidence")
        return ContentClassification(
            content_class=CONTENT_CLASS_MOVIE,
            classification_state=CLASSIFICATION_EXPLICIT,
            media_type=MEDIA_TYPE_MOVIE,
            is_importable=True,
            is_auxiliary=False,
            reasons=tuple(reasons),
            traces=tuple(traces),
        )

    # 4. 独立作品/外传：发行形式不排除，保留独立可播放身份。
    if standalone_hit or standalone_dir is not None:
        origin = ORIGIN_FILENAME if standalone_hit else ORIGIN_DIRECTORY
        reasons.append("独立作品/外传保留为独立可播放作品")
        trace(CONTENT_CLASS_STANDALONE, origin, "standalone_evidence")
        return ContentClassification(
            content_class=CONTENT_CLASS_STANDALONE,
            classification_state=CLASSIFICATION_EXPLICIT,
            media_type=MEDIA_TYPE_TV if has_numbering else MEDIA_TYPE_UNKNOWN,
            is_importable=True,
            is_auxiliary=False,
            reasons=tuple(reasons),
            traces=tuple(traces),
        )

    # 5. 有明确集/绝对编号或**显式**季号：正片（显式电影证据已在上面返回）。
    #    识别器父目录启发式给的季号（parser_rule）不足以单独把文件升为 tv。
    explicit_season = numbering.numbering.season_origin in {
        ORIGIN_EXPLICIT_FILENAME,
        ORIGIN_EXPLICIT_DIRECTORY,
    }
    if has_numbering or (numbering.season is not None and explicit_season):
        reasons.append("存在明确季/集编号，作为正片导入")
        trace(CONTENT_CLASS_REGULAR, ORIGIN_FILENAME, "episode_numbering")
        return ContentClassification(
            content_class=CONTENT_CLASS_REGULAR,
            classification_state=CLASSIFICATION_EXPLICIT,
            media_type=MEDIA_TYPE_TV,
            is_importable=True,
            is_auxiliary=False,
            reasons=tuple(reasons),
            traces=tuple(traces),
        )

    # 6. 仅发行形式（OVA/OAD），无附属关系也无季集证据：unknown、可播放、不自动归父。
    if release_form is not None:
        reasons.append("只有 OVA/OAD 发行形式、缺少类型证据，保留为 unknown 可播放内容")
        trace(CONTENT_CLASS_UNKNOWN, ORIGIN_FILENAME, "release_form_only")
        return ContentClassification(
            content_class=CONTENT_CLASS_UNKNOWN,
            classification_state=CLASSIFICATION_INFERRED,
            media_type=MEDIA_TYPE_UNKNOWN,
            is_importable=True,
            is_auxiliary=False,
            reasons=tuple(reasons),
            traces=tuple(traces),
        )

    reasons.append("缺少作品类型证据，保留为 unknown 可播放内容")
    trace(CONTENT_CLASS_UNKNOWN, ORIGIN_UNKNOWN, "no_type_evidence")
    return ContentClassification(
        content_class=CONTENT_CLASS_UNKNOWN,
        classification_state=CLASSIFICATION_UNKNOWN,
        media_type=MEDIA_TYPE_UNKNOWN,
        is_importable=True,
        is_auxiliary=False,
        reasons=tuple(reasons),
        traces=tuple(traces),
    )


# --- 标题（C-003/C-004） ----------------------------------------------------


def arbitrate_title(
    tokens: FilenameTokens,
    directory: tuple[DirectoryToken, ...] = (),
    classification: ContentClassification | None = None,
    *,
    provisional_title: str = "",
    provisional_original: str = "",
    provisional_series_group: str = "",
) -> TitleDecision:
    """标题优先级：明确文件作品标题 > 最近非结构目录的唯一标题 > 临时识别结果。

    ``provisional_*`` 是既有识别器给出的候选（可能来自目录容器）。仅当它没有
    更强来源时才采用，并在 trace 中标明来源，避免目录标题静默压过明确文件名。
    """

    classification = classification or ContentClassification()
    file_title = _filename_title(tokens)
    directory_token = _nearest_semantic_directory(directory)
    directory_title = _directory_title(directory_token) if directory_token is not None else ""
    traces: list[DecisionTrace] = []

    def trace(value: str, origin: str, rule_id: str, alternatives: tuple[str, ...] = ()) -> None:
        traces.append(
            DecisionTrace(
                field="work_title",
                value=value,
                origin=origin,
                scope="file" if origin == ORIGIN_FILENAME else "directory",
                rule_id=rule_id,
                alternatives=alternatives,
            )
        )

    if file_title:
        alternatives = tuple(item for item in (directory_title, provisional_title) if item and item != file_title)
        trace(file_title, ORIGIN_FILENAME, "explicit_file_title", alternatives)
        return TitleDecision(
            work_title=file_title,
            original_title=provisional_original or file_title,
            series_group=provisional_series_group,
            traces=tuple(traces),
        )
    if directory_title:
        alternatives = tuple(item for item in (provisional_title,) if item and item != directory_title)
        trace(directory_title, ORIGIN_DIRECTORY, "nearest_semantic_directory", alternatives)
        return TitleDecision(
            work_title=directory_title,
            original_title=provisional_original or directory_title,
            series_group=provisional_series_group or directory_title,
            only_from_directory_hint=True,
            traces=tuple(traces),
        )
    trace(provisional_title, ORIGIN_PARSER_RULE, "provisional_title")
    return TitleDecision(
        work_title=provisional_title,
        original_title=provisional_original,
        series_group=provisional_series_group,
        traces=tuple(traces),
    )


def _filename_title(tokens: FilenameTokens) -> str:
    """文件名里的作品标题：复用成熟识别器的系列名提取，只保留文件级证据。

    提取不到时返回空，由最近非结构目录补位——目录不得静默压过明确文件名。
    """

    from app.recognition.media import _extract_series_name_from_filename

    head = _filename_title_head(tokens)
    if not head:
        return ""
    extracted = _extract_series_name_from_filename(f"{head}.mkv")
    extracted = (extracted or "").strip(" ._-·[]【】()（）")
    if not extracted or is_generic_container_title(extracted):
        return ""
    if _TECHNICAL_TOKEN_RE.match(extracted):
        return ""
    return extracted


def _filename_title_head(tokens: FilenameTokens) -> str:
    """截取集号 token 之前的部分；只剩季标记时返回空。"""

    stem = tokens.stem
    cut = len(stem)
    for pattern in (
        _SEASON_EPISODE_RE,
        _EPISODE_ONLY_RE,
        _RANGE_RE,
        _ABS_MARKER_RE,
        _CJK_EPISODE_RE,
        *_BARE_NUMBER_RES,
    ):
        match = pattern.search(stem)
        if match is not None:
            cut = min(cut, match.start())
    head = stem[:cut]
    head = re.sub(r"[\[【(（]\s*$", "", head).strip(" ._-·")
    # 只剩季标记（`作品甲 第1季 [S01E01]`）时不算文件级作品标题。
    for pattern in _DIR_SEASON_RES:
        head = pattern.sub(" ", head)
    head = re.sub(r"(?i)^\s*(?:Season|S)\s*\d*\s*$", "", head.strip())
    return head.strip(" ._-·")


def is_generic_container_title(value: str) -> bool:
    """通用结构容器名（Season/Specials/分类目录）不是作品标题。"""

    cleaned = _normalize_segment(value)
    if not cleaned:
        return True
    if _STRUCTURAL_DIR_RE.match(cleaned):
        return True
    return cleaned in {
        "动画", "新番", "剧集", "电影", "动漫", "番剧", "影视", "已完结", "完结",
        "media", "video", "tv", "anime", "movies", "series", "shows", "movie",
    }


def _directory_title(token: DirectoryToken) -> str:
    name = re.sub(r"(?i)^\[[^\]]*\]\s*", "", token.name).strip()
    name = re.sub(r"(?i)[\[【]?(?:tmdb|tmdbid)[-_\s]?\d+[\]】]?", "", name).strip()
    name = re.sub(r"\s*\{[^}]*\}\s*$", "", name).strip()
    return name


# --- 结构化提示（C-004） ----------------------------------------------------


def arbitrate_hint(
    *,
    filename_hint: tuple[int | None, str],
    structured_hint: tuple[str | int | None, str],
    evidence_hint: tuple[str | int | None, str] = ("", ""),
    classification: ContentClassification | None = None,
) -> HintDecision:
    """合并文件名提示、入口结构化提示与观察自带提示；冲突不自动绑定。"""

    classification = classification or ContentClassification()
    traces: list[DecisionTrace] = []
    reasons: list[str] = []

    def normalize(pair: tuple[str | int | None, str]) -> tuple[int | None, str]:
        raw_id, raw_type = pair if pair else (None, "")
        if raw_id is None:
            return None, ""
        try:
            value = int(raw_id)
        except (TypeError, ValueError):
            reasons.append("provider_hint_invalid")
            return None, ""
        if value <= 0:
            reasons.append("provider_hint_invalid")
            return None, ""
        hint_type = (raw_type or "").strip().casefold()
        if hint_type not in {"tv", "movie"}:
            hint_type = ""
        return value, hint_type

    filename_id, filename_type = normalize(filename_hint)
    structured_id, structured_type = normalize(structured_hint)
    observation_id, observation_type = normalize(
        (int(evidence_hint[0]), evidence_hint[1]) if evidence_hint[0] else (None, "")
    )

    candidates = [
        item
        for item in (
            (filename_id, filename_type, ORIGIN_FILENAME),
            (structured_id, structured_type, ORIGIN_STRUCTURED_HINT),
            (observation_id, observation_type, ORIGIN_STRUCTURED_HINT),
        )
        if item[0] is not None
    ]
    if not candidates:
        return HintDecision(reasons=tuple(reasons))

    ids = {item[0] for item in candidates}
    types = {item[1] for item in candidates if item[1]}
    conflict = len(ids) > 1 or len(types) > 1
    if conflict:
        reasons.append("provider_hint_conflict")
        for value, hint_type, origin in candidates:
            traces.append(
                DecisionTrace(
                    field="provider_hint",
                    value={"id": value, "type": hint_type},
                    origin=origin,
                    scope="file",
                    rule_id="hint_conflict",
                    alternatives=tuple(
                        f"{item[0]}:{item[1]}" for item in candidates if item[0] != value or item[1] != hint_type
                    ),
                )
            )
        return HintDecision(conflict=True, reasons=tuple(reasons), traces=tuple(traces))

    value, hint_type, origin = candidates[0]
    declarations = [item[2] for item in candidates if item[0] == value and item[1] == hint_type]
    merged_origin = origin if len(declarations) == 1 else ORIGIN_STRUCTURED_HINT
    traces.append(
        DecisionTrace(
            field="provider_hint",
            value={"id": value, "type": hint_type},
            origin=merged_origin,
            scope="file",
            rule_id="hint_agreement" if len(declarations) > 1 else "hint_single_source",
        )
    )
    if len(declarations) > 1:
        reasons.append("provider_hint_merged")
    # 类型未指定时不能自行猜 tv/movie；只在分类能明确媒体类型时补。
    if not hint_type and classification.media_type in {MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE}:
        hint_type = classification.media_type
    return HintDecision(tmdb_id=value, tmdb_type=hint_type, reasons=tuple(reasons), traces=tuple(traces))


def group_type_for(content_class: str, numbering: NumberingFacts, media_type: str) -> str:
    """把分类结果映射到既有 group_type 词表，供下游兼容消费。"""

    if content_class == CONTENT_CLASS_AUXILIARY:
        return "auxiliary"
    if content_class == CONTENT_CLASS_ATTACHED_SPECIAL:
        return "special"
    if content_class == CONTENT_CLASS_MOVIE or media_type == MEDIA_TYPE_MOVIE:
        return "movie"
    if content_class in {CONTENT_CLASS_REGULAR, CONTENT_CLASS_STANDALONE} and (
        numbering.episode is not None or numbering.absolute is not None or numbering.season is not None
    ):
        return "season"
    return "unknown"


def is_importable_classification(classification: ContentClassification) -> bool:
    return classification.content_class not in NON_IMPORTABLE_CONTENT_CLASSES
