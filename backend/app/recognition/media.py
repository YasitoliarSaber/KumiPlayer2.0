# -*- coding: utf-8 -*-
"""媒体结构识别规则

识别作品名、年份、Season、Special、Movie；OP/ED 直接忽略。
PV/CM/Menu/Trailer/Eyecatch 等附属视频只保留播放结构，不进入 Special/S00 刮削。
优先级：OP/ED ignored > auxiliary > 独立卡片 > Special > Season。
不做 .strm 生成、TMDB 调用、数据库写入。
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Optional


# ============================================================
# 关键词定义
# ============================================================

# OP/ED 关键词匹配正则（按优先级排列）
# NCOP/NCED 可子串匹配（足够特定）
# OP/ED 需要边界匹配，避免 "Redline" 被 ED 误判
_OP_ED_PATTERNS = [
    re.compile(r"NCOP", re.IGNORECASE),           # NCOP01
    re.compile(r"NCED", re.IGNORECASE),           # NCED01
    re.compile(r"NON[-\s]?CREDIT\s+(?:OP|ED)", re.IGNORECASE),  # Non-Credit OP/ED
    re.compile(r"无字幕\s*(?:OP|ED)", re.IGNORECASE),  # 无字幕 OP/ED
    re.compile(r"OPENING", re.IGNORECASE),         # Opening
    re.compile(r"ENDING", re.IGNORECASE),          # Ending
    re.compile(r"[\[【]OP(?:\d+|\s+[^\]】]+)?[\]】]", re.IGNORECASE),  # [OP], [OP Original]
    re.compile(r"[\[【]ED(?:\d+|\s+[^\]】]+)?[\]】]", re.IGNORECASE),  # [ED], [ED Creditless]
    re.compile(r"\[OP\d*\]", re.IGNORECASE),       # [OP], [OP01]
    re.compile(r"\[ED\d*\]", re.IGNORECASE),       # [ED], [ED01]
    re.compile(r"(?:^|[^A-Za-z0-9])OP\d*(?=$|[^A-Za-z0-9])", re.IGNORECASE),  # OP「标题」/ OP - Title
    re.compile(r"(?:^|[^A-Za-z0-9])ED\d*(?=$|[^A-Za-z0-9])", re.IGNORECASE),  # ED「标题」/ ED - Title
    re.compile(r"(?:^|[^A-Za-z])OP\d+", re.IGNORECASE),  # OP01（前面不能是字母）
    re.compile(r"(?:^|[^A-Za-z])ED\d+", re.IGNORECASE),  # ED01（前面不能是字母）
]

# OpenList 通用分类目录名：选中此类目录时不用目录名作为系列名
# （选中“动画/剧集/已完结”导入多部作品，系列名必须来自各自作品）
_GENERIC_CATEGORY_NAMES = {
    "动画", "新番", "剧集", "电影", "动漫", "番剧", "影视", "动画电影",
    "已完结", "完结", "全部", "网盘", "115网盘", "百度网盘", "夸克网盘",
    "media", "video", "tv", "anime", "movies", "series", "shows", "movie",
}


def _is_generic_category_name(name: str) -> bool:
    return (name or "").strip().casefold() in _GENERIC_CATEGORY_NAMES


# OP/ED 目录名
_OP_ED_DIR_PATTERNS = [
    "OP＆ED", "OP&ED", "OPED", "OP_ED",
]

# 附属视频关键词。它们不是 TMDB Special，不应生成 S00 ScrapeTarget。
_AUXILIARY_PATTERNS = [
    re.compile(r"TV\s*SPOT", re.IGNORECASE),
    re.compile(r"TRAILER", re.IGNORECASE),
    re.compile(r"EYECATCH", re.IGNORECASE),
    re.compile(r"PREVIEW\s*\d+(?:\.\d+)?", re.IGNORECASE),
    re.compile(r"NON[-\s]?TELOP", re.IGNORECASE),
    re.compile(r"\[PV\d*\]", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z])PV\d+", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z])PV(?:\s|$|[([])", re.IGNORECASE),
    re.compile(r"[\[【]MV(?:\s+[^\]】]+)?[\]】]", re.IGNORECASE),
    re.compile(r"\[CM\d*\]", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z])CM\d+", re.IGNORECASE),
    re.compile(r"\[MENU\d*\]", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z])MENU\d+", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z0-9])MENU\d*(?:$|[^A-Za-z0-9])", re.IGNORECASE),
    re.compile(r"菜单|预告|花絮"),
]

_AUXILIARY_DIR_KEYWORDS = {
    "PV", "PVS", "CM", "CMS", "MV", "MVS", "MV合集", "MENU", "MENUS", "TRAILER", "TRAILERS", "EYECATCH",
}

# Special 关键词匹配正则
# OVA/OAD/Special/SP 等短关键词需要边界匹配，避免 "SPY x FAMILY" 被 SP 误判，
# 也避免 "Casanova" 被 OVA、"Roadshow" 被 OAD、"Specialized" 被 SPECIAL 子串误判。
# 使用字母边界 (?<![A-Za-z]) / (?![A-Za-z]) 而非 \b：\b 把字母↔数字视为连续
# （都是 word char），会让 "OVA" 在 "OVA01" 中不构成边界而漏匹配；
# 字母边界只禁止前后紧邻英文字母，既保留 "OVA01" 这类无空格写法，
# 又能挡住 "Casanova"/"Roadshow"/"Specialized" 这类词内子串。
# SPECIAL 保留可选复数 's' 后缀（SPECIALS?），以匹配 "Specials" 复数形式，
# 但仍挡住 "Specialized"（后跟 'i' 是字母）。
_SPS_PATTERNS = [
    re.compile(r"(?<![A-Za-z])OVA(?![A-Za-z])", re.IGNORECASE),      # OVA、OVA01
    re.compile(r"(?<![A-Za-z])OAD(?![A-Za-z])", re.IGNORECASE),      # OAD、OAD01
    re.compile(r"(?<![A-Za-z])SPECIALS?(?![A-Za-z])", re.IGNORECASE),  # Special、Specials
    re.compile(r"\bS00(?:E\d+)?\b", re.IGNORECASE),          # S00, S00E01
    re.compile(r"番外"),                                      # 番外
    re.compile(r"短篇"),                                      # 短篇
    re.compile(r"小剧场"),                                    # 小剧场
    re.compile(r"BD\s*特典"),                                 # BD 特典
    re.compile(r"[\[【]SP\d*[\]】]", re.IGNORECASE),           # [SP], 【SP01】
    re.compile(r"[\[【]LITE[\]】]", re.IGNORECASE),             # [Lite]
    re.compile(r"\[SP\d*\]", re.IGNORECASE),                 # [SP], [SP01]
    re.compile(r"(?:^|[^A-Za-z])SP\d+", re.IGNORECASE),     # SP01（前面不能是字母）
]

_EXPLICIT_FILENAME_SPECIAL_PATTERNS = [
    re.compile(r"[\[【](?:SP|OVA|OAD)\d*[\]】]", re.IGNORECASE),
    re.compile(r"[\[【]LITE[\]】]", re.IGNORECASE),
    re.compile(r"\bS00(?:E\d+)?\b", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z])(?:SP|OVA|OAD)\d+(?:$|[^A-Za-z])", re.IGNORECASE),
]

# 独立卡片关键词
_STANDALONE_KEYWORDS = {
    "movie": ("movie", "movie"),
    "剧场版": ("movie", "movie"),
    "映画": ("movie", "movie"),
    "总集篇": ("movie", "recap"),
    "extra edition": ("movie", "recap"),
    "外传": ("tv", "spin_off"),
    "spin-off": ("tv", "spin_off"),
    "spin off": ("tv", "spin_off"),
}

_KNOWN_ANIME_MOVIE_TITLE_KEYWORDS = {
    # Some anime films are released without "Movie"/"剧场版" in the filename.
    "yumeginga paradise": "佐贺偶像是传奇 梦幻银河乐园",
}

_MOVIE_CATEGORY_KEYWORDS = ("动画电影", "电影")

_MOVIE_TITLE_TECH_TOKENS = {
    "movie", "movies", "bdrip", "bluray", "bd", "web-dl", "webrip",
    "chs", "cht", "jpn", "eng", "mp4", "mkv", "avc", "aac", "flac",
    "x264", "x265", "h264", "h265", "hevc", "hi10p", "ma10p",
}

_BAIDU_CATEGORY_DIRS = {
    "动画",
    "新番",
    "刮削好的动画",
    "动画电影",
    "电影",
    "剧集",
    "番剧",
    "TV",
    "TV动画",
    "SPs",
    "Specials",
}

_GROUP_FOLDER_PATTERNS = [
    re.compile(r"^Season\s*\d+$", re.IGNORECASE),
    re.compile(r"^第\s*\d+\s*季$"),
    re.compile(r"^S\d+$", re.IGNORECASE),
    re.compile(r"^Specials?$", re.IGNORECASE),
    re.compile(r"^SPs?$", re.IGNORECASE),
    re.compile(r"^S00$", re.IGNORECASE),
    re.compile(r"^OP[＆&_-]?ED$", re.IGNORECASE),
]

_SPECIAL_TITLE_PATTERNS = [
    re.compile(r"\bEP\s*00\b", re.IGNORECASE),
    re.compile(r"(?:^|[^A-Za-z0-9])M\d+(?:$|[^A-Za-z0-9])", re.IGNORECASE),
    re.compile(r"MYSTERY\s+CAMP", re.IGNORECASE),
    re.compile(r"TABISURU\s+SHIMA\s+RIN", re.IGNORECASE),
    re.compile(r"HORA\s+CAMP", re.IGNORECASE),
    re.compile(r"SURVIVAL\s+CAMP", re.IGNORECASE),
    re.compile(r"(?:通向|登上|迈向)大人的阶梯"),
]

_KNOWN_SPECIAL_SUBTITLE_PATTERNS = [
    re.compile(r"\s*(?:通向|登上|迈向)大人的阶梯.*$"),
]

# 状态词（从作品名中去除）
_STATUS_WORDS = ["（将更新）", "(将更新)", "（更新中）", "(更新中)"]

# 系列容器结构词
_SERIES_CONTAINER_INDICATORS = [
    re.compile(r"S\d+\s*[-~]\s*S\d+", re.IGNORECASE),  # S1-S2, S1-S3
    re.compile(r"\+SP", re.IGNORECASE),
    re.compile(r"\+OVA", re.IGNORECASE),
    re.compile(r"\+剧场版"),
    re.compile(r"\+外传"),
    re.compile(r"\+Movie", re.IGNORECASE),
]


# ============================================================
# MediaGuess 数据结构
# ============================================================

@dataclass
class MediaGuess:
    """媒体识别结果（单个视频条目的识别猜测）"""

    work_id: str = ""
    work_title: str = ""
    original_title: str = ""
    year: Optional[int] = None
    media_type: str = ""  # tv / movie
    tmdb_hint_id: Optional[int] = None
    tmdb_hint_type: str = ""  # tv / movie

    series_group: str = ""
    card_type: str = ""  # main_series / standalone
    belongs_to_series: str = ""
    relation_type: str = ""  # main / movie / recap / spin_off / related

    group_type: str = ""  # season / special / auxiliary / ignored / movie
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    special_number: Optional[int] = None
    title: str = ""

    confidence: str = "medium"
    needs_review: bool = False
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ============================================================
# 辅助函数
# ============================================================

def _make_work_id(source: str, work_title: str, year: Optional[int], card_type: str) -> str:
    """生成稳定的 work_id"""
    parts = [source, work_title, str(year) if year else "", card_type]
    content = ":".join(parts)
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _extract_work_container(relative_path: str, source: str = "pan115") -> str:
    """从 relative_path 提取作品容器（分类层的下一层）

    例如：动画/AIR.2005/AIR.S01E01.mkv → AIR.2005
    本地来源没有固定分类层，直接取第一层：
    Yuru Camp/Season 1/xxx.mkv → Yuru Camp
    """
    parts = relative_path.replace("\\", "/").split("/")
    if source == "local":
        if len(parts) >= 3 and _is_local_collection_dir(parts[0]):
            if _looks_like_plain_season_dir(parts[1]):
                return parts[0]
            return parts[1]
        if len(parts) >= 2:
            return parts[0]
        return ""
    if source == "baidu":
        idx = _source_work_index(parts, source)
        return parts[idx] if idx is not None else ""
    # 优先按“分类层 + 作品目录”结构取第二层；路径不足三段（如 OpenList
    # 相对选中 root 的路径没有分类层，作品目录下直接就是文件）时，第一层
    # 就是作品容器，不能把文件名当容器。结构段目录名（S1/Season/第X季）
    # 永远不是作品容器。
    if len(parts) >= 3:
        if _looks_like_plain_season_dir(parts[1]):
            return ""
        return parts[1]
    if len(parts) == 2:
        if _looks_like_plain_season_dir(parts[0]):
            return ""
        return parts[0]
    return ""


def _extract_series_name_from_filename(filename: str) -> str:
    """从文件名提取系列名（剥离集号与发布标签后的标题）。

    对照看影音 LocalEpisodeParser 的 series 命名组思路：作品身份来自
    文件名本身（集号 token 之前的标题部分），不依赖路径层级，因此
    OpenList root-relative 路径（无分类层/作品目录缺失）也不会把
    集号或季目录误当作品名。

    支持：
    - 【Top o Nerae2! DieBuster】【04】【BDrip】... → Top o Nerae2! DieBuster
    - Violet Evergarden - 10                       → Violet Evergarden
    - Title S01E01                                 → Title
    - Title [04] / Title (04)                      → Title

    无法可靠提取时返回空串（调用方按 needs_review 处理）。
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    # 1) 全角括号链：第一个【】块通常是标题（不含集号特征时采用）
    m = re.match(r"^\s*【([^】]+)】", stem)
    if m:
        first = m.group(1).strip()
        if first and not re.search(
            r"(?i)(?:S\d{1,2}E\d{1,3}|第\s*\d+\s*[话話集]|\s*[-–—]\s*\d{1,3}$)",
            first,
        ):
            return first

    # 2) 按集号锚点切前缀（优先级从强到弱）
    for pattern in (
        re.compile(r"\bS\d{1,2}E\d{1,3}\b", re.IGNORECASE),
        re.compile(r"第\s*\d{1,3}\s*[话話集]"),
        re.compile(r"\s+[-–—]\s*\d{1,3}(?!\d)"),
        re.compile(r"[\[【]\s*\d{1,3}\s*[\]】]"),
        re.compile(r"\s+EP?\s*\d{1,3}\b", re.IGNORECASE),
    ):
        m = pattern.search(stem)
        if m and m.start() > 0:
            prefix = stem[:m.start()].strip().rstrip(" ._-–—")
            if prefix:
                return prefix
    return ""


