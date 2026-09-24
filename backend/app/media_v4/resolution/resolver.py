"""把 ParsedFacts 批量收口为 Work/Season/Episode/Asset 图。"""

from __future__ import annotations

import re
from collections import OrderedDict, defaultdict
from collections.abc import Set as AbstractSet
from pathlib import PurePosixPath

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
from app.media_v4.resolution.title_norm import normalize_identity_title
from app.media_v4.sources.adapters import provider_to_source
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
            (_boundary_key_title(facts.series_group), _effective_media_type(facts))
            for _evidence, facts in entries
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
        for evidence, facts in entries:
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
            for evidence, facts in entries
        }
        _coalesce_missing_year_keys(entries, entry_work_keys)
        for evidence, facts in entries:
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
            entries,
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

        for evidence, facts in entries:
            if not facts.is_importable or facts.is_auxiliary:
                continue
            identity_title = facts.work_title or (facts.title_candidates or ("",))[0]
            key = entry_work_keys[evidence.evidence_id]
            # 阶段 1（架构方案）：身份无法确定的条目得到"本地兜底键"，**不再被丢弃**。
            local_fallback = key.startswith("local:")
            if facts.needs_review:
                # 身份确实无法确定的条目保留 need_review 语义（供界面提示）；身份已确定的
                # 解析不确定信息只是提示，不阻断确认。前端据 blocking_issue_count 判断。
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
            if local_fallback:
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

            work = work_rows.setdefault(
                key,
                {
                    "title": _preferred_work_title(facts, key),
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
            if facts.year_candidate is not None:
                # 同一 Work 出现多个年份时取最早的一年（系列起始年）。之前是
                # "先出现的赢"，会让作品年份随条目顺序变化，与
                # _preferred_work_title 声明的顺序无关不一致。
                current_year = work["year"]
                work["year"] = (
                    facts.year_candidate
                    if current_year is None
                    else min(current_year, facts.year_candidate)
                )

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
                local_special_identity = _local_special_identity(evidence, facts)
                if local_special_identity is not None:
                    resolved_special_number = allocated_local_specials[
                        (key, local_special_identity)
                    ]
                else:
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
