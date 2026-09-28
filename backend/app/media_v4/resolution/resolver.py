"""把 ParsedFacts 批量收口为 Work/Season/Episode/Asset 图。

C-002/C-003/C-004 的集合级归属全部在这里完成：准入集合、确定性归组、统一
Episode key。集合级预计算（系列身份、年份借用、标题频率、季号传播、特殊篇编号）
只吃准入条目；冲突不拦整批（强标题不同拆 Work、编号不明拆 Episode），只有内部
不变量违背（同 evidence 被分到互斥 Work、未定位集缺 SourceFile 槽位、准入条目
没有去向）才抛错。
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict, defaultdict
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from app.media_v4.domain.identity import (
    CONTENT_CLASS_UNKNOWN,
    NON_IMPORTABLE_CONTENT_CLASSES,
    ORIGIN_DIRECTORY,
    ORIGIN_EXPLICIT_ABSOLUTE,
    ORIGIN_EXPLICIT_DIRECTORY,
    ORIGIN_EXPLICIT_FILENAME,
    ORIGIN_FILENAME,
    ORIGIN_PARSER_RULE,
    ORIGIN_SIDECAR,
    SEASON_KIND_REGULAR,
    SEASON_KIND_UNASSIGNED,
    trace_field,
)
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
from app.media_v4.resolution.candidates import strip_season_marker
from app.media_v4.resolution.identity_contract import (
    SPECIAL_SEASON_NUMBER,
    TitleCandidate,
    episode_identity_key,
    episode_row_key,
    season_identity_key,
    select_canonical_value,
    select_canonical_work,
)
from app.media_v4.resolution.title_norm import normalize_identity_title
from app.media_v4.sources.adapters import provider_to_source
from app.media_v4.sources.file_identity import (
    IDENTITY_KIND_LOCATOR,
    ObservedFile,
    decide_source_identity,
    derived_source_file_id,
)

if TYPE_CHECKING:
    from app.media_v4.persistence.source_identity import IdentityContext
from app.recognition.media import (
    _extract_work_container,
    _is_bracket_heavy,
    _is_series_container,
    _looks_like_plain_season_dir,
    _looks_like_specials_dir,
    _parse_work_title_and_year,
)

# 这些 issue 只是提示：作品身份已经确定，或者关系需要后续补全，不阻断确认。
# 前端据 blocking_issue_count 决定能否建立媒体库。
NON_BLOCKING_ISSUE_CODES = frozenset({
    "parsed_facts_review_hint",
    "unresolved_parent_relation",
})

def _normalize_title(value: str) -> str:
    """身份语义：唯一实现在 `title_norm.normalize_identity_title`。

    它参与持久化身份键（`title:<标题>:<年份>:<类型>`），因此只能收紧、不能放宽；
    比较"是否同名"请用 `title_norm.normalize_match_title`。
    """

    return normalize_identity_title(value)


def _is_placeholder_title(value: str) -> bool:
    return is_generic_container_title(value)


#: 中日韩文字（含假名、谚文、片假名中点）。
_CJK_CHAR_CLASS = (
    "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"
    "\u3005\u3006\u30fb\uff01\uff1f"
)
#: 中日韩文本**内部**的空格：`天元突破 红莲螺岩` 与 `天元突破红莲螺岩` 是同一部作品。
#: 只收敛夹在两个字之间的空白，拉丁词之间的空格（`Love Live`）必须保留。
_CJK_INNER_SPACE = re.compile(rf"(?<=[{_CJK_CHAR_CLASS}])[ \t]+(?=[{_CJK_CHAR_CLASS}])")


def _boundary_key_title(value: str | None) -> str:
    """作品**边界键**专用的标题归一化（身份语义 + 忽略中日韩内部空格）。

    同一部作品的两份物理目录常把空格写成 `天元突破 红莲螺岩` / `天元突破红莲螺岩`；
    这是命名差异，不是两个作品。该差异只允许在**边界键**上收敛：
    `title_norm.normalize_identity_title` 是共享身份语义，不能被放宽。
    """

    return _CJK_INNER_SPACE.sub("", normalize_identity_title(value))


#: 容器/季目录名**首部**的季度标记：`S3 辉夜大小姐想让我告白…`、`第2季 某作品`。
#: 标记后面必须是分隔符或行尾，避免把 `S1m0ne` 这类真标题当成季度。
_BOUNDARY_SEASON_PREFIX = re.compile(
    r"^(?:s(\d{1,2})|season\s*(\d+)|第\s*([一二三四五六七八九十百零〇0-9]+)\s*[季期部])"
    r"(?:[\s._\-—–~～:：·]|$)",
    re.IGNORECASE,
)
#: 容器/季目录名**尾部**的季度标记：`暗杀教室第二季`、`赛马娘 第三季`、`Show S2`、
#: `2nd Season`、`灵能百分百 路人超能100 Ⅲ`。
_BOUNDARY_SEASON_SUFFIX = re.compile(
    r"[\s._\-—–~～:：·（）()【】]*"
    r"(?:"
    r"第\s*([一二三四五六七八九十百零〇0-9]+)\s*[季期部]"
    r"|season\s*(\d+)"
    r"|(\d+)(?:st|nd|rd|th)\s*season"
    r"|s(\d{1,2})"
    r"|([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ])"
    r"|[\s._\-—–~～:：·]+(ii|iii|iv|vi|vii|viii|ix|xi|xii)"
    r")(\s*)$",
    re.IGNORECASE,
)
#: 罗马数字季度号（`灵能百分百 路人超能100 Ⅲ`）——`strip_season_marker` 不处理它。
_ROMAN_SEASON_SUFFIX = re.compile(
    r"[\s._\-—–~～:：·]*(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]|(?:ii|iii|iv|vi|vii|viii|ix|xi|xii))\s*$",
    re.IGNORECASE,
)
_ROMAN_NUMERALS = {
    "Ⅰ": 1, "Ⅱ": 2, "Ⅲ": 3, "Ⅳ": 4, "Ⅴ": 5, "Ⅵ": 6,
    "Ⅶ": 7, "Ⅷ": 8, "Ⅸ": 9, "Ⅹ": 10, "Ⅺ": 11, "Ⅻ": 12,
    "ii": 2, "iii": 3, "iv": 4, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "xi": 11, "xii": 12,
}
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100,
}


def _season_index_from_token(token: str) -> int | None:
    """`一`/`二`/`十一`/`2`/`II`/`Ⅲ` 形式的季号 → 整数。"""

    text = str(token or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    lowered = text.casefold()
    if lowered in _ROMAN_NUMERALS:
        return _ROMAN_NUMERALS[lowered]
    # 全角罗马数字大小写不同码位：`Ⅲ`.casefold() → `ⅲ`，需再按大写查一次。
    upper = text.upper()
    if upper in _ROMAN_NUMERALS:
        return _ROMAN_NUMERALS[upper]
    if all(char in _CN_DIGITS for char in text):
        if text == "十":
            return 10
        if "十" in text:
            high, _, low = text.partition("十")
            return (_CN_DIGITS.get(high, 1) if high else 1) * 10 + (_CN_DIGITS.get(low, 0) if low else 0)
        value = 0
        for char in text:
            value = value * 10 + _CN_DIGITS[char]
        return value
    return None


def _season_index_from_name(value: str) -> int | None:
    """目录名里的季标记序号（`第二季`/`S2`/`Season 2`/`Ⅲ`）；无标记返回 None。"""

    text = str(value or "").strip()
    if not text:
        return None
    for pattern in (_BOUNDARY_SEASON_PREFIX, _BOUNDARY_SEASON_SUFFIX):
        match = pattern.match(text) if pattern is _BOUNDARY_SEASON_PREFIX else pattern.search(text)
        if not match:
            continue
        for group in match.groups():
            if group is None:
                continue
            try:
                index = int(group)
            except ValueError:
                index = None
            if index is None:
                index = _season_index_from_token(group)
            if index is not None:
                return index
    return None


def _boundary_season_indexes(evidence: SourceEvidence, container: str) -> list[int]:
    """作品容器及其下方季目录里出现的**全部**季标记序号。

    纯结构季目录（`Season 1` / `S01` / `第一季` / `Specials`）不参与：
    它们不携带作品名，只是本地组织方式；把它们算作季标记会让
    `AIR.2005/Season 1/…` 与 `AIR.2015/Season 1/…` 因年份被抹掉而错误合并。
    """

    names = [container]
    directories = list(PurePosixPath(evidence.relative_path.replace("\\", "/")).parts[:-1])
    if container in directories:
        index = len(directories) - 1 - directories[::-1].index(container)
        names.extend(directories[index + 1:])
    indexes = []
    for name in names:
        if _looks_like_plain_season_dir(name) or _looks_like_specials_dir(name):
            continue
        season = _season_index_from_name(name)
        if season is not None:
            indexes.append(season)
    return indexes


def _has_boundary_season_marker(value: str) -> bool:
    """目录名是否带**明确**季度标记（首部 `S3 ` / `第2季 ` 或尾部 `第二季` / `S2` / `Ⅲ`）。"""

    return _season_index_from_name(value) is not None


def _strip_boundary_season_marker(title: str) -> str:
    """剥离季标记得到基名；剥完为空或过短时返回原值（只做单向收缩）。"""

    base = strip_season_marker(title)
    stripped = _ROMAN_SEASON_SUFFIX.sub("", base).strip(" ._-·、:：|/\\")
    if stripped != base and len(stripped) >= 2:
        return stripped
    return base


def _boundary_season_scoped(evidence: SourceEvidence, facts: ParsedFacts, container: str) -> bool:
    """本条目是否落在**带季度标记的目录**里，且该标记已经落到本地季号上。

    两种真实结构都要覆盖：
    - 标记就在作品容器上（`A 4k 暗杀教室/暗杀教室第一季/…`、`S1 灵能百分百…/…`）；
    - 容器只是作品名，标记在它下面的季目录上
      （`辉夜大小姐想让我告白/辉夜大小姐想让我告白/S3 …（2022）/…`、
      `命运石之门/命运石之门/S1 命运石之门（2011）/…`）。

    这两种情况下容器名解析出的年份都是**本季播出年**，不是作品身份的一部分。

    **季号一致是前提**：只有当标记序号与本地季号相同（`第二季` ↔ `season_candidate=2`）
    才能合并。否则（实测 `P 4k 排球少年/排球少年第二季/…` 的本地季号仍是 1——
    文件名没有 SxxEyy 也没有季 token）合并会把四季的集号全部撞在一起，
    85 集塔缩成 25 集。这种情形需要 Season 层补季号，不属于作品边界层。
    """

    indexes = _boundary_season_indexes(evidence, container)
    if not indexes:
        return False
    return all(index == facts.season_candidate for index in indexes)


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
    if facts.relation_type in {"spin_off", "movie", "recap", "related"}:
        return ""
    series = (facts.series_group or "").strip()
    if not series or is_generic_container_title(series):
        return ""
    if facts.relation_type == "main":
        return series
    work = (facts.work_title or "").strip()
    if _boundary_key_title(series) != _boundary_key_title(work):
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
    # 带季度标记的容器/季目录：目录名里的标题是"本季的写法"，年份是本季播出年。
    # 两者都不能进入作品身份，否则同一部作品的多季会被拆成多个 Work。
    season_scoped = _boundary_season_scoped(evidence, facts, container)
    if season_scoped:
        title = _strip_boundary_season_marker(title)
        year = None
    normalized = _boundary_key_title(title)
    if not normalized or _is_placeholder_title(normalized):
        return ""
    media_type = _effective_media_type(facts)
    resolved_series = _boundary_key_title(facts.series_group)
    key_year = "" if season_scoped else str(year or facts.year_candidate or "")
    if (
        facts.group_type == "special"
        and facts.card_type != "standalone"
        and resolved_series
        and not _is_placeholder_title(resolved_series)
        and resolved_series != normalized
    ):
        # 特别篇副标题可能成为独立目录名；当识别器已经通过通用副标题规则
        # 收口到主系列时，边界键必须消费该事实，不能再用原目录全名拆卡。
        return f"title:{resolved_series}:{key_year}:{media_type}"
    explicit_collection = bool(
        (_is_series_container(container) and not _is_bracket_heavy(container))
        or re.search(r"(?i)(?:系列|合集|\bseries\b|\bcollection\b)\s*$", container)
    )
    if explicit_collection:
        return f"series:{normalized}:{media_type}"
    return f"title:{normalized}:{key_year}:{media_type}"


def _work_key(facts: ParsedFacts, evidence: SourceEvidence | None = None) -> str:
    # 高置信 Provider 身份可以把不同语言、不同目录的同一正片合并；但它
    # 不能跨越已显式标出的独立作品/外传边界。后者仍由本地结构键保持隔离，
    # 并在 revision 预览阶段校验是否与既有绑定冲突。
    if (
        facts.tmdb_hint_id
        and facts.tmdb_hint_type
        and facts.confidence == "high"
        and facts.card_type != "standalone"
    ):
        return f"provider:{facts.tmdb_hint_type.casefold()}:{facts.tmdb_hint_id}"
    if evidence is not None:
        boundary_key = _boundary_work_key(evidence, facts)
        if boundary_key:
            return boundary_key
    series_title = _main_series_identity_title(facts)
    if series_title:
        return f"series:{_boundary_key_title(series_title)}:{_effective_media_type(facts)}"
    # 当前作品身份优先：独立/外传/电影子作品用 work_title；只有 work_title
    # 是通用容器/占位时才用稳定的 series_group 聚合（P-001 7.3.C）。
    identity_title = facts.work_title or (facts.title_candidates or ("",))[0]
    if is_generic_container_title(identity_title):
        for candidate in (facts.series_group, *facts.title_candidates):
            if candidate and not is_generic_container_title(candidate):
                identity_title = candidate
                break
    # 无容器可用时标题只能来自事实本身；此时若标题自带季标记，同样按基名收口
    # （年份同理不入键），否则它会与有容器的那份目录算成两个作品。
    season_marker = _has_boundary_season_marker(identity_title)
    if season_marker:
        identity_title = _strip_boundary_season_marker(identity_title)
    title = _boundary_key_title(identity_title)
    if _is_placeholder_title(title):
        # Provider hint 只在本地事实无法形成 Work 身份时兜底。它是外部
        # 映射，不得优先于目录结构、作品标题或主系列关系；否则错误 hint
        # 会把主系列、外传和电影直接改写成同一个 provider Work。
        if facts.tmdb_hint_id and facts.tmdb_hint_type:
            return f"provider:{facts.tmdb_hint_type.casefold()}:{facts.tmdb_hint_id}"
        return ""
    media_type = _effective_media_type(facts)
    year = "" if season_marker else str(facts.year_candidate or "")
    return f"title:{title}:{year}:{media_type}"


def _boundary_title_from_key(key: str) -> tuple[str, str, str] | None:
    """拆开 `title:<标题>:<年份>:<类型>` 边界键；不是边界键时返回 None。"""

    if not key.startswith("title:"):
        return None
    title, year, media_type = key[len("title:"):].rsplit(":", 2)
    return title, year, media_type


def _relation_work_key_from_row(row: dict) -> str:
    """由 work 行数据推导父系列 Work key，供 relations 构建使用。"""

    series_group = str(row.get("series_group") or "")
    if not series_group or is_generic_container_title(series_group):
        return ""
    child_media_type = str(row.get("media_type") or "unknown").casefold()
    relation_type = str(row.get("relation_type") or "").casefold()
    # 外传、总集篇和剧场版自身都是 movie Work，但 series_group 指向的是
    # TV 主系列。若沿用子作品的 movie 类型构造父键，持久化层只会查找一个
    # 不存在的 ``series:<主系列>:movie``，留下伪“待处理”关系。目录树与
    # OpenList 都会经过这里，因此在聚合层统一把这类父项收口为 TV。
    forced_tv_parent = child_media_type == "movie" and relation_type in {
        "movie",
        "spin_off",
        "recap",
        "related",
    }
    if forced_tv_parent:
        media_type = "tv"
    else:
        media_type = str(
            row.get("relation_media_type") or child_media_type or "unknown"
        ).casefold()
    # 自名抑制：series_group 常常只是作品自身的名字，此时不能当父系列。
    # 关键在于"被 movie 关系规则强制成 tv"也算自名——否则 media_type 已经
    # 不等于子类型，抑制失效，会造出永远查不到的 series:<自己>:tv。
    # 而显式给出不同 relation_media_type（例如同名电影确实有 TV 主系列）时
    # 仍然保留父子关系。
    if (_boundary_key_title(series_group) == _boundary_key_title(str(row.get("title") or ""))
            and (media_type == child_media_type or forced_tv_parent)):
        return ""
    return f"series:{_boundary_key_title(series_group)}:{media_type}"


def _edition_key(facts: ParsedFacts) -> str:
    tags = sorted({_normalize_title(tag) for tag in facts.edition_tags if _normalize_title(tag)})
    return "+".join(tags) or "default"


#: 子作品容器名里的季度/分部后缀：``Yuru Camp S2``、``CLANNAD 第二季``、``Part 2``。
#: 去掉它之后比较"基名"才能区分两种完全不同的结构：
#: - 基名相同（``Yuru Camp S1`` vs ``Yuru Camp S2``）→ **同一作品的多季**，仍归并；
#: - 基名不同（``CLANNAD`` vs ``轻音少女``）→ **合集**，各自成作品。
_SEASON_SUFFIX_PATTERN = re.compile(
    r"(?i)\s*(?:s\d{1,2}|season\s*\d{1,2}|\d{1,2}(?:st|nd|rd|th)\s+season"
    r"|第\s*(?:\d+|[一二三四五六七八九十]+)\s*季|part\s*\d+|cour\s*\d+)\s*$"
)


def _series_base_title(title: str) -> str:
    """去掉季度/分部后缀后的基名（用于判断是否同一作品的多季）。"""

    base = _SEASON_SUFFIX_PATTERN.sub("", title or "").strip()
    return base or (title or "").strip()


#: 判定"这是合集而不是多季系列"必须先看**名字**：真实目录里 ``京阿尼合集``、
#: ``4k 物语系列`` 会被明确命名成合集；而 ``Yuru Camp``、``摇曳露营`` 这类作品名
#: 永远不该因为"子目录基名不同"被当成合集——季节目录、特典目录与命名变体
#: （``Yuru Camp Season 2``、``[VCB-Studio] Yuru Camp``）都会产生不同基名。
#: 实测：只看基名差异会把一次本地导入的 Yuru Camp 拆成 3 部作品，其中一部无法
#: 自动匹配在线作品（忠实重放已复现：6 部 → 修正后 4 部）。
_COLLECTION_NAME_PATTERN = re.compile(
    r"(?i)(?:合集|合辑|全集|套装|系列|collection|complete\s+series|\bbox\s*set\b|\bpack\b)"
)


def _looks_like_collection_name(name: str) -> bool:
    """系列容器名是否**自称**为合集（判定合集的前提条件）。"""

    return bool(_COLLECTION_NAME_PATTERN.search(str(name or "")))


def _local_fallback_work_key(evidence: SourceEvidence, facts: ParsedFacts) -> str:
    """身份无法确定时的**稳定本地键**（阶段 1）。

    本地记录是基本结果，在线资料是可选补充：无法判断作品身份的视频也必须留下可查的
    本地作品与文件。键必须稳定（同一路径每次相同），且**不能**冒用在线身份或路径哈希
    作为全局身份；无可用标题时退化为按证据的本地键。
    """

    identity_title = facts.work_title or (facts.title_candidates or ("",))[0]
    seed = (
        _normalize_title(identity_title)
        or _normalize_title(facts.work_title)
        or _normalize_title(facts.series_group)
    )
    media_type = _effective_media_type(facts)
    if seed and not is_generic_container_title(seed):
        return f"local:{seed}:{media_type}"
    return f"local:file:{evidence.evidence_id}"


def _resolved_entry_work_key(
    evidence: SourceEvidence,
    facts: ParsedFacts,
    structural_series_identities: set[tuple[str, str]],
    collection_series: AbstractSet[tuple[str, str]] = frozenset(),
) -> str:
    key = _work_key(facts, evidence)
    if facts.card_type == "standalone":
        return key
    media_type = _effective_media_type(facts)
    # 后续正片季会把完整目录名（例如 "Yuru Camp Season 2"）保留在
    # work_title，但同时给出主系列 series_group 与 relation_type=main。
    # 这是结构化的同作品证据，应归入同一个 TV Work。外传和电影的
    # series_group 同样可能指向父系列，却只能用于 relation；它们已经由
    # standalone/card type 在上方隔离，不能被这一规则吞并。
    #
    # **合集例外**：``京阿尼合集/CLANNAD/…`` 与 ``京阿尼合集/轻音少女/…`` 的
    # series_group 都是"京阿尼合集"，但它们是**不同作品**。把系列提升应用在合集上
    # 会把 200+ 个不同动画的文件并成同一部作品的剧集（身份、刮削、剧集编号全错），
    # 因此合集不参与系列归并（见 resolve() 里的 collection_series 判定）。
    normalized_group = _boundary_key_title(facts.series_group)
    if (
        facts.relation_type == "main"
        and normalized_group
        and (normalized_group, media_type) in structural_series_identities
        and (normalized_group, media_type) not in collection_series
    ):
        return f"series:{normalized_group}:{media_type}"
    matching_series = next(
        (
            normalized
            for normalized in (_boundary_key_title(facts.work_title),)
            if (normalized, media_type) in structural_series_identities
        ),
        "",
    )
    if matching_series:
        return f"series:{matching_series}:{media_type}"
    if key:
        return key
    # 阶段 1（架构方案）：身份无法确定的视频也必须有稳定本地键，否则会在后续流程里
    # 被丢弃（实测"revision confirmed 但 works=0 / assets=0"）。兜底键必须在这里生成：
    # 特殊篇编号等预分配都按这个键建立，晚生成会出现 KeyError。
    return _local_fallback_work_key(evidence, facts)


def _preferred_work_title(facts: ParsedFacts, work_key: str) -> str:
    """为同一 Work 选择与输入顺序无关的规范显示名。"""

    if work_key.startswith("series:") and facts.relation_type in {"", "main"}:
        series_title = (facts.series_group or "").strip()
        if series_title and not is_generic_container_title(series_title):
            return series_title
    return (facts.work_title or (facts.title_candidates or ("",))[0]).strip()


def _coalesce_missing_year_keys(
    entries: list[tuple[SourceEvidence, ParsedFacts]], entry_keys: dict[str, str],
) -> None:
    """同一来源、同一作品标题下，缺年份不构成另一个作品。

    作用域是 ``root_id + 边界标题 + 媒体类型``，不再是"同一个实际作品目录"：
    同一部作品的两份物理目录本来就常一侧写年份、一侧不写
    （``天元突破红莲螺岩.2007`` 与 ``T 4k 天元突破 红莲螺岩``、
    ``命运石之门/命运石之门 0`` 与 ``命运石之门 [KissSub&MAI]/3.命运石之门 0``）。

    只有该标题下**唯一**一个已知年份时才补齐；两侧都带年份且不同
    （同一标题的 1999 版与 2011 版）必须继续是两条。只改本地键，
    不改 ParsedFacts，也不跨 root 借用标题或 Provider。
    """
    scopes: dict[tuple[str, str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for evidence, facts in entries:
        key = entry_keys[evidence.evidence_id]
        if not key.startswith("title:") or facts.card_type in {"standalone", "movie"}:
            continue
        if facts.relation_type not in {"", "main"}:
            continue
        parsed = _boundary_title_from_key(key)
        if parsed is None:
            continue
        title, year, media_type = parsed
        scopes[(evidence.root_id, title, media_type)].append((evidence.evidence_id, key, year))
    for members in scopes.values():
        dated_keys = {key for _eid, key, year in members if year}
        if len(dated_keys) != 1:
            continue
        canonical = next(iter(dated_keys))
        for evidence_id, _key, year in members:
            if not year:
                entry_keys[evidence_id] = canonical


_LOCAL_SPECIAL_TOKEN = re.compile(
    r"(?i)^(SP|OVA|OAD|OAV)0*(\d+)(?:[_-]0*(\d+))?$"
)
_AUTHORITATIVE_SPECIAL_TOKEN = re.compile(r"(?i)^S00E0*(\d+)$")


def _special_context(evidence: SourceEvidence) -> str:
    """取得发布内特别篇编号的最小稳定上下文。

    `SP01` 通常会在每一季重新编号，不能作为整部 Work 的全局键。去掉
    尾部 SPs/Specials 结构目录后，保留季度/子作品目录；这样同季度同 token
    的 1080p/2160p 会合并，不同季度或复合 token 不会互相覆盖。
    """

    directories = list(PurePosixPath(evidence.relative_path.replace("\\", "/")).parts[:-1])
    while directories and re.fullmatch(
        r"(?i)(?:SPs?|Specials?|S00|Extras?|Bonus)",
        directories[-1].strip(),
    ):
        directories.pop()
    return _normalize_title(directories[-1] if directories else "")


def _local_special_identity(
    evidence: SourceEvidence,
    facts: ParsedFacts,
) -> tuple[str, int, int | None] | None:
    match = _LOCAL_SPECIAL_TOKEN.fullmatch((facts.episode_token_raw or "").strip())
    if not match:
        return None
    return (
        _special_context(evidence),
        int(match.group(2)),
        int(match.group(3)) if match.group(3) is not None else None,
    )


#: 准入集合排除的类别（C-002）：附属内容与辅助视频不生成媒体实体。
_ADMITTED_EXCLUDED_CONTENT_CLASSES = NON_IMPORTABLE_CONTENT_CLASSES

#: 单文件可展开的最大集合集数；与 evidence_policy 的同一上限一致（非法范围不扩张）。
_MAX_EPISODE_RANGE = 1000

#: 标题证据等级：文件级 > 目录级 > 解析规则兜底。
_TITLE_EVIDENCE_RANK = {
    ORIGIN_FILENAME: 3,
    ORIGIN_DIRECTORY: 2,
    ORIGIN_SIDECAR: 2,
    ORIGIN_PARSER_RULE: 1,
}


def _is_admitted(facts: ParsedFacts) -> bool:
    """条目是否进入集合级计算（C-002 准入集合）。

    兼容升级前的人工/旧 ParsedFacts：``content_class`` 默认 unknown 不构成排除
    依据（unknown 不等于 special/movie），只按显式的 ``is_importable`` /
    ``is_auxiliary`` 与明确声明的排除类别过滤。
    """

    if not facts.is_importable or facts.is_auxiliary:
        return False
    return facts.content_class not in _ADMITTED_EXCLUDED_CONTENT_CLASSES


def _observed_for_identity(
    evidence: SourceEvidence, *, namespace: str, namespace_kind_name: str
) -> ObservedFile:
    """把一次观察映射为连续性判定的输入（C-001）。

    这里只消费槽位 ID：连续性分类（unchanged/uncertain/replaced）由确认事务的
    持久化仓储负责，Resolver 不重新判定内容代次，也不访问文件系统。
    """

    return ObservedFile(
        evidence_id=evidence.evidence_id,
        source_key=evidence.source_key,
        locator=(
            evidence.source_locator
            or evidence.playback_locator
            or evidence.source_key
            or evidence.relative_path
        ),
        identity_namespace=namespace,
        namespace_kind_name=namespace_kind_name,
        size=evidence.size,
        mtime=evidence.mtime,
        raw_file_id=evidence.raw_file_id,
        identity_kind=IDENTITY_KIND_LOCATOR,
    )


def _source_file_ids(
    entries: list[tuple[SourceEvidence, ParsedFacts]],
    identity_context: IdentityContext | None,
) -> dict[str, str]:
    """每条准入证据的 SourceFile 槽位 ID（C-001/C-003 的未定位锚点）。

    传了 ``identity_context`` 就按它的上一代观察做连续槽位判定；没传时由出生观察
    推导（同一个 evidence 每次得到同一 ID）。两种方式都是确定的，不依赖遍历顺序。
    """

    if identity_context is None:
        return {
            evidence.evidence_id: derived_source_file_id(evidence.evidence_id)
            for evidence, _facts in entries
        }
    previous = tuple(identity_context.previous_observations)
    namespace = str(getattr(identity_context, "namespace", "") or "")
    namespace_kind_name = str(getattr(identity_context, "identity_kind_name", "") or "remote")
    result: dict[str, str] = {}
    for evidence, _facts in entries:
        current = _observed_for_identity(
            evidence, namespace=namespace, namespace_kind_name=namespace_kind_name
        )
        decision = decide_source_identity(current, previous, namespace=namespace or None)
        result[evidence.evidence_id] = decision.source_file_id
    return result


def _work_identity_title(work_key: str) -> str:
    """取出 Work key 声明的本地身份标题；provider/local 键没有本地身份标题。"""

    if work_key.startswith("series:"):
        body = work_key[len("series:"):]
        parts = body.rsplit(":", 1)
        return parts[0] if len(parts) == 2 else ""
    if work_key.startswith("title:"):
        parsed = _boundary_title_from_key(work_key)
        return parsed[0] if parsed is not None else ""
    return ""


def _title_evidence_rank(facts: ParsedFacts) -> int:
    """标题的证据等级：来自单文件仲裁的 decision_trace；缺证据记 0（最弱）。"""

    trace = trace_field(facts.decision_trace, "work_title")
    if trace is None:
        return 0
    return _TITLE_EVIDENCE_RANK.get(trace.origin, 0)


def _local_episodes(facts: ParsedFacts) -> tuple[int | None, ...]:
    """单文件覆盖的本地集号；非法范围（反向/超长）不扩张，保留文件级定位。"""

    episode_range = facts.episode_range
    if episode_range is not None:
        start, end = int(episode_range[0]), int(episode_range[1])
        if start <= end and (end - start) <= _MAX_EPISODE_RANGE:
            return tuple(range(start, end + 1))
    return (facts.episode_candidate,)


@dataclass(frozen=True, slots=True)
class _EpisodeClaim:
    """一个准入条目编号后的成员主张（尚未决定最终 Episode 身份）。"""

    evidence_id: str
    work_key: str
    source_file_id: str
    edition_key: str
    season_kind: str
    local_season_number: int | None
    local_episodes: tuple[int | None, ...]
    absolute_episode_number: int | None
    special_number: int | None
    episode_kind: str
    display_title: str
    title_norm: str
    has_provider_identity: bool
    has_structural_series_identity: bool
    has_boundary_identity: bool


@dataclass(frozen=True, slots=True)
class _NumberingPlacement:
    """同批裁决结果（STEP-004）。仅 Resolver 局部使用，不写回 ParsedFacts。"""

    evidence_id: str
    effective_media_type: str
    local_episode_number: int | None
    local_season_number: int | None
    origin: str
    reason: str


#: 只有这些来源的集号才能充当“同批正片锚点”。
_ANCHOR_EPISODE_ORIGINS = frozenset(
    {ORIGIN_EXPLICIT_FILENAME, ORIGIN_EXPLICIT_ABSOLUTE}
)


def _candidate_group_key(evidence: SourceEvidence, facts: ParsedFacts) -> tuple[str, str]:
    """候选裁决的作用域：同一次扫描 + 同一个明确作品边界。

    刻意不包含媒体类型：待裁决条目正是“类型未知”的那批，用类型入键会让
    它们和它们的正片兄弟不在同一组，从而永远拿不到证据。
    """

    group = _boundary_key_title(facts.series_group)
    if not group:
        container = _extract_work_container(
            evidence.relative_path, provider_to_source(evidence.provider)
        )
        if container:
            group = _normalize_title(_parse_work_title_and_year(container)[0])
    return (str(evidence.scan_id or ""), group)


def _is_tv_anchor(facts: ParsedFacts) -> bool:
    if _effective_media_type(facts) != "tv":
        return False
    if facts.group_type != "season":
        return False
    if facts.episode_candidate is None:
        return False
    return facts.numbering.episode_origin in _ANCHOR_EPISODE_ORIGINS


def _episode_candidates(facts: ParsedFacts) -> list[tuple[int, str, str]]:
    """从不可变事实的决定轨迹里读回（number, rule_id, title_prefix）候选。

    ParsedFacts 只持久化 NumberingEvidence，不持久化 NumberingFacts；候选按合同
    存在 decision_trace 中，这是唯一读取入口。
    """

    out: list[tuple[int, str, str]] = []
    for trace in facts.decision_trace or ():
        if str(getattr(trace, "field", "") or "") != "episode_number_candidate":
            continue
        value = trace.value if isinstance(trace.value, dict) else {}
        number = value.get("number")
        if not isinstance(number, int):
            continue
        out.append((number, str(value.get("rule_id") or trace.rule_id or ""), str(value.get("title_prefix") or "")))
    return out


def _adjudicate_numbering_candidates(
    admitted: list[tuple[SourceEvidence, ParsedFacts]],
) -> tuple[dict[str, _NumberingPlacement], set[str]]:
    """用同批正片证据裁决低上下文集号候选（F-004）。

    规则（只提升、不下沉，不补齐缺集，不跨作品边界合并）：

    - 在同一个“扫描 + 明确作品边界”分组里找 TV 正片锚点（显式文件集号）；
    - 至少 2 个不同集号的锚点 → 强上下文，纯数字 stem 与标题尾数均可提升；
      标题尾数还要求去掉尾数后的前缀与分组规范标题等价；
    - 只有 1 个锚点 → 弱上下文，仅当本条目带显式目录季号时提升（只赋本地季号）；
    - 候选与锚点集号相同、或一个文件有多个相矛盾候选 → 不提升（保留未知）；
    - 电影/独立外传/OVA/附属内容一律不提升。

    返回（提升表, 处于“序列上下文但仍未定位”的证据集）。后者只用于提示，
    不参与任何编号写入。
    """

    anchor_numbers: dict[tuple[str, str], set[int]] = defaultdict(set)
    group_of: dict[str, tuple[str, str]] = {}
    for evidence, facts in admitted:
        group_of[evidence.evidence_id] = _candidate_group_key(evidence, facts)
        if _is_tv_anchor(facts):
            anchor_numbers[group_of[evidence.evidence_id]].add(int(facts.episode_candidate))

    placements: dict[str, _NumberingPlacement] = {}
    unresolved_in_series: set[str] = set()
    for evidence, facts in admitted:
        group = group_of[evidence.evidence_id]
        anchors = anchor_numbers.get(group, set())
        series_context = len(anchors) >= 1
        if (
            _effective_media_type(facts) not in {"unknown", ""}
            or facts.content_class != CONTENT_CLASS_UNKNOWN
            or facts.group_type == "movie"
            or facts.numbering.basis
        ):
            continue
        candidates = _episode_candidates(facts)
        promoted: _NumberingPlacement | None = None
        if len(candidates) == 1 and series_context:
            number, rule_id, title_prefix = candidates[0]
            season = facts.season_candidate
            strong = len(anchors) >= 2
            weak_ok = (
                len(anchors) == 1
                and facts.numbering.season_origin == ORIGIN_EXPLICIT_DIRECTORY
                and season is not None
            )
            prefix_ok = rule_id != "title_suffix_number" or (
                _normalize_title(title_prefix) in _group_titles(admitted, group)
            )
            if (strong or weak_ok) and prefix_ok and number not in anchors:
                promoted = _NumberingPlacement(
                    evidence_id=evidence.evidence_id,
                    effective_media_type="tv",
                    local_episode_number=number,
                    local_season_number=season,
                    origin="batch_inferred",
                    reason=f"series_anchor_{len(anchors)}",
                )
        if promoted is not None:
            placements[evidence.evidence_id] = promoted
        elif series_context:
            unresolved_in_series.add(evidence.evidence_id)
    return placements, unresolved_in_series


def _group_titles(
    admitted: list[tuple[SourceEvidence, ParsedFacts]], group: tuple[str, str]
) -> frozenset[str]:
    """分组内已有的规范作品标题（含锚点的 work_title 与 series_group）。"""

    titles: set[str] = set()
    for evidence, facts in admitted:
        if _candidate_group_key(evidence, facts) != group:
            continue
        for value in (facts.work_title, facts.series_group, *(facts.title_candidates or ())):
            normalized = _normalize_title(value)
            if normalized and not is_generic_container_title(value):
                titles.add(normalized)
    return frozenset(titles)


def _apply_numbering_placement(facts: ParsedFacts, placement: _NumberingPlacement) -> ParsedFacts:
    """把裁决结果变成 Resolver 局部的派生事实（不写回不可变 ParsedFacts）。"""

    return replace(
        facts,
        media_type=placement.effective_media_type,
        group_type="season",
        season_candidate=placement.local_season_number,
        episode_candidate=placement.local_episode_number,
        reasons=(*facts.reasons, "batch_inferred_episode_number"),
    )


def _entry_issues(
    evidence: SourceEvidence, facts: ParsedFacts, work_key: str
) -> list[ResolutionIssue]:
    """解析不确定信息与本地兜底身份的提示（不阻断确认）。"""

    issues: list[ResolutionIssue] = []
    if facts.needs_review:
        local_fallback = work_key.startswith("local:")
        issues.append(
            ResolutionIssue(
                code="parsed_facts_review_hint" if not local_fallback else "parsed_facts_need_review",
                evidence_id=evidence.evidence_id,
                message=(
                    "解析结果包含不确定信息，不影响确认，可稍后核对"
                    if not local_fallback
                    else "解析结果标记为需要人工复核，已按清洗后的本地名称入库"
                ),
            )
        )
    if work_key.startswith("local:"):
        identity_title = facts.work_title or (facts.title_candidates or ("",))[0]
        generic = bool(identity_title.strip()) and is_generic_container_title(identity_title)
        issues.append(
            ResolutionIssue(
                code="generic_container_title" if generic else "work_identity_missing",
                evidence_id=evidence.evidence_id,
                message=(
                    "目录/文件只能提供通用容器标题（Season/S01/Specials/分类/纯数字），"
                    "已按清洗后的本地名称入库，可稍后补充在线资料"
                    if generic
                    else "缺少足够的作品标题或 Provider 身份，已按清洗后的本地名称入库，"
                    "可稍后补充在线资料"
                ),
            )
        )
    # F-005：分维度提示。预期是 TV 正片（已判定类型与季度）却没有集号时，
    # 必须给出具体原因；普通电影没有集号不发提示；“开口未知”的单独文件
    # （没有序列上下文、没有候选）也不强行报错，避免把合法未知一律当失败。
    if (
        _effective_media_type(facts) == "tv"
        and facts.group_type == "season"
        and facts.episode_candidate is None
    ):
        issues.append(
            ResolutionIssue(
                code="episode_number_unresolved",
                evidence_id=evidence.evidence_id,
                message=(
                    "已归入本作品的正片序列，但文件名与目录都没有可用的集号；"
                    "不会自动补号，请核对文件命名或在预览里手动指定"
                ),
            )
        )
    return issues


def _build_work_groups(
    groups: OrderedDict[str, list[tuple[SourceEvidence, ParsedFacts]]],
) -> tuple[tuple[ResolvedWork, ...], OrderedDict[str, dict]]:
    """先建组，再用 select_canonical_work 选展示名与类别（C-004）。

    组内取值一律与遍历顺序无关：标题按 ``(证据等级, 独立 SourceFile 支持数,
    非全大写拉丁拼写, NFC 码点序)`` 选择；年龄/类型/关系等标量取"唯一明确值"，
    多值冲突按 unknown 处理（不再 setdefault 先到先得）。证据 ID 列表保持输入
    顺序，供调用方按本批观察读取。
    """

    works: list[ResolvedWork] = []
    rows: OrderedDict[str, dict] = OrderedDict()
    for work_key, members in groups.items():
        row = _canonical_work_row(work_key, members)
        rows[work_key] = row
        works.append(
            ResolvedWork(
                work_key=work_key,
                preferred_title=row["title"],
                year=row["year"],
                media_type=row["media_type"],
                source_evidence_ids=tuple(row["evidence_ids"]),
                card_type=row["card_type"],
                show_type=row["show_type"],
                series_group=row["series_group"],
                relation_type=row["relation_type"],
            )
        )
    return tuple(works), rows


def _canonical_work_row(
    work_key: str, members: list[tuple[SourceEvidence, ParsedFacts]]
) -> dict:
    """一个 Work 组内的规范字段（标题/类别/年份/身份）。"""

    identity_title = _work_identity_title(work_key)
    spelling_support: dict[str, int] = defaultdict(int)
    for _evidence, facts in members:
        title = _preferred_work_title(facts, work_key)
        if title:
            spelling_support[unicodedata.normalize("NFC", title.strip())] += 1
    title_candidates: list[TitleCandidate] = []
    for _evidence, facts in members:
        title = _preferred_work_title(facts, work_key)
        if not title:
            continue
        spelling = unicodedata.normalize("NFC", title.strip())
        rank = (
            4
            if identity_title and _boundary_key_title(spelling) == identity_title
            else _title_evidence_rank(facts)
        )
        title_candidates.append(
            TitleCandidate(
                title=spelling,
                evidence_rank=rank,
                support=spelling_support[spelling],
            )
        )
    canonical_title, _ambiguous = select_canonical_work(title_candidates)
    media_type, _media_conflict = select_canonical_value(
        [_effective_media_type(facts) for _evidence, facts in members]
    )
    card_type, _card_conflict = select_canonical_value([facts.card_type for _evidence, facts in members])
    series_group, _series_conflict = select_canonical_value(
        [facts.series_group for _evidence, facts in members]
    )
    relation_type, _relation_conflict = select_canonical_value(
        [facts.relation_type for _evidence, facts in members]
    )
    show_type, _show_conflict = select_canonical_value([facts.show_type for _evidence, facts in members])
    relation_media_type, _relation_media_conflict = select_canonical_value(
        [(facts.media_type or facts.group_type or "unknown").casefold() for _evidence, facts in members]
    )
    explicit_years = {
        facts.year_candidate for _evidence, facts in members if facts.year_candidate is not None
    }
    evidence_ids: list[str] = []
    for evidence, _facts in members:
        if evidence.evidence_id not in evidence_ids:
            evidence_ids.append(evidence.evidence_id)
    return {
        "title": canonical_title or (identity_title and unicodedata.normalize("NFC", identity_title)) or "",
        # 同组年份取最早的明确年份：年份已参与 Work 身份键，组内不会出现两个
        # 互不相容的系列起始年（跨 Work 借用另有唯一性约束）。
        "year": min(explicit_years) if explicit_years else None,
        "media_type": media_type,
        "relation_media_type": relation_media_type,
        "card_type": card_type,
        "show_type": show_type,
        "series_group": series_group,
        "relation_type": relation_type,
        "evidence_ids": evidence_ids,
    }


def _episode_claim(
    evidence: SourceEvidence,
    facts: ParsedFacts,
    *,
    work_key: str,
    edition_key: str,
    source_file_id: str,
    allocated_local_specials: dict[tuple[str, tuple[str, int, int | None]], int],
    allocated_unnumbered_specials: dict[tuple[str, str], int],
) -> _EpisodeClaim:
    """把一个准入条目收口为成员主张（特殊篇编号在这里确定）。"""

    identity_title = facts.work_title or (facts.title_candidates or ("",))[0]
    if facts.group_type == "special" or facts.special_candidate:
        local_season: int | None = SPECIAL_SEASON_NUMBER
        local_episodes: tuple[int | None, ...] = (None,)
        resolved_special_number: int | None
        local_special_identity = _local_special_identity(evidence, facts)
        if local_special_identity is not None:
            resolved_special_number = allocated_local_specials[(work_key, local_special_identity)]
        else:
            resolved_special_number = facts.special_number or facts.episode_candidate
        if resolved_special_number is None or resolved_special_number <= 0:
            title_identity = _normalize_title(facts.episode_title) or _normalize_title(
                evidence.relative_path
            )
            resolved_special_number = allocated_unnumbered_specials[(work_key, title_identity)]
        display_title = ensure_special_title_number(facts.episode_title, resolved_special_number)
        season_kind = "special"
        episode_kind = "special"
    else:
        local_season = facts.season_candidate
        local_episodes = _local_episodes(facts)
        resolved_special_number = None
        display_title = facts.episode_title
        # C-003：未知季是显式的 unassigned，不默认 S1，也不写 0。
        season_kind = SEASON_KIND_UNASSIGNED if local_season is None else SEASON_KIND_REGULAR
        episode_kind = "regular" if facts.group_type == "season" else facts.group_type or "unknown"
    return _EpisodeClaim(
        evidence_id=evidence.evidence_id,
        work_key=work_key,
        source_file_id=source_file_id,
        edition_key=edition_key,
        season_kind=season_kind,
        local_season_number=local_season,
        local_episodes=local_episodes,
        # Local numbering is authoritative. Absolute numbering only participates
        # in identity when no local episode number exists.
        absolute_episode_number=facts.absolute_episode_candidate,
        special_number=resolved_special_number,
        episode_kind=episode_kind,
        display_title=display_title,
        title_norm=_normalize_title(identity_title),
        has_provider_identity=bool(facts.tmdb_hint_id and facts.tmdb_hint_type),
        has_structural_series_identity=bool(_main_series_identity_title(facts)),
        has_boundary_identity=bool(_boundary_work_key(evidence, facts)),
    )


def _build_members(
    admitted: list[tuple[SourceEvidence, ParsedFacts]],
    *,
    entry_work_keys: dict[str, str],
    source_file_ids: dict[str, str],
    allocated_local_specials: dict[tuple[str, tuple[str, int, int | None]], int],
    allocated_unnumbered_specials: dict[tuple[str, str], int],
) -> tuple[tuple[ResolvedEpisode, ...], tuple[ResolvedWorkAsset, ...], list[ResolutionIssue]]:
    """把准入条目收口为 Episode 与电影 WorkAsset。

    先收集全部成员主张，再按统一 key 归组：本地编号为主键、绝对编号只作佐证、
    完全没有编号的条目以 SourceFile 槽位为稳定锚点（两个未知文件因此是两个
    Episode）。同一 Local 出现互斥绝对号时拆为各 SourceFile 的 provisional
    Episode，不合并后任取一个绝对号（C-003）。
    """

    issues: list[ResolutionIssue] = []
    claims: list[_EpisodeClaim] = []
    work_asset_rows: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
    for evidence, facts in admitted:
        work_key = entry_work_keys[evidence.evidence_id]
        issues.extend(_entry_issues(evidence, facts, work_key))
        edition_key = _edition_key(facts)
        if _effective_media_type(facts) == "movie":
            movie_assets = work_asset_rows.setdefault((work_key, edition_key), [])
            if evidence.evidence_id not in movie_assets:
                movie_assets.append(evidence.evidence_id)
            continue
        claims.append(
            _episode_claim(
                evidence,
                facts,
                work_key=work_key,
                edition_key=edition_key,
                source_file_id=source_file_ids[evidence.evidence_id],
                allocated_local_specials=allocated_local_specials,
                allocated_unnumbered_specials=allocated_unnumbered_specials,
            )
        )

    episode_rows = _episode_rows(claims, issues)
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
            provider_season_number=row["provider_season_number"],
            provider_episode_number=row["provider_episode_number"],
            identity_key=row["identity_key"],
            season_identity_key=row["season_identity_key"],
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
    return episodes, work_assets, issues


def _episode_rows(
    claims: list[_EpisodeClaim], issues: list[ResolutionIssue]
) -> OrderedDict[tuple, dict]:
    """按统一身份收口 Episode 行；互斥绝对号拆为独立 provisional 行。"""

    absolutes: dict[tuple[str, str, int], set[int]] = defaultdict(set)
    for claim in claims:
        if claim.absolute_episode_number is None:
            continue
        for local_episode in claim.local_episodes:
            if local_episode is None:
                continue
            season_key = season_identity_key(
                local_season_number=claim.local_season_number,
                season_kind=claim.season_kind,
                work_key=claim.work_key,
            )
            absolutes[(claim.work_key, season_key, int(local_episode))].add(
                int(claim.absolute_episode_number)
            )
    conflicted = {identity for identity, values in absolutes.items() if len(values) > 1}

    rows: OrderedDict[tuple, dict] = OrderedDict()
    for claim in claims:
        for local_episode in claim.local_episodes:
            row, conflict = _episode_row_for(claim, local_episode, conflicted)
            if conflict:
                issues.append(
                    ResolutionIssue(
                        code="absolute_episode_conflict",
                        evidence_id=claim.evidence_id,
                        message=(
                            "同一 Local Episode 出现互斥的绝对集号，已拆为独立的未定位条目并需要复核"
                        ),
                    )
                )
            row_key = (*row["logical_key"], row["edition_key"])
            existing = rows.get(row_key)
            if existing is None:
                rows[row_key] = row
                target = row
            else:
                _merge_episode_row(existing, row)
                target = existing
            _note_episode_identity_titles(target, claim, issues)
            if claim.evidence_id not in target["asset_ids"]:
                target["asset_ids"].append(claim.evidence_id)
    return rows


def _episode_row_for(
    claim: _EpisodeClaim, local_episode: int | None, conflicted: set[tuple[str, str, int]]
) -> tuple[dict, bool]:
    """一个成员主张在某集上的行数据与"是否互斥绝对号冲突"。"""

    absolute_episode_number = claim.absolute_episode_number
    local_season_number = claim.local_season_number
    season_kind = claim.season_kind
    episode_kind = claim.episode_kind
    special_number = claim.special_number
    conflict = False
    if local_episode is not None and absolute_episode_number is not None:
        season_key = season_identity_key(
            local_season_number=local_season_number,
            season_kind=season_kind,
            work_key=claim.work_key,
        )
        conflict = (claim.work_key, season_key, int(local_episode)) in conflicted
    if conflict:
        # 互斥绝对号：本地编号与绝对编号都不能作为身份，退回 SourceFile 槽位锚点。
        local_season_number = None
        local_episode = None
        absolute_episode_number = None
        special_number = None
        season_kind = SEASON_KIND_UNASSIGNED
        episode_kind = "unknown"
    season_key = season_identity_key(
        local_season_number=local_season_number,
        season_kind=season_kind,
        work_key=claim.work_key,
    )
    absolute_key = absolute_episode_number if local_episode is None else None
    episode_key = episode_row_key(
        work_key=claim.work_key,
        local_season_number=local_season_number,
        local_episode_number=local_episode,
        absolute_episode_number=absolute_episode_number,
        special_number=special_number,
        source_file_key=claim.source_file_id,
    )
    identity_key = episode_identity_key(
        season_key=season_key,
        local_episode_number=local_episode,
        absolute_episode_number=absolute_episode_number,
        fallback_key=episode_key,
    )
    if local_episode is None and absolute_episode_number is None and special_number is None:
        # 未定位条目：锚点就是 SourceFile 槽位，两个未知文件天然是两个 Episode。
        logical_key: tuple = (claim.work_key, season_key, None, None, None, claim.source_file_id)
    else:
        logical_key = (claim.work_key, season_key, local_episode, absolute_key, special_number, None)
    return (
        {
            "episode_key": episode_key,
            "identity_key": identity_key,
            "season_identity_key": season_key,
            "work_key": claim.work_key,
            "local_season_number": local_season_number,
            "local_episode_number": local_episode,
            "absolute_episode_number": absolute_episode_number,
            "season_kind": season_kind,
            "episode_kind": episode_kind,
            "special_number": special_number,
            "display_title": claim.display_title,
            "edition_key": claim.edition_key,
            "provider_season_number": None,
            "provider_episode_number": None,
            "source_file_id": claim.source_file_id,
            "logical_key": logical_key,
            "asset_ids": [],
            "title_norms": set(),
            "has_provider_identity": claim.has_provider_identity,
            "has_structural_series_identity": claim.has_structural_series_identity,
            "has_boundary_identity": claim.has_boundary_identity,
        },
        conflict,
    )


def _merge_episode_row(existing: dict, incoming: dict) -> None:
    """把同一逻辑行（同 edition）的第二个成员并入已有行。"""

    if existing["absolute_episode_number"] is None and incoming["absolute_episode_number"] is not None:
        existing["absolute_episode_number"] = incoming["absolute_episode_number"]
    chosen, _ambiguous = select_canonical_work(
        [existing["display_title"], incoming["display_title"]]
    )
    if chosen:
        existing["display_title"] = chosen
    for field in ("provider_season_number", "provider_episode_number"):
        if existing[field] is None and incoming[field] is not None:
            existing[field] = incoming[field]


def _note_episode_identity_titles(
    target: dict, claim: _EpisodeClaim, issues: list[ResolutionIssue]
) -> None:
    """同一集出现不同独立作品标题时提示，不静默当成多版本（P-001 7.3.C）。"""

    title_norm = claim.title_norm
    if not title_norm or title_norm in target["title_norms"]:
        return
    # 该行此前已有另一个标题才算"同一集出现不同独立作品标题"。单个条目首次
    # 声明标题（例如 override 修正后的唯一文件）不是冲突，不能据此发提示。
    had_other_title = bool(target["title_norms"])
    target["title_norms"].add(title_norm)
    if not had_other_title:
        return
    if target["has_structural_series_identity"] or target["has_boundary_identity"]:
        return
    if target["has_provider_identity"] and claim.has_provider_identity:
        return
    issues.append(
        ResolutionIssue(
            code="ambiguous_work_identity",
            evidence_id=claim.evidence_id,
            message="同一集出现指向不同独立作品的 Asset，不能自动合并为多版本",
        )
    )


def _assert_member_outcome(
    admitted: list[tuple[SourceEvidence, ParsedFacts]],
    episodes: tuple[ResolvedEpisode, ...],
    work_assets: tuple[ResolvedWorkAsset, ...],
) -> None:
    """内部不变量：每个准入条目必须落到恰好一个 Work，且要么在 Episode、要么在电影 Asset。

    这些是图内部的自洽条件（同 evidence 被分到互斥 Work、未定位集缺 SourceFile
    槽位、准入条目没有去向），损坏图不允许进入确认；普通识别歧义不在这里抛错。
    """

    assignment: dict[str, str] = {}

    def assign(evidence_id: str, work_key: str) -> None:
        previous = assignment.setdefault(evidence_id, work_key)
        if previous != work_key:
            raise ValueError(
                f"内部不变量违背：evidence {evidence_id} 被分配到互斥 Work（{previous} / {work_key}）"
            )

    for episode in episodes:
        if (
            episode.local_episode_number is None
            and episode.absolute_episode_number is None
            and episode.special_number is None
            and not episode.identity_key
        ):
            raise ValueError("内部不变量违背：未定位 Episode 缺少 SourceFile 槽位锚点")
        for evidence_id in episode.asset_evidence_ids:
            assign(evidence_id, episode.work_key)
    for asset in work_assets:
        for evidence_id in asset.asset_evidence_ids:
            assign(evidence_id, asset.work_key)
    missing = sorted(
        evidence.evidence_id
        for evidence, _facts in admitted
        if evidence.evidence_id not in assignment
    )
    if missing:
        raise ValueError(f"内部不变量违背：准入条目没有落到 Work/Episode/Asset：{missing}")


def _build_relations(work_rows: OrderedDict[str, dict]) -> list[ResolvedWorkRelation]:
    """由作品行的显式结构事实建立父系列关系；父 Work 可能只存在于数据库。"""

    relations: list[ResolvedWorkRelation] = []
    for key, row in work_rows.items():
        parent_key = _relation_work_key_from_row(row)
        if not parent_key or parent_key == key:
            continue
        relations.append(
            ResolvedWorkRelation(
                parent_work_key=parent_key,
                child_work_key=key,
                relation_type=row["relation_type"] or "related",
            )
        )
    return relations


class MediaResolver:
    """Resolver 只负责聚合，不修改输入事实、不淘汰 Asset。"""

    def resolve(
        self,
        entries: list[tuple[SourceEvidence, ParsedFacts]],
        *,
        identity_context: IdentityContext | None = None,
    ) -> ResolvedMediaGraph:
        # C-002 准入集合：排除条目保留 source/pf 事实与原因，但不参与系列身份、
        # 分组、年份借用、标题频率、季号传播、候选查询与关系建图。
        admitted_raw = [
            (evidence, facts)
            for evidence, facts in entries
            if _is_admitted(facts)
        ]
        # STEP-004：用同批正片证据裁决低上下文集号候选。裁决只产生 Resolver 局部
        # 的派生事实（replace 副本），绝不回写不可变 ParsedFacts；确认后写入的是
        # resolved_json，而不是改事实。
        placements, unresolved_in_series = _adjudicate_numbering_candidates(admitted_raw)
        admitted = [
            (
                evidence,
                _apply_numbering_placement(facts, placements[evidence.evidence_id])
                if evidence.evidence_id in placements
                else facts,
            )
            for evidence, facts in admitted_raw
        ]
        # 未定位条目以 SourceFile 槽位为稳定锚点：传了上一代 context 就按连续性
        # 复用槽位，没传时由出生观察推导（两者都确定，重复预览结果一致）。
        source_file_ids = _source_file_ids(admitted, identity_context)
        issues: list[ResolutionIssue] = []
        # 单文件解析只能看到当前路径；整批图谱则能看到同一目录边界中由
        # Season/Special 条目声明的主系列身份。只采用 relation_type=main
        # 的结构事实，外传/电影的父系列关系不能反向吞并当前作品。
        structural_series_identities = {
            (_boundary_key_title(facts.series_group), _effective_media_type(facts))
            for _evidence, facts in admitted
            if facts.card_type != "standalone"
            and facts.relation_type == "main"
            and facts.series_group
            and not is_generic_container_title(facts.series_group)
        }
        explicit_special_numbers: dict[str, set[int]] = defaultdict(set)
        local_special_identities: dict[str, set[tuple[str, int, int | None]]] = defaultdict(set)
        # 「合集目录」与「多季系列」只能靠**整批**区分，单条解析看不出：
        # - ``京阿尼合集/CLANNAD/…`` + ``京阿尼合集/轻音少女/…`` → 同一 series_group
        #   下出现两个不同子作品容器 → 这是合集，各子作品必须各自成作品；
        # - ``摇曳露营/第1季/…`` + ``摇曳露营/第2季/…`` → 子作品容器是结构目录
        #   （提取为空），集合里只剩作品自身 → 仍是同一 Work 的多季。
        series_child_containers: dict[tuple[str, str], set[str]] = defaultdict(set)
        for evidence, facts in admitted:
            if facts.card_type == "standalone":
                continue
            group = _boundary_key_title(facts.series_group)
            if not group or is_generic_container_title(facts.series_group):
                continue
            container = _extract_work_container(
                evidence.relative_path, provider_to_source(evidence.provider)
            )
            if not container:
                continue
            child_title = _normalize_title(_parse_work_title_and_year(container)[0])
            if child_title:
                series_child_containers[(group, _effective_media_type(facts))].add(child_title)
        # 判定合集需要**两个条件同时成立**：
        #   1. 系列容器名自称合集（``京阿尼合集`` / ``4k 物语系列`` / ``xxx collection``）；
        #   2. 去掉季度后缀后的基名确实不止一个（``CLANNAD`` vs ``轻音少女``）。
        # 只看第 2 条会把季节/特典/命名变体误判成合集，从而把同一部作品拆卡
        # （实测：一次本地导入把 Yuru Camp 拆成 3 部，其中一部无法自动匹配）。
        collection_series = {
            series_key
            for series_key, children in series_child_containers.items()
            if len({_series_base_title(child) for child in children}) > 1
            and _looks_like_collection_name(str(series_key[0]))
        }
        entry_work_keys = {
            evidence.evidence_id: _resolved_entry_work_key(
                evidence, facts, structural_series_identities, collection_series,
            )
            for evidence, facts in admitted
        }
        _coalesce_missing_year_keys(admitted, entry_work_keys)
        # 特别篇编号分配**只对显式声明附属关系的准入条目**生效：新解析器已把附属
        # 内容排除在准入集合外，这里服务升级前的人工事实与既有绑定。
        for evidence, facts in admitted:
            if facts.group_type != "special" and not facts.special_candidate:
                continue
            key = entry_work_keys[evidence.evidence_id]
            local_identity = _local_special_identity(evidence, facts)
            if key and local_identity is not None:
                local_special_identities[key].add(local_identity)
                continue
            authoritative = _AUTHORITATIVE_SPECIAL_TOKEN.fullmatch(
                (facts.episode_token_raw or "").strip()
            )
            if key and authoritative:
                explicit_special_numbers[key].add(int(authoritative.group(1)))
                continue
            number = facts.special_number or facts.episode_candidate
            if key and number is not None and number > 0:
                explicit_special_numbers[key].add(number)

        allocated_local_specials: dict[
            tuple[str, tuple[str, int, int | None]], int
        ] = {}
        for key, identities in local_special_identities.items():
            claimed = set(explicit_special_numbers.get(key, set()))
            ordered = sorted(
                identities,
                key=lambda value: (value[0], value[1], -1 if value[2] is None else value[2]),
            )
            number_frequency: dict[int, int] = defaultdict(int)
            for _context, number, _subnumber in ordered:
                number_frequency[number] += 1
            # 单一、非复合 SP08 等保留原编号，便于后续 Provider 映射；
            # 跨季度重号或 SP01_13 复合编号必须重新分配全局 S00 身份。
            for identity in ordered:
                _context, number, subnumber = identity
                if subnumber is None and number_frequency[number] == 1 and number not in claimed:
                    allocated_local_specials[(key, identity)] = number
                    claimed.add(number)
            next_number = 1
            for identity in ordered:
                allocation_key = (key, identity)
                if allocation_key in allocated_local_specials:
                    continue
                while next_number in claimed:
                    next_number += 1
                allocated_local_specials[allocation_key] = next_number
                claimed.add(next_number)
                next_number += 1
            explicit_special_numbers[key].update(claimed)
        next_special_number = {
            key: max(numbers, default=0) + 1
            for key, numbers in explicit_special_numbers.items()
        }
        allocated_unnumbered_specials: dict[tuple[str, str], int] = {}
        for evidence, facts in sorted(
            admitted,
            key=lambda item: (item[0].relative_path.casefold(), item[0].evidence_id),
        ):
            if facts.group_type != "special" and not facts.special_candidate:
                continue
            if (facts.special_number or facts.episode_candidate or 0) > 0:
                continue
            key = entry_work_keys[evidence.evidence_id]
            if not key:
                continue
            title_identity = _normalize_title(facts.episode_title) or _normalize_title(
                evidence.relative_path
            )
            # 与上面的 (key, identity) 三元组分配键不同维度：这里按"标题身份"分配
            # 无编号特别篇的编号，因此用独立变量名，避免两种元组形状互相污染。
            title_allocation_key = (key, title_identity)
            if title_allocation_key in allocated_unnumbered_specials:
                continue
            special_number = next_special_number.get(key, 1)
            while special_number in explicit_special_numbers.get(key, set()):
                special_number += 1
            allocated_unnumbered_specials[title_allocation_key] = special_number
            explicit_special_numbers[key].add(special_number)
            next_special_number[key] = special_number + 1

        work_groups: OrderedDict[str, list[tuple[SourceEvidence, ParsedFacts]]] = OrderedDict()
        for evidence, facts in admitted:
            work_groups.setdefault(entry_work_keys[evidence.evidence_id], []).append(
                (evidence, facts)
            )
        works, work_rows = _build_work_groups(work_groups)
        episodes, work_assets, member_issues = _build_members(
            admitted,
            entry_work_keys=entry_work_keys,
            source_file_ids=source_file_ids,
            allocated_local_specials=allocated_local_specials,
            allocated_unnumbered_specials=allocated_unnumbered_specials,
        )
        issues.extend(member_issues)
        # 序列上下文里仍无法定位的条目：给出具体原因，而不是静默零问题（F-005）。
        for evidence, facts in admitted:
            if evidence.evidence_id not in unresolved_in_series:
                continue
            if _effective_media_type(facts) != "unknown":
                continue
            issues.append(
                ResolutionIssue(
                    code="media_type_unresolved",
                    evidence_id=evidence.evidence_id,
                    message=(
                        "同一序列中的这个文件既没有可用集号，也没有足够的类型与作品边界证据；"
                        "已保留为未定位条目，不会自动归入某一集"
                    ),
                )
            )
        _assert_member_outcome(admitted, episodes, work_assets)
        relations = _build_relations(work_rows)
        return ResolvedMediaGraph(
            works=works,
            episodes=episodes,
            work_assets=work_assets,
            relations=tuple(relations),
            issues=tuple(issues),
        )