def _extract_parent_dir(relative_path: str) -> str:
    """从 relative_path 提取父目录名

    例如：动画/AIR.2005/OP＆ED/NCOP01.mkv → OP＆ED
    """
    parts = relative_path.replace("\\", "/").split("/")
    if len(parts) >= 3:
        return parts[-2]
    return ""


def _extract_parent_dirs(relative_path: str, n: int = 2) -> List[str]:
    """从 relative_path 提取最近的 n 层父目录名（不含文件名）"""
    parts = relative_path.replace("\\", "/").split("/")
    # 去掉文件名，取目录部分
    dirs = parts[:-1] if len(parts) > 1 else []
    # 返回最后 n 层
    return dirs[-n:] if len(dirs) >= n else dirs


def _extract_subwork_dir(relative_path: str, source: str = "pan115") -> str:
    """提取系列容器下的子作品目录

    例如：动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.After.Story.2008/xxx.mkv
    → 2.CLANNAD.After.Story.2008

    只在路径深度 >= 4（分类层 + 容器 + 子目录 + 文件）时提取。
    """
    parts = relative_path.replace("\\", "/").split("/")
    if source == "local":
        if len(parts) >= 4 and _is_local_collection_dir(parts[0]):
            return "" if _looks_like_plain_season_dir(parts[1]) else parts[1]
        # 本地来源没有分类层：容器 + 子目录 + 文件
        if len(parts) >= 3:
            return "" if _is_group_folder(parts[1]) or _looks_like_plain_season_dir(parts[1]) else parts[1]
        return ""
    if source == "baidu":
        idx = _source_work_index(parts, source)
        if idx is not None and len(parts) >= idx + 3:
            subwork = parts[idx + 1]
            return "" if _is_group_folder(subwork) else subwork
        return ""
    # 至少需要：分类层 + 容器 + 子目录 + 文件
    if len(parts) >= 4:
        return "" if _is_group_folder(parts[2]) else parts[2]
    return ""


def _extract_top_category(relative_path: str, source: str = "pan115") -> str:
    """提取来源目录中的顶层分类名。

    115/百度目录树通常有“动画/动画电影/刮削好的动画”等分类层；
    本地来源没有固定分类层。
    """
    if source == "local":
        return ""
    parts = relative_path.replace("\\", "/").split("/")
    if source == "baidu":
        return parts[0] if parts and _is_baidu_category_dir(parts[0]) else ""
    return parts[0] if parts else ""


def _extract_local_series_container(relative_path: str, source: str = "pan115") -> str:
    """提取本地合集根目录作为系列容器。

    本地库经常是：
    [VCB-Studio] Yuru Camp / Yuru Camp / ...
    [VCB-Studio] Yuru Camp / Yuru Camp Season 2 / ...

    这种第一层不是作品本身的季，而是用户整理好的系列合集。后续生成
    LibraryIndex 时应按这个系列聚合，而不是把每个季度拆成卡片。
    """
    if source != "local":
        return ""
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    if len(parts) >= 3 and _is_local_collection_dir(parts[0]):
        return parts[0]
    return ""


def _is_baidu_category_dir(dirname: str) -> bool:
    normalized = dirname.strip()
    return normalized in _BAIDU_CATEGORY_DIRS


def _source_work_index(parts: List[str], source: str) -> Optional[int]:
    """Return the path segment that represents the work container."""
    if not parts:
        return None
    if source == "baidu":
        if _is_baidu_category_dir(parts[0]):
            return 1 if len(parts) >= 2 else None
        return 0
    if source == "local":
        if len(parts) >= 3 and _is_local_collection_dir(parts[0]):
            return 1
        return 0 if len(parts) >= 2 else None
    return 1 if len(parts) >= 2 else None


def _is_group_folder(dirname: str) -> bool:
    name = dirname.strip()
    return any(pat.search(name) for pat in _GROUP_FOLDER_PATTERNS)


def _is_local_collection_dir(dirname: str) -> bool:
    """判断本地第一层是否更像合集/字幕组容器，而不是单部作品。"""
    lower = dirname.lower()
    if dirname.startswith("[") and "]" in dirname:
        return True
    collection_tokens = ("vcb-studio", "collection", "合集", "系列")
    return any(token in lower for token in collection_tokens)


def _extract_year_from_subwork(subwork_dir: str) -> Optional[int]:
    """从子作品目录名提取年份

    2.CLANNAD.After.Story.2008 → 2008
    剧场版：序列之争.2017 → 2017
    """
    if not subwork_dir:
        return None
    patterns = [
        re.compile(r"[.．](\d{4})"),           # .2008
        re.compile(r"[(\（](\d{4})[)\）]"),    # (2008)
        re.compile(r"\s(\d{4})(?:\s|$)"),       # 空格2008
    ]
    for pat in patterns:
        m = pat.search(subwork_dir)
        if m:
            year = int(m.group(1))
            if 1900 <= year <= 2099:
                return year
    return None


