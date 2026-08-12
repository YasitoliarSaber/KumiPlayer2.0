# -*- coding: utf-8 -*-
"""draft ImportPlan 生成器

把 RawSnapshot / RawFile 转换为只包含资源类型和基础动作的 draft ImportPlan。
不做作品识别、季集识别、SP/OPED 识别、目标镜像路径生成。
"""

import hashlib
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import List

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.integrations.openlist.providers import compat_ingest, compat_provider
from app.raw.models import RawFile, RawSnapshot
from app.recognition.resource_type import classify_resource_type, decide_import_action, _EXT_TYPE_MAP


def _make_plan_id(source: str, snapshot_id: str) -> str:
    """生成稳定的 plan_id"""
    content = f"{source}:{snapshot_id}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _make_item_id(plan_id: str, raw_file_id: str) -> str:
    """生成稳定的 ImportPlanItem.id"""
    content = f"{plan_id}:{raw_file_id}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _build_reasons(resource_type: str, ext: str, source: str = "ext") -> List[str]:
    """为 ImportPlanItem 生成基础 reasons

    M03 的 reasons 只说明资源类型和动作，不涉及媒体结构。

    参数:
        resource_type: 资源类型
        ext: 扩展名
        source: 识别来源，"ext"=已知扩展名，"hint"=resource_hint 弱提示，"unknown"=无法识别
    """
    ext_display = ext.lower() if ext else "未知"

    if source == "hint":
        # 来自 resource_hint 弱提示
        hint_reason = f"扩展名 {ext_display} 不在已知列表，根据 resource_hint 弱提示识别为 {resource_type}"
        if resource_type == "video":
            return [hint_reason, "video 文件将在后续生成 strm"]
        if resource_type == "subtitle":
            return [hint_reason, "subtitle 作为附属资源，不生成 strm"]
        return [hint_reason, f"{resource_type} 按规则忽略"]

    if resource_type == "other":
        return [
            f"扩展名 {ext_display} 无法识别资源类型",
            "未知类型标记为 other，需要人工确认",
        ]

    # 来自已知扩展名
    ext_reason = f"扩展名 {ext_display} 识别为 {resource_type}"
    if resource_type == "video":
        return [ext_reason, "video 文件将在后续生成 strm"]
    if resource_type == "subtitle":
        return [ext_reason, "subtitle 作为附属资源，不生成 strm"]
    if resource_type == "nfo":
        return [ext_reason, "网盘来源 nfo 第一版忽略，不从网盘读取"]
    if resource_type == "image":
        return [ext_reason, "网盘来源图片第一版忽略，不从网盘读取"]
    if resource_type == "font":
        return [ext_reason, "字体文件忽略"]
    if resource_type == "archive":
        return [ext_reason, "压缩包/安装包忽略"]
    if resource_type == "audio":
        return [ext_reason, "音频文件忽略"]
    if resource_type == "text":
        return [ext_reason, "文本文件忽略"]
    return [ext_reason, f"{resource_type} 按规则忽略"]


def _classify_source(raw_file: RawFile) -> str:
    """判断资源类型识别的来源

    返回:
        "ext": 已知扩展名识别
        "hint": resource_hint 弱提示识别
        "unknown": 无法识别
    """
    # 归一化扩展名（带点前缀，与 _EXT_TYPE_MAP 键一致）
    ext = raw_file.ext.strip().lower() if raw_file.ext else ""
    if not ext and raw_file.name:
        dot_idx = raw_file.name.rfind(".")
        if dot_idx > 0:
            ext = raw_file.name[dot_idx:].lower()
    if ext and not ext.startswith("."):
        ext = "." + ext

    if ext and ext in _EXT_TYPE_MAP:
        return "ext"
    if raw_file.resource_hint and raw_file.resource_hint.strip().lower() in {
        "video", "subtitle", "nfo", "image", "font", "archive", "audio", "text",
    }:
        return "hint"
    return "unknown"


