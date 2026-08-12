# -*- coding: utf-8 -*-
"""来源 API 端点

GET  /api/sources
POST /api/sources/{source}/parse
POST /api/sources/local/scan
"""

import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import load_config
from app.import_plan.models import ImportPlan
from app.import_plan.store import save_import_plan
from app.raw.store import load_raw_snapshot, save_raw_snapshot
from app.sources.path_validation import resolve_baidu_snapshot_root, tree_scope_name
from app.sources.registry import get_source_adapter, get_source_root, list_sources

router = APIRouter(prefix="/api/sources", tags=["sources"])

_SEASONAL_ROOT_NAMES = {"新番", "追更", "新番追更"}


# ============================================================
# 请求模型
# ============================================================

class ParseRequest(BaseModel):
    input_path: str
    source_root: Optional[str] = None
    import_family: Optional[str] = None
    import_scope: Optional[str] = None
    auto_pipeline: bool = False
    auto_scrape: bool = False


class LocalScanRequest(BaseModel):
    root_path: str
    import_family: Optional[str] = None
    import_scope: Optional[str] = None
    auto_pipeline: bool = False
    auto_scrape: bool = False


def _normalize_import_family(value: Optional[str]) -> str:
    family = (value or "").strip().lower()
    if family not in {"", "anime", "live"}:
        raise HTTPException(status_code=400, detail="import_family 只能是 anime 或 live")
    return family


def _normalize_import_scope(value: Optional[str], family: str) -> str:
    """校验 import_scope，对于目录树和本地扫描通用。"""
    scope = (value or "").strip().lower()
    if scope not in {"", "seasonal"}:
        raise HTTPException(status_code=400, detail="import_scope 只能是 seasonal")
    if scope == "seasonal" and family != "anime":
        raise HTTPException(status_code=400, detail="只有动画可标记为追更新番")
    return scope


def _resolve_final_import_scope(manual_scope: str, auto_scope: str) -> str:
    """双重保险：手动指定或自动识别任一为 seasonal 则结果为 seasonal。"""
    return "seasonal" if manual_scope == "seasonal" or auto_scope == "seasonal" else ""


def _is_seasonal_root_name(name: str) -> bool:
    normalized = name.strip().casefold()
    return normalized in _SEASONAL_ROOT_NAMES or "新番" in normalized


def _import_scope_for_baidu_tree_content(snapshot) -> str:
    """仅当百度目录树整体以新番目录为根时，按树内容识别为追更范围。"""
    root_names = {
        file.source_path_parts[0]
        for file in snapshot.files
        if file.source_path_parts and file.source_path_parts[0].strip()
    }
    return "seasonal" if root_names and all(_is_seasonal_root_name(name) for name in root_names) else ""


def _resolve_tree_source_root(input_path: str, configured_root: str) -> Path:
    """根据目录树导出文件名匹配挂载根下真实存在的子目录。"""
    root = Path(configured_root).expanduser()
    stem = Path(input_path).stem.strip()
    scope_name = tree_scope_name(input_path)
    if not scope_name or scope_name.casefold() == root.name.casefold():
        return root
    if re.fullmatch(r"根目录\d*", scope_name):
        return root
    try:
        children = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return root
    exact_child = next((item for item in children if item.name.casefold() == scope_name.casefold()), None)
    if exact_child is not None:
        return exact_child

    # 目录树导出文件常带时间戳，例如“新番_文件目录_20260712005344.txt”。
    # 旧逻辑只处理“新番_文件目录.txt”，会丢失“新番”范围，导致追更绑定不注册。
    tokens = {
        token.strip()
        for token in re.split(r"[_\s-]+", re.sub(r"(?:文件目录|目录树)", " ", stem))
        if token.strip() and not re.fullmatch(r"\d{6,}", token.strip())
    }
    token_child = next((item for item in children if item.name in tokens), None)
    if token_child is not None:
        return token_child

    seasonal_child = next(
        (item for item in children if item.name.strip().casefold() in _SEASONAL_ROOT_NAMES and item.name in stem),
        None,
    )
    return seasonal_child or root


def _import_scope_for_root(root: Path) -> str:
    return "seasonal" if _is_seasonal_root_name(root.name) else ""


def _import_scope_for_tree(input_path: str, root: Path) -> str:
    """目录树文件名中的新番范围优先于可选的挂载目录推断。

    目录树是导入时的用户意图来源。即使挂载盘暂时无法列出 ``新番``
    子目录，也不能把明确标注为新番的导入悄悄降级为普通番剧。
    """
    tree_name = Path(input_path).stem.casefold()
    if "新番" in tree_name:
        return "seasonal"
    return _import_scope_for_root(root)


class SampleFileInfo(BaseModel):
    name: str
    path: str
    size: int
    modified_at: float


# ============================================================
# 端点
# ============================================================

@router.get("")
def get_sources():
    """列出可用来源"""
    sources = list_sources()
    return {"sources": sources}