def _clean_work_title(raw: str) -> str:
    """清洗作品名：去除状态词、年份分隔点"""
    title = raw.strip()
    # 去除状态词
    for word in _STATUS_WORDS:
        title = title.replace(word, "")
    return title.strip()


def _extract_tmdb_hint(text: str) -> tuple[Optional[int], str]:
    """Extract {tmdb-123} / {tmdbid=123} / [tmdbid=123] style hints."""
    if not text:
        return None, ""
    m = re.search(r"[\{\[]\s*(tmdb|tmdbid)\s*[-_=：:]?\s*(\d+)\s*[\}\]]", text, re.IGNORECASE)
    if not m:
        return None, ""
    return int(m.group(2)), "tv"


def _strip_tmdb_hint(text: str) -> str:
    """Remove TMDB hint braces from display/search titles."""
    cleaned = re.sub(r"\s*[\{\[]\s*(?:tmdb|tmdbid)\s*[-_=：:]?\s*\d+\s*[\}\]]\s*", " ", text or "", flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()


def _extract_year(text: str) -> Optional[int]:
    """从文本中提取年份

    支持：.2005、(2005)、（2005）、2005、 2019（前导空格）
    年份可以在末尾，也可以在中间（后面跟状态词等）
    """
    if not text:
        return None
    text = text.strip()
    patterns = [
        re.compile(r"[.．](\d{4})"),             # .2005
        re.compile(r"[(\（](\d{4})[)\）]"),      # (2005) or （2005）
        re.compile(r"(?:^|\s)(\d{4})(?:\s|$)"),  # 开头或空格2005
    ]
    for pat in patterns:
        m = pat.search(text)
        if m:
            year = int(m.group(1))
            if 1900 <= year <= 2099:
                return year
    return None


def _parse_work_title_and_year(container: str) -> tuple[str, Optional[int]]:
    """从作品容器解析作品名和年份

    AIR.2005 → (AIR, 2005)
    冰菓.2012 → (冰菓, 2012)
    红辣椒.Paprika.2006 → (红辣椒.Paprika, 2006)
    败犬女主太多了！.2024（将更新） → (败犬女主太多了！, 2024)
    CLANNAD.S1-S2+SP+OVA → (CLANNAD, None)
    [LP-Raws] One Room → (One Room, None)
    """
    from app.recognition.title_cleaner import clean_work_title_container

    raw_container = re.sub(r"\.(?:mkv|mp4|avi|mov|wmv|flv|m2ts|ts|webm)$", "", _strip_tmdb_hint(container), flags=re.IGNORECASE)

    # 先清洗容器名（去掉状态词、系列结构词、字幕组标签、年份）
    clean_result = clean_work_title_container(raw_container)
    cleaned = clean_result.title

    # 从原始容器提取年份（清洗可能已去掉年份）
    # 依次尝试从原始容器、清洗后标题提取
    year = _extract_year(raw_container)
    if year is None:
        year = _extract_year(cleaned)

    # 清洗后的标题已去掉年份，直接使用
    return cleaned, year


def _clean_local_series_group(container: str) -> str:
    """清洗本地系列合集名，去掉字幕组/压制组外壳。"""
    if not container:
        return ""
    title, _ = _parse_work_title_and_year(container)
    return title.strip()


def _is_series_container(container: str) -> bool:
    """判断作品容器是否为系列容器（包含 S1-S2 等结构词）"""
    for pat in _SERIES_CONTAINER_INDICATORS:
        if pat.search(container):
            return True
    return False


def _is_bracket_heavy(container: str) -> bool:
    """判断容器名是否以方括号标签为主（字幕组目录）

    如果方括号标签数量 >= 2，或方括号内容占容器名大部分，判定为字幕组目录。
    避免 [01-47+MOVIE] 被误判为系列范围。
    """
    brackets = re.findall(r"\[.*?\]", container)
    if len(brackets) >= 2:
        return True
    return False


def _extract_series_group_name(container: str) -> str:
    """从系列容器提取系列名

    刀剑神域.S1-S3+剧场版+外传 → 刀剑神域
    CLANNAD.S1-S2+SP+OVA → CLANNAD
    """
    # 去掉状态词
    cleaned = _clean_work_title(_strip_tmdb_hint(container))
    # 去掉年份
    year = _extract_year(cleaned)
    if year is not None:
        year_patterns = [
            re.compile(r"[.．]\d{4}$"),
            re.compile(r"[(\（]\d{4}[)\）]$"),
            re.compile(r"\s\d{4}$"),
        ]
        for pat in year_patterns:
            cleaned = pat.sub("", cleaned).strip()

    # 去掉系列结构词（S1-S2, +SP, +OVA 等）
    cleaned = re.sub(r"\.S\d+\s*[-~]\s*S\d+.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\+.*$", "", cleaned)
    cleaned = re.sub(r"\[S\d+\].*$", "", cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


# ============================================================
# 文件名级识别
# ============================================================

def _check_op_ed(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """检查是否为 OP/ED

    优先级最高。使用正则边界匹配，避免 "Redline" 被 ED 子串误判。
    """
    # 检查文件名中的 OP/ED 模式
    for pat in _OP_ED_PATTERNS:
        if pat.search(filename):
            guess = MediaGuess(
                group_type="ignored",
                card_type="main_series",
                title=_extract_op_ed_title(filename),
                reasons=[f"文件名匹配 OP/ED 模式 '{pat.pattern}'，识别为 OP/ED"],
            )
            return guess

    # 检查父目录中的 OP/ED 目录名
    for parent in parent_dirs:
        parent_clean = parent.upper().replace(" ", "").replace("＆", "&")
        for dir_pat in _OP_ED_DIR_PATTERNS:
            if dir_pat.upper().replace(" ", "").replace("＆", "&") in parent_clean:
                guess = MediaGuess(
                    group_type="ignored",
                    card_type="main_series",
                    title=_extract_op_ed_title(filename),
                    reasons=[f"父目录 {parent} 包含 OP/ED 目录名，识别为 OP/ED"],
                )
                return guess

    return None


def _extract_op_ed_title(filename: str) -> str:
    """从文件名提取 OP/ED 标题

    [MAI] EIGHTY SIX [NCOP01]... → NCOP01
    NCOP01.mkv → NCOP01
    """
    # 尝试匹配 [NCOP01] 或 NCOP01 等模式
    m = re.search(r"(NCOP\d+|NCED\d+|OP\d+|ED\d+)", filename, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"[\[【]\s*((?:OP|ED)(?:\s+[^\]】]+)?)\s*[\]】]", filename, re.IGNORECASE)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    # 尝试匹配 Opening / Ending
    m = re.search(r"(Opening|Ending)", filename, re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def _check_auxiliary(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """检查是否为附属视频（PV/CM/Menu/Trailer/Eyecatch）。"""
    for pat in _AUXILIARY_PATTERNS:
        if pat.search(filename):
            marker = _extract_auxiliary_title(filename)
            return MediaGuess(
                group_type="auxiliary",
                card_type="main_series",
                media_type="tv",
                title=marker,
                reasons=[f"文件名匹配附属视频模式 '{pat.pattern}'，识别为附属视频"],
            )

    for parent in parent_dirs:
        normalized = re.sub(r"[^A-Za-z0-9一-龥]+", "", parent or "").upper()
        if normalized in _AUXILIARY_DIR_KEYWORDS or any(kw in normalized for kw in ("预告", "菜单", "花絮")):
            return MediaGuess(
                group_type="auxiliary",
                card_type="main_series",
                media_type="tv",
                title=_extract_auxiliary_title(filename),
                reasons=[f"父目录 {parent} 为附属视频目录，识别为附属视频"],
            )
    return None


def _extract_auxiliary_title(filename: str) -> str:
    """从文件名提取附属视频标题。"""
    m = re.search(r"(PV\s*\d*|CM\s*\d*|MV\s*\d*|MENU\s*\d*|TRAILER\s*\d*|EYECATCH\s*\d*|TV\s*SPOT\s*\d*)", filename, re.IGNORECASE)
    if m:
        return re.sub(r"\s+", "", m.group(1)).upper()
    if re.search(r"菜单", filename):
        return "菜单"
    if re.search(r"预告", filename):
        return "预告"
    if re.search(r"花絮", filename):
        return "花絮"
    return ""


def _check_standalone(filename: str, parent_dirs: List[str], work_container: str) -> Optional[MediaGuess]:
    """检查是否为独立卡片（剧场版/总集篇/外传）

    只检查文件名和比作品容器更深的父目录，不从系列容器名中匹配关键词。
    """
    # 比作品容器更深的目录（排除作品容器本身）
    deeper_dirs = [d for d in parent_dirs if d != work_container]
    check_text = f"{filename} {' '.join(deeper_dirs)}"

    for keyword, (media_type, relation_type) in _STANDALONE_KEYWORDS.items():
        if keyword.lower() in check_text.lower():
            effective_media_type = media_type
            if media_type == "tv" and relation_type == "spin_off" and _looks_like_release_file(filename):
                effective_media_type = "movie"
            movie_title = ""
            if effective_media_type == "movie":
                movie_title = _extract_bracket_movie_title(filename)
            guess = MediaGuess(
                work_title=movie_title,
                title=movie_title,
                series_group=_clean_local_series_group(work_container) if movie_title else "",
                card_type="standalone",
                media_type=effective_media_type,
                group_type="movie",
                relation_type=relation_type,
                reasons=[f"包含关键词 '{keyword}'，识别为独立卡片 ({relation_type})"],
            )
            return guess

    lowered = check_text.lower()
    for keyword, display_title in _KNOWN_ANIME_MOVIE_TITLE_KEYWORDS.items():
        if keyword in lowered:
            return MediaGuess(
                work_title=display_title,
                original_title=display_title,
                card_type="standalone",
                media_type="movie",
                group_type="movie",
                relation_type="movie",
                title=display_title,
                confidence="medium",
                reasons=[f"文件名包含已知动画电影标题 '{keyword}'，识别为独立电影"],
            )

    return None


def _extract_bracket_movie_title(filename: str) -> str:
    """Extract concrete title from bracket-heavy movie filenames.

    Example:
    [BeanSub&FZSD][Jujutsu_Kaisen_0][MOVIE][BDRip]... -> Jujutsu Kaisen 0
    """
    stem = re.sub(r"\.[^.]+$", "", filename or "")
    tokens = [m.group(1).strip() for m in re.finditer(r"\[([^\]]+)\]", stem)]
    if not tokens:
        return ""
    movie_positions = [
        idx for idx, token in enumerate(tokens)
        if re.fullmatch(r"movies?", token.strip(), flags=re.IGNORECASE)
    ]
    if not movie_positions:
        return ""

    ordered_indexes: List[int] = []
    for pos in movie_positions:
        ordered_indexes.extend([pos - 1, pos + 1])
    ordered_indexes.extend(range(len(tokens)))

    seen = set()
    for idx in ordered_indexes:
        if idx < 0 or idx >= len(tokens) or idx in seen:
            continue
        seen.add(idx)
        title = _clean_bracket_movie_title_token(tokens[idx])
        if title:
            return title
    return ""


def _clean_bracket_movie_title_token(token: str) -> str:
    cleaned = (token or "").replace("_", " ").strip()
    if not cleaned:
        return ""
    lower = cleaned.casefold()
    compact = re.sub(r"[\s._-]+", "", lower)
    if lower in _MOVIE_TITLE_TECH_TOKENS or compact in _MOVIE_TITLE_TECH_TOKENS:
        return ""
    if "&" in cleaned or "＆" in cleaned:
        return ""
    if re.fullmatch(r"[A-Fa-f0-9]{6,}", cleaned):
        return ""
    if re.fullmatch(r"\d+\s*[-~]\s*\d+(?:\s*\+\s*movies?)?", cleaned, flags=re.IGNORECASE):
        return ""
    if re.fullmatch(r"\d{3,4}p", cleaned, flags=re.IGNORECASE):
        return ""
    if re.search(r"\b(?:bdrip|bluray|web[- ]?dl|webrip|hevc|avc|aac|flac|x26[45]|h\.?26[45])\b", cleaned, flags=re.IGNORECASE):
        return ""
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", cleaned):
        return ""
    return " ".join(cleaned.split()).strip(" .-_")


def _check_spin_off_episode(
    filename: str,
    parent_dirs: List[str],
    subwork_dir: str,
) -> Optional[MediaGuess]:
    """检查外传 TV 系列的正片/SP 条目。

    “外传”是独立卡片关系，但不等于 movie。若文件名本身带季集证据，
    应落到外传卡片的 Season/Special，而不是主系列 Season，也不是 movie。
    """
    focused_dirs = []
    if subwork_dir:
        focused_dirs.append(subwork_dir)
    if parent_dirs:
        focused_dirs.append(parent_dirs[-1])
    dirs_text = " ".join(focused_dirs)
    if not re.search(r"外传|spin[-\s]?off", dirs_text, re.IGNORECASE):
        return None

    for checker in (
        lambda: _check_chinese_season_episode(filename, parent_dirs),
        lambda: _check_season_episode(filename),
        lambda: _check_bracket_episode(filename, parent_dirs),
        lambda: _check_bare_episode(filename, parent_dirs),
        lambda: _check_attached_episode(filename, parent_dirs),
    ):
        guess = checker()
        if guess and guess.group_type in {"season", "special"}:
            guess.card_type = "standalone"
            guess.media_type = "tv"
            guess.relation_type = "spin_off"
            guess.work_title = _clean_standalone_dir_title(subwork_dir) or _clean_standalone_dir_title(parent_dirs[-1])
            # 外传有自己的季度与集号，分组身份不能沿用主系列，否则计划级
            # 重复集检测会把外传正片错误降级成主系列 Special。
            guess.series_group = guess.work_title
            guess.original_title = subwork_dir or parent_dirs[-1]
            guess.reasons.append("路径上下文为外传 TV 系列，识别为独立外传卡片")
            return guess

    return None


def _check_local_collection_subwork(
    filename: str,
    relative_path: str,
    parent_dirs: List[str],
    subwork_dir: str,
    work_container: str,
    local_series_container: str,
) -> Optional[MediaGuess]:
    """识别本地合集里的独立子作品。

    本地库常见结构是：
    [VCB-Studio] Yuru Camp / Heya Camp / Heya Camp [01].mkv

    根目录代表用户整理的“系列合集”，但 Heya Camp 不是 Yuru Camp 第 1 季。
    只有子目录标题和合集标题明显不同、且子目录不是显式 Season 2/第2季时，才拆成
    独立关联卡片，避免把外传/衍生短片写进主系列 Season 1。
    """
    if not local_series_container or not subwork_dir:
        return None

    subwork_title = _clean_standalone_dir_title(subwork_dir)
    series_title = _clean_local_series_group(local_series_container)
    if not subwork_title or not series_title:
        return None

    if _same_title_for_collection(subwork_title, series_title):
        return None
    if _is_explicit_series_season_subwork(subwork_title, series_title):
        return None
    if _looks_like_standalone_movie_title(subwork_title):
        return None

    base_guess = _check_path_context_special(relative_path, parent_dirs, subwork_dir, work_container)
    if not base_guess or base_guess.group_type == "movie":
        for checker in (
            lambda: _check_chinese_season_episode(filename, parent_dirs),
            lambda: _check_season_episode(filename),
            lambda: _check_bracket_episode(filename, parent_dirs),
            lambda: _check_bare_episode(filename, parent_dirs),
            lambda: _check_attached_episode(filename, parent_dirs),
            lambda: _check_sps(filename, parent_dirs),
        ):
            base_guess = checker()
            if base_guess and base_guess.group_type in {"season", "special"}:
                break

    if not base_guess or base_guess.group_type not in {"season", "special"}:
        return None

    base_guess.card_type = "standalone"
    base_guess.media_type = "tv"
    base_guess.relation_type = "spin_off"
    base_guess.work_title = subwork_title
    base_guess.original_title = subwork_dir
    base_guess.reasons.append("本地合集中的子作品标题不同，识别为独立关联作品")
    return base_guess


def _clean_standalone_dir_title(dirname: str) -> str:
    """清理独立卡片目录名前缀。"""
    if not dirname:
        return ""
    title = re.sub(r"^\d+[.．]\s*", "", dirname).strip()
    title = re.sub(r"[.．]\d{4}$", "", title).strip()
    try:
        from app.recognition.title_cleaner import clean_work_title_container

        cleaned = clean_work_title_container(_strip_tmdb_hint(title)).title
        return cleaned or title
    except Exception:
        return title


def _is_generic_movie_directory_title(title: str) -> bool:
    """判断目录名是否仅是电影分类标签，而不是可刮削的作品标题。"""
    normalized = re.sub(r"[\s._\-·:：/\\()（）【】\[\]]+", "", title or "").casefold()
    return normalized in {"剧场版", "movie", "movies", "映画", "电影"}


def _extract_release_movie_title(filename: str) -> str:
    """从发布文件名提取可用于独立电影卡片的标题。

    目录树里常见“剧场版”这类纯分类目录；此时不能把目录名直接作为
    卡片标题，优先取去掉字幕组和技术标签后的文件名主体。
    """
    stem = re.sub(r"\.[^.]+$", "", filename or "")
    # 方括号内容通常是字幕组、编码、分辨率等发布标签。
    plain = re.sub(r"[\[【][^\]】]*[\]】]", " ", stem)
    plain = plain.replace("_", " ")
    plain = re.sub(r"\s+", " ", plain).strip(" ._- ")
    if not plain or not re.search(r"[A-Za-z\u4e00-\u9fff]", plain):
        return ""
    return plain


def _normalize_collection_title(title: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (title or "").lower())


def _same_title_for_collection(left: str, right: str) -> bool:
    return _normalize_collection_title(left) == _normalize_collection_title(right)


def _is_explicit_series_season_subwork(subwork_title: str, series_title: str) -> bool:
    normalized_subwork = _normalize_collection_title(subwork_title)
    normalized_series = _normalize_collection_title(series_title)
    patterns = [
        re.compile(r"\bS(?:eason)?\s*\d+\b", re.IGNORECASE),
        re.compile(r"\b\d+(?:st|nd|rd|th)\s+Season\b", re.IGNORECASE),
        re.compile(r"第\s*\d+\s*季"),
        re.compile(r"第\s*[一二三四五六七八九十]+\s*季"),
    ]
    has_season_marker = any(pattern.search(subwork_title) for pattern in patterns)
    if not has_season_marker:
        return False
    if normalized_series and normalized_subwork.startswith(normalized_series):
        return True
    return _looks_like_plain_season_dir(subwork_title)


def _looks_like_plain_season_dir(title: str) -> bool:
    cleaned = re.sub(r"^\s*\[[^\]]+\]\s*", "", title or "").strip()
    cleaned = re.sub(r"\[[^\]]+\]", " ", cleaned).strip()
    cleaned = re.sub(r"[\s._\-·:：/\\()（）【】]+", " ", cleaned).strip()
    return bool(
        re.fullmatch(r"(?:S|Season)\s*\d+", cleaned, flags=re.IGNORECASE)
        or re.fullmatch(r"\d+(?:st|nd|rd|th)\s+Season", cleaned, flags=re.IGNORECASE)
        or re.fullmatch(r"第\s*(?:\d+|[一二三四五六七八九十]+)\s*季", cleaned)
    )


def _looks_like_standalone_movie_title(title: str) -> bool:
    lowered = (title or "").lower()
    return any(keyword in lowered for keyword in ("movie", "剧场版", "映画", "电影", "总集篇"))


def _check_movie_fallback(
    top_category: str,
    filename: str,
    work_container: str,
    year: Optional[int],
) -> Optional[MediaGuess]:
    """检查目录语义明确的电影条目。

    动画电影分类，或“带年份容器 + 文件名无季集结构”的单文件式条目，
    应当作为 movie 进入独立卡片，不应丢给人工确认。
    """
    is_movie_category = any(kw in top_category for kw in _MOVIE_CATEGORY_KEYWORDS)
    is_movie_release_bundle = (
        _is_bracket_heavy(work_container)
        and bool(re.search(r"\bmovie\b|剧场版|映画", work_container, re.IGNORECASE))
    )
    if not is_movie_category and year is None and not is_movie_release_bundle:
        return None
    if (
        not is_movie_category
        and not is_movie_release_bundle
        and str(year) not in filename
        and not _looks_like_release_file(filename)
    ):
        return None

    # 已有季集/特殊内容结构的文件，交给更高优先级规则处理。
    structured_patterns = [
        re.compile(r"S\d+\s*E\d+", re.IGNORECASE),
        re.compile(r"第\d+季"),
        re.compile(r"\s-\s\d{1,3}(?:\s|$|[\[（(【])"),
        re.compile(r"\s\d{1,3}(?:\s|$|[\[（(【])"),
        re.compile(r"\[\d+(?:\.\d+|v\d+)?\]", re.IGNORECASE),
        re.compile(r"\b(?:OP|ED|SP|OVA|OAD|PV|CM|MENU)\d*\b", re.IGNORECASE),
    ]
    if any(p.search(filename) for p in structured_patterns):
        return None

    reason = "顶层分类为动画电影，识别为独立电影"
    if is_movie_release_bundle:
        reason = "发布目录包含 MOVIE 且文件名无季集结构，识别为独立电影"
    elif not is_movie_category:
        reason = "作品容器包含年份且文件名无季集结构，识别为独立电影"

    return MediaGuess(
        card_type="standalone",
        media_type="movie",
        group_type="movie",
        relation_type="movie",
        title=work_container,
        original_title=work_container,
        confidence="medium",
        reasons=[reason],
    )


def _looks_like_release_file(filename: str) -> bool:
    """判断文件名是否带有明确发布/压制技术标签。"""
    patterns = [
        r"Ma\d+p",
        r"x26[45]",
        r"h\.?26[45]",
        r"hevc",
        r"blu[- ]?ray",
        r"web[- ]?dl",
        r"remux",
        r"truehd",
        r"flac",
        r"e-ac-3",
        r"\d{3,4}p",
    ]
    return any(re.search(p, filename, re.IGNORECASE) for p in patterns)


def _check_path_context_special(
    relative_path: str,
    parent_dirs: List[str],
    subwork_dir: str,
    work_container: str,
) -> Optional[MediaGuess]:
    """检查路径上下文中的特殊内容标记

    优先级高于文件名集号识别。
    父目录或子作品目录中的 [OVA] / [SP] / 总集篇 / 剧场版 等标记，
    会覆盖文件名中的 [23] / [24] / S02E25 等集号。

    返回:
        MediaGuess 或 None（无特殊标记时）
    """
    # 收集所有要检查的目录（排除作品容器本身）
    deeper_dirs = [d for d in parent_dirs if d != work_container]
    if subwork_dir and subwork_dir not in deeper_dirs:
        deeper_dirs.append(subwork_dir)

    # Special 目录标记只认明确目录名/括号标签，不从系列容器的范围说明里
    # 做宽松子串匹配，避免 “S1-S3+剧场版+外传” 污染下层正片。
    _SPS_DIR_KEYWORDS = [
        "OVA", "OAD", "SP", "SPS", "SPECIAL", "SPECIALS", "番外", "特典",
    ]
    for d in deeper_dirs:
        d_upper = d.upper()
        normalized_dir = re.sub(r"[\s._\-·:：/\\()（）【】\[\]]+", "", d).upper()
        if re.search(r"(番外|特典|小剧场|短篇)", d):
            guess = MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=[f"路径上下文目录 '{d}' 含中文 Special 标记，识别为 Special"],
            )
            season = _infer_season_from_context(deeper_dirs)
            if season:
                guess.season_number = season
            return guess
        for kw in _SPS_DIR_KEYWORDS:
            if (
                f"[{kw}]" in d_upper
                or f"[{kw}." in d_upper
                or normalized_dir == kw
                or re.fullmatch(rf"{re.escape(kw)}\d+", normalized_dir)
            ):
                guess = MediaGuess(
                    group_type="special",
                    card_type="main_series",
                    reasons=[f"路径上下文目录 '{d}' 含 {kw} 标记，识别为 Special"],
                )
                # 尝试从目录提取季号
                season = _infer_season_from_context(deeper_dirs)
                if season:
                    guess.season_number = season
                return guess

    # 总集篇 / 剧场版 目录标记
    _STANDALONE_DIR_KEYWORDS = {
        "总集篇": ("movie", "recap"),
        "剧场版": ("movie", "movie"),
        "movie": ("movie", "movie"),
        "映画": ("movie", "movie"),
    }
    for d in deeper_dirs:
        d_lower = d.lower()
        for keyword, (media_type, relation_type) in _STANDALONE_DIR_KEYWORDS.items():
            if keyword in d_lower:
                # 从目录名提取年份
                dir_year = _extract_year(d)
                directory_title = _clean_standalone_dir_title(d)
                movie_title = directory_title
                if relation_type == "movie" and _is_generic_movie_directory_title(directory_title):
                    movie_title = _extract_release_movie_title(relative_path.rsplit("/", 1)[-1])
                # 保留原始目录名作为标题线索
                # original_title 也保留子作品目录，供 M08 刮削使用
                guess = MediaGuess(
                    work_title=movie_title if relation_type == "movie" else "",
                    card_type="standalone",
                    media_type=media_type,
                    group_type="movie",
                    relation_type=relation_type,
                    year=dir_year,
                    title=(movie_title or d) if relation_type == "movie" else d,
                    original_title=d,  # 子作品目录也写入 original_title
                    reasons=[f"路径上下文目录 '{d}' 含 '{keyword}'，识别为独立卡片 ({relation_type})"],
                )
                return guess

    return None


def _check_explicit_filename_special(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """文件名里的明确 SP/OVA/OAD/Lite 标记优先于父目录电影上下文。

    例如剧场版目录里可能附带 [SP02] 映像特典；这种应进番剧 Special，
    不能被父目录“剧场版”提前归为电影。
    """
    if not any(pat.search(filename) for pat in _EXPLICIT_FILENAME_SPECIAL_PATTERNS):
        return None
    return _check_sps(filename, parent_dirs)


def _check_sps(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """检查是否为 Special（OVA/AD/Special/半集等）

    使用正则边界匹配，避免 "SPY x FAMILY" 被 SP 子串误判。
    """
    for pat in _SPECIAL_TITLE_PATTERNS:
        if pat.search(filename):
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=[f"文件名匹配特殊篇标题 '{pat.pattern}'，识别为 Special"],
            )

    # 检查 Special 模式
    for pat in _SPS_PATTERNS:
        if pat.search(filename):
            guess = MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=[f"文件名匹配 Special 模式 '{pat.pattern}'，识别为 Special"],
            )
            # 尝试提取 special_number
            m = re.search(r"(?:SP|OVA|OAD)\s*(\d+)", filename, re.IGNORECASE)
            if m:
                guess.special_number = int(m.group(1))
            return guess

    # 检查特殊集标记（如 Steins;Gate [23β]）
    if re.search(r"\[\d+\s*[ββ]\]", filename, re.IGNORECASE):
        return MediaGuess(
            group_type="special",
            card_type="main_series",
            reasons=["文件名包含 β 特殊集标记，识别为 Special"],
        )

    # 检查半集（11.5, 14.5 等）
    m = re.search(r"(\d+)\.5", filename)
    if m:
        half_ep = float(m.group(1) + ".5")
        guess = MediaGuess(
            group_type="special",
            card_type="main_series",
            reasons=[f"文件名包含半集 {half_ep}，识别为 Special"],
        )
        return guess

    # 检查父目录中的 Special 目录名
    for parent in parent_dirs:
        parent_upper = parent.upper().strip()
        if parent_upper in {"SPECIAL", "SPS", "SPS(1)", "SP", "SPECIALS"}:
            guess = MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=[f"父目录 {parent} 为 Special 目录，识别为 Special"],
            )
            return guess

    return None


def _check_season_episode(filename: str, parent_dirs: Optional[List[str]] = None) -> Optional[MediaGuess]:
    """检查是否为正片季集（SxxExx 模式）"""
    parent_dirs = parent_dirs or []
    # SxxExx 模式
    m = re.search(r"S(\d+)\s*E(\d+)", filename, re.IGNORECASE)
    if m:
        filename_season = int(m.group(1))
        season = filename_season
        episode = int(m.group(2))
        local_title = _extract_local_episode_title_after(filename, m.end())
        parent_override = _plain_parent_season_override(filename_season, parent_dirs)
        if parent_override is not None:
            season = parent_override
        guess = MediaGuess(
            group_type="season",
            card_type="main_series",
            season_number=season,
            episode_number=episode,
            media_type="tv",
            title=local_title,
            confidence="high",
            reasons=[f"文件名匹配 S{filename_season:02d}E{episode:02d}，识别为正片季集"],
        )
        if parent_override is not None:
            guess.reasons.append(f"父目录明确为第{season}季，覆盖文件名中的 S{filename_season:02d}")
        return guess

    return None


def _plain_parent_season_override(filename_season: int, parent_dirs: List[str]) -> Optional[int]:
    """Return a plain Season-dir override when it clearly conflicts with SxxEyy.

    Imported scraped libraries sometimes contain files named S02E01 inside a
    canonical "Season 1" folder.  The folder is safer in that case, but a
    subwork title such as "Yuru Camp Season 2" or "[S02]" remains stronger than
    the generic season folder.
    """
    plain_season = None
    for parent in reversed(parent_dirs):
        parsed = _plain_season_dir_number(parent)
        if parsed is not None:
            plain_season = parsed
            break
    if plain_season is None or plain_season == filename_season:
        return None

    for parent in parent_dirs:
        if _plain_season_dir_number(parent) is not None:
            continue
        contextual = _infer_season_from_context([parent])
        if contextual == filename_season:
            return None

    return plain_season


def _plain_season_dir_number(dirname: str) -> Optional[int]:
    name = (dirname or "").strip()
    patterns = [
        re.compile(r"^Season\s*0?([1-9]\d?)$", re.IGNORECASE),
        re.compile(r"^S0?([1-9]\d?)$", re.IGNORECASE),
        re.compile(r"^第\s*([1-9]\d?)\s*季$"),
    ]
    for pat in patterns:
        m = pat.match(name)
        if m:
            return int(m.group(1))
    return None


def _extract_local_episode_title_after(filename: str, marker_end: int) -> str:
    """Extract a useful local episode title after an SxxExx marker."""
    from app.recognition.episode_title import is_release_metadata_title

    stem = filename
    dot_idx = filename.rfind(".")
    if dot_idx > 0:
        stem = filename[:dot_idx]

    if marker_end >= len(stem):
        return ""
    title = stem[marker_end:].strip(" ._-　")
    if not title:
        return ""

    title = re.sub(r"[\[【(（][^\]】)）]*(?:\d{3,4}p|x26[45]|h\.?26[45]|hevc|aac|flac|opus|ma\d+p|hi\d+p|chs|cht|jpn|gb|big5)[^\]】)）]*[\]】)）]", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" ._-　")
    if not title:
        return ""

    normalized = re.sub(r"[\s._-]+", " ", title).strip().lower()
    if re.fullmatch(r"(?:\d{3,4}p|x26[45]|h\.?26[45]|hevc|aac|flac|opus|ma\d+p|hi\d+p|chs|cht|jpn|gb|big5)(?:\s+.*)?", normalized):
        return ""
    if re.fullmatch(r"\d{4}", normalized):
        return ""
    if is_release_metadata_title(title):
        return ""
    return title


def _check_chinese_season_episode(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """检查中文季号 + 集数模式：第1季 01、第4季 异界战争 00"""
    # 文件名中的中文季号 + 后续集号：
    # 刀剑神域 第4季 爱丽丝篇 异界战争 01 [简体内嵌].mkv
    m = re.search(r"第(\d+)季.*?(?:^|[\s._-])(\d{1,3}(?:\.\d+)?)(?=\s|[\[\(【.]|$)", filename)
    if m:
        season = int(m.group(1))
        ep_str = m.group(2)
        if "." in ep_str:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                season_number=season,
                reasons=[f"中文季号第{season}季 + 半集 {ep_str}，识别为 Special"],
            )
        episode = int(ep_str)
        if episode == 0:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                season_number=season,
                reasons=[f"中文季号第{season}季 + 第0集，识别为 Special"],
            )
        return MediaGuess(
            group_type="season",
            card_type="main_series",
            season_number=season,
            episode_number=episode,
            media_type="tv",
            reasons=[f"中文季号第{season}季 + 第{episode}集，识别为正片季集"],
        )

    # 文件名中的中文季号（允许季号和集数之间有其他文字）
    # 注意：半集用 \.\d+ 匹配（如 11.5），但 .mkv 的点号不应被匹配
    m = re.search(r"第(\d+)季\s+.*?(\d+\.\d+)\s", filename)
    if not m:
        m = re.search(r"第(\d+)季\s+.*?(\d+\.\d+)$", filename)
    if not m:
        # 整数集数（末尾或后跟空格/括号）
        m = re.search(r"第(\d+)季\s+.*?(\d+)(?:\s|[([.].*)?$", filename)
    if not m:
        # 紧凑模式：第1季01
        m = re.search(r"第(\d+)季\s*(\d+)", filename)
    if m:
        season = int(m.group(1))
        ep_str = m.group(2)
        # 半集判断
        if "." in ep_str:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                season_number=season,
                reasons=[f"中文季号第{season}季 + 半集 {ep_str}，识别为 Special"],
            )
        episode = int(ep_str)
        if episode == 0:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                season_number=season,
                reasons=[f"中文季号第{season}季 + 第0集，识别为 Special"],
            )
        return MediaGuess(
            group_type="season",
            card_type="main_series",
            season_number=season,
            episode_number=episode,
            media_type="tv",
            reasons=[f"中文季号第{season}季 + 第{episode}集，识别为正片季集"],
        )

    # 父目录中的中文季号（不带集数，文件名中可能有方括号集数）
    for parent in parent_dirs:
        m = re.search(r"第(\d+)季", parent)
        if m:
            season = int(m.group(1))
            # 检查文件名中的方括号集数
            em = re.search(r"\[(\d+\.?\d*)\]", filename)
            if em:
                ep_str = em.group(1)
                if "." in ep_str:
                    return MediaGuess(
                        group_type="special",
                        card_type="main_series",
                        season_number=season,
                        reasons=[f"父目录含中文季号第{season}季 + 文件名半集 {ep_str}，识别为 Special"],
                    )
                episode = int(ep_str)
                if episode == 0:
                    return MediaGuess(
                        group_type="special",
                        card_type="main_series",
                        season_number=season,
                        reasons=[f"父目录含中文季号第{season}季 + 第0集，识别为 Special"],
                    )
                return MediaGuess(
                    group_type="season",
                    card_type="main_series",
                    season_number=season,
                    episode_number=episode,
                    media_type="tv",
                    reasons=[f"父目录含中文季号第{season}季 + 文件名 [{episode}]，识别为正片季集"],
                )
            # 没有集数，只有季号
            # 不在这里 needs_review 返回，而是 return None，
            # 让后续 _check_bare_episode / _check_bracket_episode
            # 等集号检查器继续尝试提取集数。若全失败，由最后的
            # placement_validator 兜底标记需要人工确认。
            return None

    return None


