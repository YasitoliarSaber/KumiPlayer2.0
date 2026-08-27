"""从不可变来源路径提取可展示的本地剧集标题。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.recognition.episode_title import is_release_metadata_title

_SPECIAL_CODE = re.compile(
    r"(?i)(?:S00\s*E\s*0*(\d+)|(?:SP|OVA|OAD|OAV)\s*0*(\d+))"
)
_SPECIAL_NUMBER_PATTERNS = (
    re.compile(r"(?i)S00\s*E\s*0*(\d+)"),
    re.compile(r"(?i)SP\s*0*(\d+)"),
    re.compile(r"(?i)(?:OVA|OAD|OAV)\s*0*(\d+)"),
)
_TECHNICAL_BRACKET = re.compile(
    r"(?i)(?:\d{3,4}p|\d{3,4}x\d{3,4}|x26[45]|h\.?26[45]|hevc|avc|"
    r"flac|aac|opus|bdrip|bluray|web[-_. ]?dl|webrip|remux|ma\d+p|hi\d+p|"
    r"10bit|8bit|chs|cht|jpn|eng|gb|big5)"
)
_TECHNICAL_SUFFIX_TOKEN = re.compile(
    r"(?i)(?:\d{3,4}p|\d{3,4}x\d{3,4}|x26[45]|h\.?26[45]|hevc|avc|"
    r"flac|aac|opus|bdrip|bluray|web[-_]?dl|webrip|remux|ma\d+p|hi\d+p|"
    r"10bit|8bit|chs|cht|jpn|eng|gb|big5|[0-9a-f]{8,})"
)


def clean_special_episode_title(
    relative_path: str,
    *,
    work_title: str = "",
    original_title: str = "",
    series_group: str = "",
    special_number: int | None = None,
) -> str:
    """轻量清理特别篇文件名，保留可区分标题与 SP/OVA 编号。

    这里只删除扩展名、开头发布组、作品名前缀和纯技术参数。不会把未知
    词语、半集编号、OVA/SP 标记或用户整理的副标题当噪音删除。
    """

    normalized_path = (relative_path or "").replace("\\", "/")
    stem = PurePosixPath(normalized_path).stem.strip()
    if not stem:
        return _special_fallback(special_number)

    title = stem
    # 旧版成熟行为：开头连续方括号通常是发布组；只处理开头，不删除
    # 标题中间可能有语义的方括号。
    while True:
        cleaned = re.sub(r"^\s*(?:\[[^\]]+\]|【[^】]+】)\s*", "", title, count=1)
        if cleaned == title:
            break
        title = cleaned.strip()

    title = _strip_work_prefix(title, (work_title, original_title, series_group))
    title = _strip_trailing_technical_blocks(title)
    title = _strip_trailing_technical_suffixes(title)
    title = title.replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip(" ._-:：·")

    if not title or is_release_metadata_title(title):
        return _special_fallback(special_number)
    return title


def extract_special_episode_number(title: str) -> int | None:
    """优先从 S00E/SP/OVA/OAD 标记提取本地特别篇编号。"""

    for pattern in _SPECIAL_NUMBER_PATTERNS:
        match = pattern.search(title or "")
        if match:
            return int(match.group(1))
    return None


def ensure_special_title_number(title: str, special_number: int | None) -> str:
    """给没有显式编号的特别篇标题补稳定编号，避免不同条目显示同名。"""

    cleaned = " ".join((title or "").split()).strip(" ._-:：·")
    if special_number is None or special_number <= 0:
        return cleaned or "特别篇"
    fallback = f"SP{special_number:02d}"
    marker = _SPECIAL_CODE.search(cleaned)
    if marker:
        marker_number = next((int(value) for value in marker.groups() if value), 0)
        if marker_number > 0:
            return cleaned
        return f"{cleaned[:marker.start()]}{fallback}{cleaned[marker.end():]}".strip()
    return f"{fallback} · {cleaned}" if cleaned and cleaned != "特别篇" else fallback


def is_special_marker_only(title: str) -> bool:
    """判断标题是否只有 SP/OVA/S00E 编号，可被更具体的刮削标题替代。"""

    compact = re.sub(r"[\s._\-:：·]+", "", title or "")
    return bool(re.fullmatch(r"(?i)(?:SP|OVA|OAD|OAV)0*\d+|S00E0*\d+", compact))


def is_generic_special_title(title: str) -> bool:
    compact = re.sub(r"[\s._\-:：·]+", "", title or "").casefold()
    return compact in {"special", "specials", "sp", "sps", "特别篇", "番外篇", "特典"}


def _special_fallback(special_number: int | None) -> str:
    return f"SP{special_number:02d}" if special_number and special_number > 0 else "特别篇"


def _strip_work_prefix(title: str, candidates: tuple[str, ...]) -> str:
    current = title.strip()
    for candidate in sorted({item.strip() for item in candidates if item.strip()}, key=len, reverse=True):
        match = re.match(re.escape(candidate), current, flags=re.IGNORECASE)
        if not match:
            continue
        remainder = current[match.end() :]
        # 只有完整前缀边界才移除，避免 Show 误伤 Showdown。
        if remainder and remainder[0].isalnum():
            continue
        current = remainder.strip(" ._-:：·")
        break
    return current


def _strip_trailing_technical_blocks(title: str) -> str:
    current = title.strip()
    bracket_pattern = re.compile(r"\s*(?:\[([^\]]+)\]|【([^】]+)】|\(([^)]+)\)|（([^）]+)）)\s*$")
    while True:
        match = bracket_pattern.search(current)
        if not match:
            break
        token = next((value for value in match.groups() if value is not None), "").strip()
        if not (_TECHNICAL_BRACKET.search(token) or is_release_metadata_title(token)):
            break
        current = current[: match.start()].rstrip()
    return current


def _strip_trailing_technical_suffixes(title: str) -> str:
    """清理点分隔的年份/分辨率/压制信息，不碰自然语言副标题。"""

    current = re.sub(r"[\s._-]+20\d{6}[_-]\d{6}$", "", title.strip())
    removed_technical = False
    while True:
        match = re.search(r"[\s._-]+([^\s._-]+)$", current)
        if not match:
            break
        token = match.group(1)
        if _TECHNICAL_SUFFIX_TOKEN.fullmatch(token):
            current = current[: match.start()].rstrip()
            removed_technical = True
            continue
        if removed_technical and re.fullmatch(r"(?:19|20)\d{2}", token):
            current = current[: match.start()].rstrip()
            continue
        break
    return current
