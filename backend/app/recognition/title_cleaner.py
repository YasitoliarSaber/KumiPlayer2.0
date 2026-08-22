# -*- coding: utf-8 -*-
"""作品名清洗规则

从作品容器目录名中提取干净的 work_title。
只处理已确认的清洗问题，保守清洗，不强行猜。
"""

import re
from dataclasses import dataclass, field
from typing import List

# 状态词
_STATUS_WORDS = ["（将更新）", "(将更新)", "（更新中）", "(更新中)"]

# 系列容器结构词正则：.S1-S2、.S1~S3 等范围结构
# 只处理明确的 S\d+-S\d+ 范围，不处理单独 .S1 避免误伤
_RE_SERIES_RANGE = re.compile(r"\.[Ss]\d+\s*[-~]\s*[Ss]\d+.*$", re.DOTALL)

# 明确的目录组织标签，不是作品标题。只处理带 "+" 的合集结构，避免把
# 正式作品副标题中的 TV/外传等词误删。
_RE_SERIES_CATALOG_SUFFIX = re.compile(
    r"\.(?:TV(?:版)?|动画版)(?:\s*\+\s*(?:外传|剧场版|电影|OVA|OAD|SP|特别篇))*$",
    re.IGNORECASE,
)
_RE_RELEASE_LANGUAGE_SUFFIX = re.compile(r"\s+(?:内封中字|内封简繁|简繁内封|中字内封)$")

# 方括号 token 正则
_RE_BRACKET_TOKEN = re.compile(r"\[([^\]]*)\]")

# 部分目录树会把分区字母塞进作品名，例如：
# B 86-不存在的战区 -> 86-不存在的战区
# 仅在单字母后面接数字标题时清理，避免误伤 A Channel / K-ON! 等真实作品名。
_RE_SINGLE_LETTER_NUMERIC_PREFIX = re.compile(r"^[A-Za-z]\s+(?=\d)")

# 技术标签关键词（用于过滤方括号 token）
_TECH_KEYWORDS = {
    "bdrip", "webrip", "web-dl", "bdmv", "remux",
    "hevc", "hevc-10bit", "h264", "h265", "x264", "x265", "avc",
    "10bit", "8bit", "hi10p", "ma10p",
    "1080p", "2160p", "720p", "480p", "4k",
    "chs", "cht", "chsjp", "chttp", "chi_jpn", "scjp", "tcjp", "jpn", "eng",
    "mp4", "mkv", "flac", "aac", "ac3", "truehd", "dts", "e-ac3",
    "fin", "movie", "v2", "v3",
}

# 技术标签正则模式（匹配类似 Ma10p_1080p、01-12、01-47+MOVIE 等）
_RE_TECH_PATTERN = re.compile(
    r"^(?:"
    r"Ma\d+p.*"                    # Ma10p_1080p 等
    r"|\d+[-~]\d+.*"               # 01-12、13-24 等集数范围
    r"|\d+bit"                     # 10bit、8bit
    r"|\d+p"                       # 1080p、2160p
    r"|[A-Z]{2,3}_\d+[pi]"        # WEB_1080p 等
    r"|[A-Z]+-[A-Z]+-\d+"         # HEVC-10bit 等
    r")$",
    re.IGNORECASE,
)

# 字幕组关键词（用于识别字幕组 token）
_FANSUB_KEYWORDS = {
    "sub", "raw", "raws", "studio", "vcb", "vcb-studio",
    "lolihouse", "sakurato", "sweetsub", "dmg", "haruhana",
    "nekomoe", "kissaten", "nekomoe kissaten", "beansub", "fzsd", "cz", "mai",
    "t.h.x", "lp-raws", "airota", "ktxp", "thx",
    "sweet", "loli", "as", "nw", "ea",
}


@dataclass
class TitleCleanResult:
    """清洗结果"""

    title: str = ""
    changed: bool = False
    confidence_delta: str = ""  # 清洗后置信度变化
    needs_review: bool = False
    warnings: List[str] = field(default_factory=list)
    applied_rules: List[str] = field(default_factory=list)


def _is_tech_token(token: str) -> bool:
    """判断 token 是否为技术标签"""
    lower = token.lower().strip()
    if lower in _TECH_KEYWORDS:
        return True
    if _RE_TECH_PATTERN.match(token.strip()):
        return True
    return False


def _is_fansub_token(token: str) -> bool:
    """判断 token 是否为字幕组标签。

    字幕组名称按完整 token 精确匹配，或由 &、+ 连接的逐组件精确匹配，
    不再对 as、ea、nw、mai、sweet 等短关键词执行任意子串包含，
    避免误伤 Mai-HiME、Sweet Home 等真实标题。

    只按 & 和 + 拆分（字幕组连接符），不按 -、空格、_、. 拆分：
    一方面保留 'vcb-studio'、'lp-raws'、't.h.x' 这类带内部标点的真实组名，
    另一方面避免把 'Mai-HiME'、'Sweet Home' 这类含分隔符的真实标题拆成短词后误命中。
    """
    lower = token.lower().strip()
    if not lower:
        return False
    # 整体精确匹配（如 sweetsub、lolihouse、vcb-studio）
    if lower in _FANSUB_KEYWORDS:
        return True
    # 按 &、+ 拆分后逐组件精确匹配（如 BeanSub&FZSD、T.H.X&VCB-Studio）
    has_joiner = "&" in lower or "+" in lower
    if not has_joiner:
        return False
    parts: list[str] = []
    for sep in ("&", "+"):
        if not parts:
            parts = lower.split(sep)
        else:
            parts = [p for part in parts for p in part.split(sep)]
    for part in parts:
        cleaned = part.strip()
        if cleaned and cleaned in _FANSUB_KEYWORDS:
            return True
    return False