def _check_bare_episode(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """检查裸集数模式

    支持：
    - Silent Witch - 01 [BDRip...].mkv → season=1, episode=1
    - One Room S2 01 (BDRip...).mkv → season=2, episode=1
    - One Room 01 (BDRip...).mkv → season=1, episode=1
    - Title - 12.mkv → season=1, episode=12
    """
    # 去掉扩展名
    stem = filename
    dot_idx = filename.rfind(".")
    if dot_idx > 0:
        stem = filename[:dot_idx]

    terminal_status = r"(?:\s+(?:END|FINAL|FIN|完|完结|終|终))?"

    # 模式 0: 纯数字文件名。真实本地库可能存在 8.mkv / 9.mkv 这类
    # 临时命名；只接受整个 stem 为数字，避免把标题或年份误当集数。
    if re.fullmatch(r"\d+\.?\d*", stem):
        ep_str = stem
        if "." in ep_str:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=[f"文件名为纯半集 {ep_str}，识别为 Special"],
            )
        season, episode, confidence, reason = _normalize_bare_episode_number(ep_str, parent_dirs, filename)
        if episode == 0:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=["文件名为第0集，识别为 Special"],
            )
        return MediaGuess(
            group_type="season",
            card_type="main_series",
            season_number=season,
            episode_number=episode,
            media_type="tv",
            confidence=confidence,
            reasons=[f"文件名为纯裸集数 {ep_str}，{reason}"],
        )

    # 模式 1: " - NN"（减号分隔，后面可跟 END/FINAL/括号/方括号/结尾）
    m = re.search(rf"\s-\s(\d+\.?\d*){terminal_status}(?:\s*[(\[].*)?$", stem, re.IGNORECASE)
    if m:
        ep_str = m.group(1)
        if "." in ep_str:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=[f"文件名包含半集 - {ep_str}，识别为 Special"],
            )
        season, episode, confidence, reason = _normalize_bare_episode_number(ep_str, parent_dirs, filename)
        if episode == 0:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=["文件名包含 - 0，识别为 Special"],
            )
        return MediaGuess(
            group_type="season",
            card_type="main_series",
            season_number=season,
            episode_number=episode,
            media_type="tv",
            confidence=confidence,
            reasons=[f"文件名匹配裸集数 - {ep_str}，{reason}"],
        )

    # 模式 2: 空格+数字，后面跟 END/FINAL/空格/括号/方括号/结尾
    # 用 (?:\s*[(\[].*)?$ 允许数字后跟 (BDRip...) 或 [tags]
    m = re.search(rf"\s(\d+\.?\d*){terminal_status}(?:\s*[(\[].*)?$", stem, re.IGNORECASE)
    if m:
        prefix = stem[:m.start(1)].rstrip().lower()
        if re.search(r"(?:season|第)\s*$", prefix):
            return None
        ep_str = m.group(1)
        # 半集
        if "." in ep_str:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=[f"文件名包含半集 {ep_str}，识别为 Special"],
            )
        season, episode, confidence, reason = _normalize_bare_episode_number(ep_str, parent_dirs, filename)
        # 第0集
        if episode == 0:
            return MediaGuess(
                group_type="special",
                card_type="main_series",
                reasons=["文件名包含第0集，识别为 Special"],
            )
        return MediaGuess(
            group_type="season",
            card_type="main_series",
            season_number=season,
            episode_number=episode,
            media_type="tv",
            confidence=confidence,
            reasons=[f"文件名匹配裸集数 {ep_str}，{reason}"],
        )

    return None