def _build_item_from_raw_file(
    raw_file: RawFile,
    plan_id: str,
    import_family: str = "",
    provider_id: str = "",
    ingest_method: str = "",
    source_route_id: str = "",
) -> ImportPlanItem:
    """从单个 RawFile 生成 ImportPlanItem"""
    # 独立识别资源类型（不依赖 RawFile.resource_hint）
    resource_type = classify_resource_type(
        name=raw_file.name,
        ext=raw_file.ext,
        resource_hint=raw_file.resource_hint,
    )
    action = decide_import_action(resource_type, raw_file.source)

    # 判断识别来源
    source = _classify_source(raw_file)

    # confidence: 已知扩展名 → high，hint 弱提示 → medium，无法识别 → low
    if source == "ext":
        confidence = "high"
    elif source == "hint":
        confidence = "medium"
    else:
        confidence = "low"

    # needs_review
    needs_review = resource_type == "other"

    # reasons（区分 ext 识别和 hint 弱提示）
    reasons = _build_reasons(resource_type, raw_file.ext, source=source)

    item_id = _make_item_id(plan_id, raw_file.id)

    return ImportPlanItem(
        id=item_id,
        plan_id=plan_id,
        raw_file_id=raw_file.id,
        source=raw_file.source,
        provider_id=provider_id,
        ingest_method=ingest_method,
        source_route_id=source_route_id,
        relative_path=raw_file.relative_path,
        real_path=raw_file.real_path,
        source_size=int(raw_file.size or 0),
        source_mtime=float(raw_file.mtime or 0),
        source_fingerprint=getattr(raw_file, "content_fingerprint", ""),
        resource_type=resource_type,
        action=action,
        import_family=import_family,
        confidence=confidence,
        needs_review=needs_review,
        reasons=reasons,
        warnings=[],
    )


def _build_summary(items: List[ImportPlanItem], import_family: str = "") -> dict:
    """生成 ImportPlan.summary 统计"""
    total = len(items)

    type_counter = Counter(item.resource_type for item in items)
    action_counter = Counter(item.action for item in items)

    return {
        "total_items": total,
        "by_resource_type": dict(type_counter),
        "by_action": dict(action_counter),
        "video_count": type_counter.get("video", 0),
        "subtitle_count": type_counter.get("subtitle", 0),
        "ignored_count": action_counter.get("ignore", 0),
        "attach_only_count": action_counter.get("attach_only", 0),
        "needs_review_count": sum(1 for item in items if item.needs_review),
        "import_family": import_family,
    }


def build_draft_import_plan(snapshot: RawSnapshot) -> ImportPlan:
    """从 RawSnapshot 生成 draft ImportPlan

    每个 RawFile 生成一个 ImportPlanItem。
    只填充资源类型和基础动作，不填媒体结构字段。

    参数:
        snapshot: 来源适配器输出的 RawSnapshot

    返回:
        ImportPlan(status="draft")
    """
    plan_id = _make_plan_id(snapshot.source, snapshot.snapshot_id)
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    import_family = getattr(snapshot, "import_family", "") or ""
    import_scope = getattr(snapshot, "import_scope", "") or ""

    provider_id = getattr(snapshot, "provider_id", "") or compat_provider(snapshot.source)
    ingest_method = getattr(snapshot, "ingest_method", "") or compat_ingest(snapshot.source)
    source_route_id = getattr(snapshot, "source_route_id", "") or ""
    items = [
        _build_item_from_raw_file(
            raw_file, plan_id, import_family,
            provider_id=provider_id,
            ingest_method=ingest_method,
            source_route_id=source_route_id,
        )
        for raw_file in snapshot.files
    ]

    summary = _build_summary(items, import_family)

    return ImportPlan(
        plan_id=plan_id,
        source=snapshot.source,
        provider_id=provider_id,
        ingest_method=getattr(snapshot, "ingest_method", "") or compat_ingest(snapshot.source),
        source_route_id=getattr(snapshot, "source_route_id", "") or "",
        source_snapshot_id=snapshot.snapshot_id,
        root_container=getattr(snapshot, "root_container", "") or "",
        import_family=import_family,
        import_scope=import_scope,
        created_at=now,
        updated_at=now,
        status="draft",
        items=items,
        warnings=[],
        summary={**summary, "import_scope": import_scope},
    )
