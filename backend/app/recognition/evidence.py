"""共享识别证据接口（任务 4）。

- 只包装现有 :func:`recognize_media` 与识别层既有规则表，不复制规则；
- 不复制 OLIST-04 的 OpenList 专用判断函数；
- 命名与 Source Catalog 无关（发现器消费的是识别证据）。
"""

from __future__ import annotations

from app.recognition.media import (
    _EXPLICIT_FILENAME_SPECIAL_PATTERNS,
    _SPS_PATTERNS,
    _is_baidu_category_dir,
    _is_group_folder,
    _is_local_collection_dir,
    _looks_like_plain_season_dir,
    recognize_media,
)


def provider_to_source(provider_id: str) -> str:
    """内容提供商 → 识别层来源语义（quark/other 走 openlist 通用分支）。"""
    if provider_id in ("pan115", "baidu", "local"):
        return provider_id
    return "openlist"


def recognize_path_evidence(
    name: str,
    relative_path: str,
    provider_id: str = "",
    source: str = "",
    existing_work_title: str = "",
) -> dict:
    """对单个视频文件返回识别证据（发现器消费，规则只在 recognition 层）。

    ``existing_work_title`` 传入已确认的作品外层目录名（深层分类下稳定 work 身份）。
    返回：work_id / work_title / series_group / group_type / season_number /
    confidence / needs_review / is_importable / is_auxiliary / is_ignored /
    is_special / is_season。
    """
    effective_source = provider_to_source(provider_id) if provider_id else (source or "pan115")
    guess = recognize_media(name, relative_path, source=effective_source, existing_work_title=existing_work_title)
    return {
        "work_id": guess.work_id,
        "work_title": guess.work_title,
        "series_group": guess.series_group,
        "group_type": guess.group_type,
        "season_number": guess.season_number,
        "confidence": guess.confidence,
        "needs_review": guess.needs_review,
        "is_video": True,
        "is_importable": guess.group_type not in ("ignored", "auxiliary"),
        "is_auxiliary": guess.group_type == "auxiliary",
        "is_ignored": guess.group_type == "ignored",
        "is_special": guess.group_type == "special",
        "is_season": guess.group_type == "season",
    }


def is_structure_dirname(dirname: str) -> bool:
    """目录名是否为识别规则覆盖的结构段（季度/分类/合集/SP/OVA 容器）。

    只判断、不解析；规则全部复用识别层既有模式表。
    """
    name = (dirname or "").strip()
    if not name:
        return False
    if _looks_like_plain_season_dir(name):
        return True
    if _is_baidu_category_dir(name) or _is_group_folder(name) or _is_local_collection_dir(name):
        return True
    if any(pat.search(name) for pat in _SPS_PATTERNS):
        return True
    if any(pat.search(name) for pat in _EXPLICIT_FILENAME_SPECIAL_PATTERNS):
        return True
    return False


def is_auxiliary_filename(name: str) -> bool:
    """文件名是否为辅助段（PV/CM/OP/ED 等；规则在识别层）。"""
    guess = recognize_media(name or "", name or "", source="openlist")
    return guess.group_type in ("auxiliary", "ignored")