def _normalize_bare_episode_number(ep_str: str, parent_dirs: List[str], filename: str = "") -> tuple[int, int, str, str]:
    """Normalize bare numeric episode names using path context and SSEE forms.

    Examples:
    - parent [S02] + 02 -> S02E02
    - 102 -> S01E02
    - 203 -> S02E03
    """
    parent_season = _infer_season_from_context(parent_dirs)
    filename_season = _infer_explicit_s_marker(filename)
    inferred_season = parent_season or filename_season
    if re.fullmatch(r"[1-9]\d{2}", ep_str):
        encoded_season = int(ep_str[0])
        encoded_episode = int(ep_str[1:])
        if encoded_episode > 0:
            season = inferred_season or encoded_season
            confidence = "high"
            if parent_season:
                return season, encoded_episode, confidence, f"父目录确认第{season}季，三位格式 {ep_str} 取第{encoded_episode}集"
            if filename_season:
                return season, encoded_episode, confidence, f"文件名 S{season} 确认第{season}季，三位格式 {ep_str} 取第{encoded_episode}集"
            return season, encoded_episode, confidence, f"三位格式 {ep_str} 识别为第{season}季第{encoded_episode}集"

    episode = int(ep_str)
    season = inferred_season or 1
    confidence = "high" if inferred_season else "medium"
    if parent_season:
        return season, episode, confidence, f"从父目录确认第{season}季第{episode}集"
    if filename_season:
        return season, episode, confidence, f"从文件名 S{season} 确认第{season}季第{episode}集"
    return season, episode, confidence, f"从上下文推断为第{season}季第{episode}集"


