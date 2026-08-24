"""共享“非作品容器”分类器。

同一套规则供 recognizer、resolver 身份门禁、structural key 与刮削目标
门禁复用，禁止各处维护不同的 generic 集合。判断对象是目录名/路径段或
候选作品标题时，应视为“结构容器”，不能成为正式 Work 身份。
"""

from __future__ import annotations

import re
import unicodedata

_GENERIC_TOKENS = frozenset({
    # 英文结构容器
    "season",
    "seasons",
    "specials",
    "special",
    "sp",
    "sps",
    "sp01",
    "extras",
    "bonus",
    "menu",
    "trailers",
    "previews",
    "pv",
    "cm",
    "nced",
    "ncop",
    "op",
    "ed",
    "unknown",
    "untitled",
    "n/a",
    "none",
    "null",
    # 中文分类/结构容器
    "动画",
    "新番",
    "电影",
    "剧场版",
    "剧集",
    "动画电影",
    "特典",
    "花絮",
    "预告",
    "预告片",
    "正片",
    "番外",
    "特别篇",
    "未分类",
    "其他",
})

# S01 / S1 / Season 1 / Season1 / 第1季 / 第 1 季
_SEASON_TOKEN = re.compile(r"^(?:season\s*)?s?\d{1,2}$", re.IGNORECASE)
_CHINESE_SEASON_TOKEN = re.compile(r"^第\s*\d{1,3}\s*季$")
# 纯数字（集数/年份之外的目录名，保守只拦 1-3 位数字）
_PURE_NUMBER = re.compile(r"^\d{1,3}$")
# 集数范围 E01-E24 / 01-24
_EPISODE_RANGE = re.compile(
    r"^(?:e\d{1,3}\s*[-~]\s*e?\d{1,3}|\d{1,3}\s*[-~]\s*\d{1,3})$",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"\s+", " ", value).strip().strip(" ._-·:：/\\()（）【】[]{}<>《》「」『』\"'")


def is_generic_container_name(value: str) -> bool:
    """判断目录名/路径段是否为通用结构容器。

    空值、Season 目录、Specials、分类目录、纯数字或集数范围都算容器。
    """
    raw = (value or "").strip().rstrip("/\\")
    if not raw:
        return True
    lower = raw.casefold()
    if lower in _GENERIC_TOKENS:
        return True
    if _SEASON_TOKEN.fullmatch(lower):
        return True
    if _CHINESE_SEASON_TOKEN.fullmatch(raw):
        return True
    if _PURE_NUMBER.fullmatch(lower):
        return True
    if _EPISODE_RANGE.fullmatch(lower):
        return True
    return False


def is_generic_container_title(value: str) -> bool:
    """判断候选作品标题是否为通用容器标题（含规范化后的占位词）。"""

    normalized = _normalize(value)
    if not normalized:
        return True
    return is_generic_container_name(normalized)
