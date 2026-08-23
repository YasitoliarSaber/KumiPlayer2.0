# -*- coding: utf-8 -*-
"""Stable card identity boundaries shared by scraping and LibraryIndex."""

import os
import re
from pathlib import Path

from app.import_plan.models import ImportPlanItem

#: 自动派生的 unit 级 / 文件级 canonical（durable pipeline 早期格式）。
#: 只有这两种格式允许在投影层按系列/作品键收敛；人工绑定、tracking 绑定、
#: 新系列级身份（series:/standalone:）一律原样保留。
_AUTO_UNIT_CANONICAL_RE = re.compile(r"^unit:[0-9a-f]{32}(?::sub:[0-9a-f]{12})?$")


#: 标题首尾需剥离的弱标点（任何组合，逐字符剥离；不用 str.strip 多字符串以
#: 避免 ruff B005 误判）。中文标点、引号、括号、分隔符都包括在内。
_EDGE_PUNCT = " ._·-:：!！?？,，、()（）[]【】\"'“”‘’"
_EDGE_PUNCT_RE = re.compile(f"^[{re.escape(_EDGE_PUNCT)}]+|[{re.escape(_EDGE_PUNCT)}]+$")


def _identity_key(value: str) -> str:
    """标题 → 稳定身份键：压缩空白、去首尾弱标点、大小写折叠。"""
    text = " ".join(str(value or "").split())
    text = _EDGE_PUNCT_RE.sub("", text)
    return text.casefold()


def series_identity_from_titles(work_title: str, series_group: str) -> str:
    """主系列（main_series）系列级 canonical 身份。

    「编号季目录」布局（如 ``作品名.S1-S3+剧场版/1.作品名.[S1].2020``）会把
    同一系列的各季/OVA 拆成多个 MediaUnit；系列级身份保证它们共享同一
    canonical（一张卡、一个镜像作品根）。work_title 优先（编号季目录里
    series_group 可能被提取成 "1.立志篇." 这类碎片），series_group 兜底。
    """
    key = _identity_key(work_title) or _identity_key(series_group)
    if not key:
        return ""
    return f"series:{key}"


def standalone_identity_from_titles(work_title: str, series_group: str, year) -> str:
    """standalone（剧场版/外传系列）作品级 canonical 身份。

    外传 TV 系列的每一集（如 Gun Gale Online 01-25）、同一电影的多个版本
    文件（如 剧场版：序列之争 双版本）共享同一 canonical，不再逐集/逐文件
    拆成独立卡片。
    """
    key = _identity_key(work_title) or _identity_key(series_group)
    if not key:
        return ""
    return f"standalone:{key}:{year or ''}"


def effective_library_identity(
    *,
    card_type: str,
    work_title: str,
    series_group: str,
    year,
    canonical: str,
) -> str:
    """卡片有效身份：自动 unit/文件级 canonical 按系列/作品键收敛。

    - canonical 为空或非自动格式（人工绑定 / tracking 绑定 / series: /
      standalone: 新格式）→ 原样返回（保持既有行为与人工绑定优先）；
    - main_series 且有标题证据 → ``series:{键}``；
    - standalone 且有标题证据 → ``standalone:{键}:{年份}``；
    - 无标题证据 → 原样返回（不弱于旧行为）。
    """
    canonical = str(canonical or "")
    if not canonical or not _AUTO_UNIT_CANONICAL_RE.match(canonical):
        return canonical
    if str(card_type or "") == "standalone":
        identity = standalone_identity_from_titles(work_title, series_group, year)
    else:
        identity = series_identity_from_titles(work_title, series_group)
    return identity or canonical


def _looks_like_season_dirname(value: str) -> bool:
    """判断路径段是否像季目录（S1/Season 1/第X季），这类目录不是作品根。"""
    cleaned = re.sub(r"[\s._\-·:：/\\()（）【】]+", " ", value or "").strip()
    return bool(
        re.fullmatch(r"(?:S|Season)\s*\d+", cleaned, flags=re.IGNORECASE)
        or re.fullmatch(r"第\s*(?:\d+|[一二三四五六七八九十]+)\s*季", cleaned)
    )


def _looks_like_file_part(value: str) -> bool:
    """判断路径段是否像文件名（含视频扩展名）。"""
    return bool(re.search(r"\.(?:mkv|mp4|avi|ts|m2ts|wmv|flv|mov|rmvb|webm)$", value or "", re.IGNORECASE))


def library_card_identity(item: ImportPlanItem) -> str:
    """Return one identity per source-side work root, without metadata merging."""
    parts = [part for part in (item.relative_path or "").replace("\\", "/").split("/") if part]

    if item.card_type != "standalone":
        if item.source != "local" and len(parts) >= 2:
            # 目录树 TXT 路径带分类层（前两段=分类/作品目录，稳定）；而 OpenList
            # 相对选中 root 的路径可能只有“季目录/文件名”或“作品目录/文件”，
            # 路径前两段会因文件名/季目录不同把同一作品拆成不同身份。此时回退
            # 到识别后的作品身份（work_id），保证同一作品身份稳定。
            if _looks_like_file_part(parts[1]) or _looks_like_season_dirname(parts[0]):
                identity = item.work_id or item.series_group or item.work_title
                if identity:
                    return f"source:{item.source}:{identity.casefold()}"
            return f"source:{item.source}:" + "/".join(parts[:2]).casefold()
        if item.source == "local" and parts and _is_local_collection_root(parts[0]):
            return f"source:local:{parts[0].casefold()}"

    directory = Path(item.target_dir) if item.target_dir else None
    if directory is None and item.target_strm_path:
        directory = Path(item.target_strm_path).parent
    if directory is not None:
        if item.group_type in {"season", "special", "sps"} and re.match(
            r"^(?:Season\s*\d+|S\d+|SPs)$",
            directory.name,
            flags=re.IGNORECASE,
        ):
            directory = directory.parent
        normalized = os.path.normcase(os.path.normpath(str(directory)))
        return f"mirror:{item.source}:{normalized}"

    fallback = item.work_id or item.series_group or item.work_title
    return f"fallback:{item.source}:{fallback.casefold()}" if fallback else ""


def effective_work_identity(item) -> str:
    """V3 统一作品身份解析：canonical_work_id 为身份事实，缺失时兼容回退。

    这是 Mirror / Scrape / LibraryIndex 共享的唯一入口，任何模块不得再
    自己写 ``canonical_work_id or work_id or series_group...`` 的猜测链。
    新 durable pipeline 的 current revision 必须携带 canonical_work_id；
    只有 legacy plan（旧 JSON 计划）才允许回退 work_id。
    """
    canonical = str(getattr(item, "canonical_work_id", "") or "")
    if canonical:
        return canonical
    return str(getattr(item, "work_id", "") or "")


def _is_local_collection_root(value: str) -> bool:
    lower = value.casefold()
    return (
        (value.startswith("[") and "]" in value)
        or "vcb-studio" in lower
        or "collection" in lower
        or "合集" in value
        or "系列" in value
    )