def _check_attached_episode(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """检查标题与集数直接相连的模式。

    例如：上伊那牡丹醉姿如百合09.mp4 → S01E09。
    只处理中文标题 + 2/3 位结尾数字，避免把年份或技术参数当集数。
    """
    stem = filename
    dot_idx = filename.rfind(".")
    if dot_idx > 0:
        stem = filename[:dot_idx]

    m = re.search(r"[\u4e00-\u9fff].*?(\d{2,3})$", stem)
    if not m:
        return None

    episode = int(m.group(1))
    if episode == 0:
        return MediaGuess(
            group_type="special",
            card_type="main_series",
            reasons=["文件名末尾为第0集，识别为 Special"],
        )

    season = _infer_season_from_context(parent_dirs) or _infer_explicit_s_marker(filename) or 1
    return MediaGuess(
        group_type="season",
        card_type="main_series",
        season_number=season,
        episode_number=episode,
        media_type="tv",
        confidence="medium",
        reasons=[f"文件名标题后直接跟集数 {episode:02d}，从上下文推断为第{season}季"],
    )


def _check_bracket_episode(filename: str, parent_dirs: List[str]) -> Optional[MediaGuess]:
    """检查方括号集数模式：[01]"""
    ep_str = _find_bracket_episode_token(filename)
    if not ep_str:
        return None

    # 半集
    if "." in ep_str:
        return MediaGuess(
            group_type="special",
            card_type="main_series",
            reasons=[f"文件名包含半集 [{ep_str}]，识别为 Special"],
        )

    episode = int(ep_str)

    # 第0集
    if episode == 0:
        return MediaGuess(
            group_type="special",
            card_type="main_series",
            reasons=[f"文件名包含第0集 [{episode}]，识别为 Special"],
        )

    # 尝试从父目录或作品容器推断季号
    season = _infer_season_from_context(parent_dirs) or _infer_explicit_s_marker(filename)
    if season is not None:
        return MediaGuess(
            group_type="season",
            card_type="main_series",
            season_number=season,
            episode_number=episode,
            media_type="tv",
            reasons=[f"文件名包含 [{episode}]，从上下文推断为第{season}季"],
        )

    # 无法推断季号，默认第1季（正常行为，不报 warning）
    return MediaGuess(
        group_type="season",
        card_type="main_series",
        season_number=1,
        episode_number=episode,
        media_type="tv",
        confidence="medium",
        reasons=[f"文件名包含 [{episode}]，默认为第1季"],
    )


def _find_bracket_episode_token(filename: str) -> str:
    """从窄白名单方括号 token 中提取集数。

    字幕组命名常把作品名、集数、技术参数都放进多个方括号里，例如：
    【Top o Nerae! GunBuster】【01】【BDrip】【HEVC 2880x2160p FLAC】。
    这里只接受“纯集数 + 少量正片后缀”，避免把技术参数或季度范围误当集数。
    """
    for match in re.finditer(r"\[([^\[\]]+)\]|【([^【】]+)】", filename):
        token = (match.group(1) or match.group(2) or "").strip()
        episode_match = re.fullmatch(
            r"(\d{1,3}(?:\.\d+)?)(?:\s*(?:v\d+|END|FINAL|FIN|Non\s+Trimming\s+Ver\.?))?",
            token,
            flags=re.IGNORECASE,
        )
        if episode_match:
            return episode_match.group(1)
    return ""


def _infer_season_from_context(parent_dirs: List[str]) -> Optional[int]:
    """从父目录上下文推断季号"""
    # 子目录中的 S2/S02 是最强证据，优先于用于排序的“3.”前缀。
    for parent in reversed(parent_dirs):
        explicit_marker = _infer_explicit_s_marker(parent)
        if explicit_marker is not None:
            return explicit_marker
    # 用户整理的系列容器常用 1./2./3./4. 给子作品排序。只有外层明确
    # 声明为多季合集时才采用，避免把普通编号目录误判成季度。
    if any(_is_series_container(parent) for parent in parent_dirs):
        for parent in reversed(parent_dirs):
            m = re.match(r"^\s*([1-9]\d?)\s*[.．、_-]", parent)
            if m:
                return int(m.group(1))
    for parent in parent_dirs:
        # [S1], [S01]
        m = re.search(r"\[S(\d+)\]", parent, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # 第x季
        m = re.search(r"第(\d+)季", parent)
        if m:
            return int(m.group(1))
        # Season x
        m = re.search(r"Season\s*(\d+)", parent, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # Title S2 [tags] — 字幕组目录中的 S2 标记
        m = re.search(r"(?:^|\s)[Ss](\d+)(?:\s|$|\[)", parent)
        if m:
            return int(m.group(1))
        # 罗马数字季号：魔法禁书目录 I.2008 / II.2010 / III.2018
        m = re.search(r"\b(III|II|I)\b", parent)
        if m:
            roman_map = {"I": 1, "II": 2, "III": 3}
            return roman_map[m.group(1)]
        season = _infer_numbered_title_season(parent)
        if season is not None:
            return season
    return None


def _infer_explicit_s_marker(text: str) -> Optional[int]:
    """从明确 S2/S02 标记推断季号，不处理标题末尾裸数字。"""
    if not text:
        return None
    cleaned = re.sub(
        r"(?<![A-Za-z0-9])[Ss]0?[1-9]\d?\s*[-~]\s*[Ss]0?[1-9]\d?(?![A-Za-z0-9])",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    for m in re.finditer(r"(?<![A-Za-z0-9])[Ss]0?([1-9]\d?)(?![A-Za-z0-9])", cleaned):
        return int(m.group(1))
    return None


def _infer_numbered_title_season(parent: str) -> Optional[int]:
    """从字幕组季度目录推断季号。

    常见目录：
    [VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 2 [Ma10p_1080p]
    KonoSuba [2] [BDRip]

    这里仅处理父目录，不处理文件名里的 [06]，避免把集数误认为季号。
    """
    if not parent:
        return None

    for content in re.findall(r"\[([^\]]+)\]", parent):
        token = content.strip()
        if re.fullmatch(r"[2-9]", token):
            return int(token)

    cleaned = re.sub(r"\[[^\]]+\]", " ", parent)
    cleaned = re.sub(r"\{[^}]+\}", " ", cleaned)
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    m = re.search(r"(?:^|[\s._\-!！:：])([2-9])\s*$", cleaned)
    if not m:
        return None
    return int(m.group(1))


def _check_parent_dir_season(parent_dirs: List[str]) -> Optional[int]:
    """从父目录提取季号（用于补充推断）"""
    return _infer_season_from_context(parent_dirs)


# ============================================================
# 主识别函数
# ============================================================

def recognize_media(
    filename: str,
    relative_path: str,
    source: str = "pan115",
    existing_work_title: str = "",
    existing_year: Optional[int] = None,
    root_container: str = "",
) -> MediaGuess:
    """识别单个视频文件的媒体结构

    优先级：OP/ED ignored > 独立卡片 > Special > Season

    参数:
        filename: 文件名，如 "AIR.S01E01.微风～breeze～.mkv"
        relative_path: 相对路径，如 "动画/AIR.2005/AIR.S01E01.微风～breeze～.mkv"
        source: 来源标识
        existing_work_title: 已有的作品名（如从父目录已识别）
        existing_year: 已有的年份
        root_container: 选中目录名（OpenList 无作品容器层时的系列名候选）

    返回:
        MediaGuess 识别结果
    """
    # 提取路径上下文
    work_container = _extract_work_container(relative_path, source)
    _extract_parent_dir(relative_path)
    parent_dirs = _extract_parent_dirs(relative_path)
    subwork_dir = _extract_subwork_dir(relative_path, source)
    top_category = _extract_top_category(relative_path, source)
    local_series_container = _extract_local_series_container(relative_path, source)
    tmdb_hint_id, tmdb_hint_type = _extract_tmdb_hint(relative_path)
    if tmdb_hint_id is None:
        from app.recognition.verified_titles import match_verified_tmdb_binding

        verified_binding = match_verified_tmdb_binding(relative_path)
        if verified_binding:
            tmdb_hint_id = verified_binding.tmdb_id
            tmdb_hint_type = verified_binding.tmdb_type

    # 保存清洗前的原始容器名作为 original_title
    original_title = _strip_tmdb_hint(work_container)

    # 解析作品名和年份
    if existing_work_title:
        work_title = existing_work_title
        year = existing_year
        clean_warnings = []
        clean_needs_review = False
    else:
        from app.recognition.title_cleaner import clean_work_title_container
        # 路径中无作品容器（root 直属文件 / 结构段开头）时，从文件名本身
        # 提取系列名（集号 token 前的标题），保证识别普适、不依赖路径层级。
        container_for_parse = work_container
        if not container_for_parse:
            container_for_parse = _extract_series_name_from_filename(filename)
        clean_result = clean_work_title_container(_strip_tmdb_hint(container_for_parse))
        work_title, year = _parse_work_title_and_year(container_for_parse)
        clean_warnings = clean_result.warnings
        clean_needs_review = clean_result.needs_review

    # 检查是否为系列容器
    # 用清洗后的标题判断，避免字幕组标签中的 [01-47+MOVIE] 误判
    series_group = ""
    if _is_series_container(work_container) and not _is_bracket_heavy(work_container):
        # 清洗后的标题就是系列名
        series_group = work_title
    elif local_series_container:
        series_group = _clean_local_series_group(local_series_container)
    elif root_container and not _is_generic_category_name(root_container):
        # OpenList 选中目录即系列容器：无论有无作品子目录（S1/S2 季目录
        # 或 1.天才/2.阶梯 等作品子目录），series_group 统一用选中目录名，
        # 同一系列的季/剧集归同一卡片；通用分类名（动画/剧集等）除外。
        series_group = _parse_work_title_and_year(root_container)[0]

    # 按优先级检查：OP/ED ignored > auxiliary > 独立卡片 > Special > Season
    guess = None

    # 1. OP/ED
    guess = _check_op_ed(filename, parent_dirs)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 2. 附属视频（PV/CM/Menu/Trailer/Eyecatch）不能落进 Special/S00
    guess = _check_auxiliary(filename, parent_dirs)
    if guess:
        _attach_local_collection_subwork_identity(guess, subwork_dir, local_series_container)
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 3. 本地合集中的独立子作品（例如 Yuru Camp 合集里的 Heya Camp）
    guess = _check_local_collection_subwork(
        filename,
        relative_path,
        parent_dirs,
        subwork_dir,
        work_container,
        local_series_container,
    )
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 4. 已通过官方资料/TMDB 核实的系列特别篇，优先于“总集篇=电影”等通用弱规则。
    from app.recognition.verified_titles import match_verified_series_special

    verified_special = match_verified_series_special(relative_path, filename)
    if verified_special:
        guess = MediaGuess(
            work_title=verified_special.series_title,
            original_title=subwork_dir or original_title,
            series_group=verified_special.series_title,
            belongs_to_series=verified_special.series_title,
            card_type="main_series",
            media_type="tv",
            tmdb_hint_id=verified_special.tmdb_id,
            tmdb_hint_type="tv",
            group_type="special",
            season_number=0,
            special_number=verified_special.special_number,
            title=verified_special.title,
            confidence="high",
            reasons=[verified_special.reason],
        )
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 5. 文件名明确 SP/OVA/OAD/Lite 时，先按 Special 处理，避免被“剧场版”目录抢先归为电影
    guess = _check_explicit_filename_special(filename, parent_dirs)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 6. 路径上下文特殊内容（[OVA]/[SP]/总集篇/剧场版 目录标记，优先级高于集号）
    guess = _check_path_context_special(relative_path, parent_dirs, subwork_dir, work_container)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 6. 外传 TV 系列（独立卡片，但内部仍按 Season/Special）
    guess = _check_spin_off_episode(filename, parent_dirs, subwork_dir)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 7. 独立卡片（文件名关键词）
    guess = _check_standalone(filename, parent_dirs, work_container)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 8. Special（文件名关键词）
    guess = _check_sps(filename, parent_dirs)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 9. 电影兜底先于裸集数/标题尾号，避免 声之形.2016.mkv 被当成第 16 集。
    guess = _check_movie_fallback(top_category, filename, work_container, year)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 10. Season（SxxExx）
    guess = _check_season_episode(filename, parent_dirs)
    if guess:
        # 补充季号推断
        if guess.season_number is None:
            parent_season = _check_parent_dir_season(parent_dirs)
            if parent_season is not None:
                guess.season_number = parent_season
                guess.reasons.append(f"从父目录推断季号为 {parent_season}")
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 11. 中文季号
    guess = _check_chinese_season_episode(filename, parent_dirs)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 12. 方括号集数
    guess = _check_bracket_episode(filename, parent_dirs)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 13. 裸集数（Title - 01、Title 01、Title S2 01）
    guess = _check_bare_episode(filename, parent_dirs)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 14. 标题与集数直接相连（中文标题09）
    guess = _check_attached_episode(filename, parent_dirs)
    if guess:
        _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type)
        return guess

    # 15. 无法识别分组，但可能有作品名
    guess = MediaGuess(
        work_title=work_title,
        year=year,
        series_group=series_group or work_title,
        confidence="low",
        needs_review=True,
        warnings=["无法识别分组类型（Season/Special/Movie）"],
    )
    if work_title:
        guess.work_id = _make_work_id(source, work_title, year, "")
        guess.reasons.append(f"识别作品名为 {work_title}，但无法确定分组")
    else:
        guess.warnings.append("无法识别作品名")
    _enrich_guess(guess, source, work_title, year, original_title, series_group, subwork_dir, clean_warnings, clean_needs_review, tmdb_hint_id, tmdb_hint_type, skip_finalize=True)
    return guess


def _enrich_guess(
    guess: MediaGuess,
    source: str,
    work_title: str,
    year: Optional[int],
    original_title: str,
    series_group: str,
    subwork_dir: str,
    clean_warnings: List[str],
    clean_needs_review: bool,
    tmdb_hint_id: Optional[int] = None,
    tmdb_hint_type: str = "",
    skip_finalize: bool = False,
) -> None:
    """为 guess 填充公共字段：work_title、year、original_title、series_group、清洗结果

    顺序：先算有效年份（含子作品回退），再生成 work_id，最后 finalize。
    """
    # 1. 先算有效年份（含子作品目录回退）
    effective_year = year
    if effective_year is None and subwork_dir:
        subwork_year = _extract_year_from_subwork(subwork_dir)
        if subwork_year is not None:
            effective_year = subwork_year

    if guess.group_type == "special":
        base_special_title = _split_known_special_work_title(guess.work_title or work_title)
        if base_special_title and base_special_title != (guess.work_title or work_title):
            guess.work_title = base_special_title
            guess.series_group = base_special_title
            guess.card_type = guess.card_type or "main_series"
            guess.reasons.append("作品名包含已知特别篇副标题，归入主系列 Special")

    # 2. 设置基本字段（独立外传等可预先设置 work_title）
    effective_work_title = _strip_tmdb_hint(guess.work_title or work_title)
    guess.work_title = effective_work_title
    guess.year = effective_year
    if guess.group_type == "special":
        guess.season_number = 0
        guess.episode_number = None
        guess.media_type = guess.media_type or "tv"
    elif guess.group_type == "auxiliary":
        guess.season_number = 0
        guess.episode_number = None
        guess.media_type = guess.media_type or "tv"
    if not guess.original_title:
        guess.original_title = _strip_tmdb_hint(original_title)
    guess.series_group = _strip_tmdb_hint(guess.series_group or series_group or effective_work_title)
    guess.tmdb_hint_id = guess.tmdb_hint_id or tmdb_hint_id
    guess.tmdb_hint_type = guess.tmdb_hint_type or tmdb_hint_type

    # 3. 用有效年份生成 work_id
    guess.work_id = _make_work_id(source, effective_work_title, effective_year, guess.card_type)
    if series_group:
        guess.belongs_to_series = series_group

    # 4. 传递清洗结果
    if clean_needs_review:
        guess.needs_review = True
    for w in clean_warnings:
        if w not in guess.warnings:
            guess.warnings.append(w)

    # 5. 子作品目录作为刮削线索（写入 reasons 而非 warnings，避免噪音）
    if subwork_dir:
        guess.reasons.append(f"子作品目录: {subwork_dir}")
        if year is None and effective_year is not None:
            guess.reasons.append("年份来自子作品目录")

    # 6. finalize（用有效年份，避免误报"未识别到年份"）
    if not skip_finalize:
        _finalize_guess(guess, effective_work_title, effective_year)


def _split_known_special_work_title(title: str) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        return ""
    for pat in _KNOWN_SPECIAL_SUBTITLE_PATTERNS:
        base = pat.sub("", cleaned).strip(" ._-:：")
        if base and base != cleaned:
            return base
    return cleaned


def _attach_local_collection_subwork_identity(
    guess: MediaGuess,
    subwork_dir: str,
    local_series_container: str,
) -> None:
    """把本地合集里的附属视频归到具体子作品卡片。"""
    if not local_series_container or not subwork_dir:
        return
    subwork_title = _clean_standalone_dir_title(subwork_dir)
    series_title = _clean_local_series_group(local_series_container)
    if not subwork_title or not series_title:
        return
    if _same_title_for_collection(subwork_title, series_title):
        return
    if _is_explicit_series_season_subwork(subwork_title, series_title):
        return
    if _looks_like_standalone_movie_title(subwork_title):
        return
    guess.card_type = "standalone"
    guess.media_type = "tv"
    guess.relation_type = "spin_off"
    guess.work_title = subwork_title
    guess.original_title = subwork_dir
    guess.reasons.append("本地合集中的子作品标题不同，附属视频归到独立关联作品")


def _finalize_guess(guess: MediaGuess, work_title: str, year: Optional[int]) -> None:
    """最终化 guess：补充置信度和 work_id"""
    # 置信度调整
    if not work_title:
        guess.confidence = "low"
        guess.needs_review = True
        guess.warnings.append("无法识别作品名")
    elif year is None and guess.confidence == "high":
        # 年份缺失但其他识别明确（如 SxxExx），降为 medium
        guess.confidence = "medium"
    elif year is None and guess.confidence != "low":
        guess.confidence = "medium"

    # 确保 work_id 已生成
    if not guess.work_id:
        guess.work_id = _make_work_id("", work_title, year, guess.card_type)
