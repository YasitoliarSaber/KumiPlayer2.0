# -*- coding: utf-8 -*-
"""TMDB 强绑定提示的单点解析。

`{tmdb-123}` / `{tmdbid=123}` / `[tmdbid=123]` 家族（`{}` / `[]` 包裹、
连字符/等号/冒号分隔、大小写不敏感）在刮削、识别、LibraryIndex 各链路
必须解析一致。历史上同族正则在多处复制后发生漂移（方括号、等号分隔、
裸格式的覆盖各不相同），本模块是唯一权威实现，其余位置一律引用。
"""

import re
from typing import Optional

TMDB_HINT_STRIP_PATTERN = re.compile(
    r"\s*[\{\[]\s*(?:tmdb|tmdbid)\s*[-_=：:]?\s*\d+\s*[\}\]]\s*",
    re.IGNORECASE,
)
TMDB_HINT_EXTRACT_PATTERN = re.compile(
    r"[\{\[]\s*(?:tmdb|tmdbid)\s*[-_=：:]?\s*(\d+)\s*[\}\]]",
    re.IGNORECASE,
)


def strip_tmdb_hint(title: str) -> str:
    """剥离标题中的 TMDB 强绑定提示并压缩空白。"""
    cleaned = TMDB_HINT_STRIP_PATTERN.sub(" ", title or "")
    return " ".join(cleaned.split()).strip()


def extract_tmdb_hint(title: str) -> Optional[int]:
    """提取标题中的 TMDB 强绑定 ID；无提示时返回 None。"""
    match = TMDB_HINT_EXTRACT_PATTERN.search(title or "")
    if not match:
        return None
    return int(match.group(1))
