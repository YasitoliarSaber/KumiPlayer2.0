# -*- coding: utf-8 -*-
"""Stable card identity boundaries shared by scraping and LibraryIndex."""

import os
import re
from pathlib import Path

from app.import_plan.models import ImportPlanItem


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
