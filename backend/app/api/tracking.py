from __future__ import annotations

import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.tracking.models import TrackingBinding
from app.tracking.service import import_seasonal_root, scan_all_tracking, scan_tracking_binding
from app.tracking.store import (
    get_tracking_binding,
    get_tracking_binding_by_root,
    list_tracking_bindings,
    upsert_tracking_binding,
)

router = APIRouter(prefix="/api/tracking", tags=["tracking"])


class TrackingCreateRequest(BaseModel):
    work_id: str = ""
    display_title: str = ""
    logical_source: str = "local"
    root_path: str
    season_number: Optional[int] = None
    series_group: str = ""


class TrackingPatchRequest(BaseModel):
    tracking_state: Optional[str] = None
    attention_state: Optional[str] = None
    root_path: Optional[str] = None
    season_number: Optional[int] = None


class ScanRequest(BaseModel):
    include_scrape: bool = True
    source: Optional[str] = None
    work_ids: Optional[list[str]] = None


class TrackingRootImportRequest(BaseModel):
    root_path: str
    logical_source: str = "local"
    include_scrape: bool = True


@router.get("/works")
def list_works():
    return {"items": [asdict(item) for item in list_tracking_bindings()]}


@router.post("/works")
def create_work(req: TrackingCreateRequest):
    root = Path(req.root_path).expanduser()
    if not root.is_absolute():
        raise HTTPException(status_code=400, detail="追更目录必须是绝对路径")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="所选追更目录不可访问")
    display_title = req.display_title.strip() or _title_from_folder(root.name)
    if not display_title:
        raise HTTPException(status_code=400, detail="无法从目录名称识别作品名")
    work_id = req.work_id or f"series_{uuid4().hex[:12]}"
    attention = "ready" if req.work_id else "waiting_metadata"
    from app.core.paths import configured_mount_source
    logical_source = configured_mount_source(root) or req.logical_source
    existing_root = get_tracking_binding_by_root(logical_source, str(root), req.season_number)
    if existing_root is not None and not req.work_id:
        # 目录树已登记的追更目录再次被手动添加时，复用原绑定与作品卡片。
        return asdict(existing_root)
    existing = get_tracking_binding(work_id)
    try:
        if existing is not None:
            item = upsert_tracking_binding(replace(
                existing,
                display_title=display_title,
                logical_source=logical_source,
                root_path=str(root),
                season_number=req.season_number,
                series_group=req.series_group or display_title,
            ))
        else:
            item = upsert_tracking_binding(TrackingBinding(
                work_id=work_id, display_title=display_title,
                logical_source=logical_source, root_path=str(root),
                season_number=req.season_number, series_group=req.series_group or display_title,
                attention_state=attention,
            ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(item)


def _title_from_folder(name: str) -> str:
    title = re.sub(r"\s*[（(]\d{4}[）)]\s*$", "", (name or "").strip())
    return " ".join(title.split())


@router.patch("/works/{work_id}")
def patch_work(work_id: str, req: TrackingPatchRequest):
    item = get_tracking_binding(work_id)
    if item is None:
        raise HTTPException(status_code=404, detail="追更作品不存在")
    patch = req.model_dump(exclude_none=True)
    try:
        saved = upsert_tracking_binding(replace(item, **patch))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(saved)


@router.post("/works/{work_id}/scan")
def scan_work(work_id: str, req: ScanRequest = ScanRequest()):
    item = get_tracking_binding(work_id)
    if item is None:
        raise HTTPException(status_code=404, detail="追更作品不存在")
    from app.tasks.registry import get_task_manager
    try:
        task = get_task_manager().submit_queued(
            "tracking_scan", item.logical_source, scan_tracking_binding, item.binding_id,
            queue_name="tracking_mount_scan",
            include_scrape=req.include_scrape, message=f"扫描 {item.display_title or work_id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task.task_id, "status": task.status}


@router.post("/scan-all")
def scan_all(req: ScanRequest = ScanRequest()):
    if req.source not in {None, "all", "local", "pan115", "baidu"}:
        raise HTTPException(status_code=400, detail="未知的扫描来源")
    if req.work_ids is not None and len(req.work_ids) > 1000:
        raise HTTPException(status_code=400, detail="单次扫描作品数量不能超过 1000")
    from app.tasks.registry import get_task_manager
    try:
        task = get_task_manager().submit_queued(
            # 批量追更仍使用全局单通道，避免不同来源同时写媒体库索引；
            # 实际扫描范围由 logical_source 与 work_ids 双重约束。
            "tracking_scan_all", req.source or "all", scan_all_tracking,
            queue_name="tracking_mount_scan",
            include_scrape=req.include_scrape,
            logical_source=None if req.source in {None, "all"} else req.source,
            work_ids=req.work_ids,
            message="扫描当前筛选范围内的追更新番",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task.task_id, "status": task.status}


@router.post("/import-root")
def import_root(req: TrackingRootImportRequest):
    """把包含多部作品的目录作为一个新番批次导入。"""
    root = Path(req.root_path).expanduser()
    if not root.is_absolute():
        raise HTTPException(status_code=400, detail="新番根目录必须是绝对路径")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="所选新番根目录不可访问")
    if req.logical_source not in {"local", "pan115", "baidu"}:
        raise HTTPException(status_code=400, detail="未知的新番来源")

    from app.core.paths import configured_mount_source
    from app.tasks.registry import get_task_manager

    logical_source = configured_mount_source(root) or req.logical_source
    try:
        task = get_task_manager().submit_queued(
            "tracking_import_root",
            logical_source,
            import_seasonal_root,
            str(root),
            logical_source,
            queue_name="tracking_mount_scan",
            include_scrape=req.include_scrape,
            message=f"批量识别新番目录：{root.name}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task.task_id, "status": task.status}