def _select_best_token(tokens: List[str]) -> tuple[str, bool]:
    """从方括号 token 中选择最像作品名的

    返回:
        (选中的 token, 是否需要 review)
    """
    if not tokens:
        return "", True

    if len(tokens) == 1:
        return tokens[0], False

    # 过滤掉技术标签和字幕组标签
    candidates = []
    for t in tokens:
        if _is_tech_token(t):
            continue
        if _is_fansub_token(t):
            continue
        candidates.append(t)

    if not candidates:
        # 全被过滤了，选最长的
        return max(tokens, key=len), True

    if len(candidates) == 1:
        return candidates[0], False

    # 多个候选，选最长的
    return max(candidates, key=len), True


def clean_work_title_container(container: str) -> TitleCleanResult:
    """清洗作品容器目录名，提取干净的 work_title

    清洗顺序：
    1. 去掉状态词
    2. 去掉系列容器结构词（.S1-S2 等范围）
    3. 处理字幕组方括号标签
    4. 空白归一化

    参数:
        container: 作品容器目录名，如 "CLANNAD.S1-S2+SP+OVA"

    返回:
        TitleCleanResult
    """
    if not container:
        return TitleCleanResult(title="", changed=False, needs_review=True, warnings=["容器名为空"])

    original = container
    result = TitleCleanResult(title=container)
    applied = []

    # 1. 去掉状态词
    cleaned = container
    for word in _STATUS_WORDS:
        if word in cleaned:
            cleaned = cleaned.replace(word, "")
            applied.append(f"去掉状态词: {word}")

    # 1.2 去掉 TMDB ID 提示，ID 本身由识别层作为结构化字段保留。
    from app.scrape.tmdb_hint import strip_tmdb_hint

    tmdb_cleaned = strip_tmdb_hint(cleaned)
    if tmdb_cleaned != cleaned:
        cleaned = " ".join(tmdb_cleaned.split())
        applied.append("去掉 TMDB ID 提示")

    # 1.5 去掉目录树分区字母前缀
    prefixed = _RE_SINGLE_LETTER_NUMERIC_PREFIX.sub("", cleaned).strip()
    if prefixed != cleaned:
        cleaned = prefixed
        applied.append("去掉单字母分区前缀")

    # 2. 去掉系列容器结构词（.S1-S2 等范围）
    m = _RE_SERIES_RANGE.search(cleaned)
    if m:
        cleaned = _RE_SERIES_RANGE.sub("", cleaned)
        applied.append(f"去掉系列容器结构词: {m.group()}")

    # 2.1 去掉明确的合集目录标签与发布语言尾注。
    catalog_cleaned = _RE_SERIES_CATALOG_SUFFIX.sub("", cleaned).strip()
    if catalog_cleaned != cleaned:
        cleaned = catalog_cleaned
        applied.append("去掉系列合集目录标签")

    release_cleaned = _RE_RELEASE_LANGUAGE_SUFFIX.sub("", cleaned).strip()
    if release_cleaned != cleaned:
        cleaned = release_cleaned
        applied.append("去掉发布语言尾注")

    # 3. 处理字幕组方括号标签
    brackets = _RE_BRACKET_TOKEN.findall(cleaned)
    if brackets:
        # 去掉所有方括号 token
        no_brackets = _RE_BRACKET_TOKEN.sub("", cleaned).strip()
        # 空白归一化
        no_brackets = " ".join(no_brackets.split())

        if no_brackets:
            # 有剩余文本，使用剩余文本
            cleaned = no_brackets
            applied.append(f"去掉方括号标签，保留剩余文本")
        else:
            # 去掉方括号后为空，从 token 中选择
            selected, needs_review = _select_best_token(brackets)
            if selected:
                cleaned = selected
                applied.append(f"从方括号 token 中选择: {selected}")
                if needs_review:
                    result.needs_review = True
                    result.warnings.append("从多个方括号 token 中选择，可能不准确")
            else:
                # 无法选择，保留原始值
                result.needs_review = True
                result.warnings.append("无法从方括号 token 中提取作品名")
                applied.append("无法提取，保留原始值")

    # 4. 去掉末尾年份（.2005、(2005)、（2005）等）
    _year_patterns = [
        re.compile(r"[.．]\d{4}$"),
        re.compile(r"[(\（]\d{4}[)\）]$"),
        re.compile(r"\s\d{4}$"),
    ]
    for pat in _year_patterns:
        new_cleaned = pat.sub("", cleaned).strip()
        if new_cleaned != cleaned:
            cleaned = new_cleaned
            applied.append(f"去掉末尾年份")
            break

    # 5. 空白归一化
    cleaned = " ".join(cleaned.split()).strip()

    # 更新结果
    result.title = cleaned
    result.changed = cleaned != original
    result.applied_rules = applied

    # 如果清洗后为空，保留原始值
    if not cleaned:
        result.title = original
        result.changed = False
        result.needs_review = True
        result.warnings.append("清洗后为空，保留原始值")

    return result