@router.get("/samples")
def get_sample_tree_files():
    """列出配置目录下可导入的目录树文件。

    前端浏览器不能直接枚举本机任意目录，所以这里由后端暴露项目内
    配置的目录树目录，让用户点选文件而不是手动输入绝对路径。
    """
    configured_dir = load_config().directory_tree_dir.strip()
    if not configured_dir:
        return {"sample_dir": "", "files": []}

    sample_dir = Path(configured_dir).expanduser()
    if not sample_dir.exists():
        return {"sample_dir": str(sample_dir), "files": []}

    files = []
    for path in sorted(sample_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".txt", ".tree", ".log"}:
            continue
        stat = path.stat()
        files.append(SampleFileInfo(
            name=path.name,
            path=str(path),
            size=stat.st_size,
            modified_at=stat.st_mtime,
        ))
    return {"sample_dir": str(sample_dir), "files": files}


@router.post("/{source}/parse")
def parse_source(source: str, req: ParseRequest):
    """解析来源目录树（txt 类来源）

    流程：
    1. 获取适配器
    2. parse → RawSnapshot
    3. 保存 RawSnapshot
    4. build_draft_import_plan
    5. recognize_import_plan_media
    6. 保存 ImportPlan
    7. 返回 snapshot_id + plan_id + counts
    """
    if source == "local":
        raise HTTPException(
            status_code=400,
            detail="本地来源请使用 POST /api/sources/local/scan",
        )

    # 获取适配器
    try:
        adapter = get_source_adapter(source)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    family = _normalize_import_family(req.import_family)
    manual_scope = _normalize_import_scope(req.import_scope, family)

    # 获取 source_root
    try:
        source_root = get_source_root(source, req.source_root or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not source_root:
        raise HTTPException(
            status_code=400,
            detail=f"未配置 {source}_root，请在配置中设置或传入 source_root",
        )

    # 解析
    try:
        effective_root = _resolve_tree_source_root(req.input_path, source_root)
        snapshot = adapter.parse(req.input_path, str(effective_root))
        path_validation = None
        if source == "baidu":
            path_validation = resolve_baidu_snapshot_root(
                snapshot,
                req.input_path,
                str(effective_root),
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")

    # 设置 import_family
    snapshot.import_family = family

    # 校验手动 import_scope 并与自动识别合并
    auto_scope = _import_scope_for_tree(req.input_path, Path(snapshot.source_root))
    if source == "baidu":
        auto_scope = _resolve_final_import_scope(
            auto_scope,
            _import_scope_for_baidu_tree_content(snapshot),
        )
    snapshot.import_scope = _resolve_final_import_scope(manual_scope, auto_scope)

    # 保存 RawSnapshot
    save_raw_snapshot(snapshot)

    # 生成 draft ImportPlan
    plan = _build_and_recognize_plan(snapshot)

    # 保存 ImportPlan
    save_import_plan(plan)

    task = _submit_auto_pipeline(source, plan.plan_id, req.auto_scrape) if req.auto_pipeline else None

    response = {
        "snapshot_id": snapshot.snapshot_id,
        "plan_id": plan.plan_id,
        "source": source,
        "file_count": snapshot.file_count,
        "video_count": snapshot.video_count,
        "plan_status": plan.status,
        "import_family": plan.import_family,
        "import_scope": snapshot.import_scope,
    }
    if path_validation is not None:
        response["path_validation"] = path_validation.to_dict()
    if task:
        response["task_id"] = task.task_id
        response["task_status"] = task.status
    return response


@router.post("/local/scan")
def scan_local(req: LocalScanRequest):
    """扫描本地目录，并按实际路径建立或增量更新媒体库卡片。"""
    from app.api.imports import _diff_to_dict
    from app.media_presets.service import preset_to_dict, scan_local_preset

    preset, version, plan, diff, reused, unchanged = scan_local_preset(
        req.root_path,
        req.import_family or "",
        req.import_scope or "",
    )

    task = _submit_auto_pipeline("local", plan.plan_id, req.auto_scrape) if req.auto_pipeline else None
    current_snapshot = load_raw_snapshot(preset.current_snapshot_id)

    response = {
        "snapshot_id": preset.current_snapshot_id,
        "plan_id": plan.plan_id,
        "source": "local",
        "file_count": current_snapshot.file_count if current_snapshot is not None else 0,
        "video_count": preset.video_count,
        "plan_status": plan.status,
        "import_family": plan.import_family,
        "import_scope": preset.import_scope,
        "path_validation": version.path_validation,
        "preset": preset_to_dict(preset),
        "version": asdict(version),
        "reused_preset": reused,
        "unchanged": unchanged,
    }
    if diff is not None:
        response["diff"] = _diff_to_dict(diff)
    if task:
        response["task_id"] = task.task_id
        response["task_status"] = task.status
    return response


def _build_and_recognize_plan(snapshot) -> ImportPlan:
    """从 RawSnapshot 生成 draft ImportPlan 并执行媒体识别"""
    from app.recognition.plan_recognizer import recognize_import_plan_media
    from app.recognition.planner import build_draft_import_plan

    plan = build_draft_import_plan(snapshot)
    plan = recognize_import_plan_media(plan)
    return plan


def _submit_auto_pipeline(source: str, plan_id: str, include_scrape: bool):
    """Submit the hands-off import pipeline task."""
    from app.import_pipeline.service import run_auto_import_pipeline
    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    try:
        return manager.submit(
            "import_auto",
            source,
            run_auto_import_pipeline,
            source,
            plan_id,
            include_scrape=include_scrape,
            message="自动导入流水线",
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
