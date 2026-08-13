# -*- coding: utf-8 -*-
"""媒体库 API.

This module is also the final UI contract boundary.  The library index remains
the source for media structure, while this layer adds presentation-safe
fallbacks for assets and episode display names so the frontend does not need to
understand mirror filesystem details.
"""

from dataclasses import asdict, is_dataclass
from copy import deepcopy
from pathlib import Path
import re
from threading import RLock
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from app.library.delete import (
    CatalogCleanupBusyError,
    DeletePreview,
    build_delete_preview,
    build_library_clear_preview,
    execute_delete,
)
from app.library.delete_store import load_delete_preview, save_delete_log, save_delete_preview
from app.library.diagnostics import diagnose_library_consistency
from app.library import service as library_service
from app.library.store import get_library_index_signature
from app.library.watch_status import (
    VALID_WATCH_STATUS,
    WorkWatchStatus,
    get_watch_status,
    load_watch_statuses,
    set_watch_status,
)
from app.tasks.registry import get_task_manager

router = APIRouter(prefix="/api/library", tags=["library"])


class RescanRequest(BaseModel):
    source: Optional[str] = None


class DeleteConfirmRequest(BaseModel):
    preview_id: str


class DeleteLibraryPreviewRequest(BaseModel):
    source: Optional[str] = "all"


class WatchStatusPatchRequest(BaseModel):
    status: str = ""
    note: str = ""
    favorite: Optional[bool] = None


class WorkTitlePatchRequest(BaseModel):
    title: str


class ManualEpisodePreviewRequest(BaseModel):
    paths: list[str]
    season_number: Optional[int] = None


class ManualEpisodeCommitRequest(BaseModel):
    plan_id: str
    auto_scrape: bool = True


_DELETE_PREVIEWS: dict[str, DeletePreview] = {}
_CACHE_SIGNATURE = ""
_LIBRARY_RESPONSE_CACHE: dict[tuple[str, str, str, str], Any] = {}
_WORK_RESPONSE_CACHE: dict[tuple[str, str], Any] = {}
_RESPONSE_CACHE_LOCK = RLock()


@router.get("")
def get_library(source: Optional[str] = None, watch_status: Optional[str] = None, compact: bool = False):
    """获取媒体库索引."""
    with _RESPONSE_CACHE_LOCK:
        watch_status_filter = _normalize_watch_status_filter(watch_status)
        signature = _prepare_response_cache()
        key = (signature, source or "", "__all__" if watch_status_filter is None else watch_status_filter, "compact" if compact else "full")
        cached = _LIBRARY_RESPONSE_CACHE.get(key)
        if cached is not None:
            return deepcopy(cached)

        payload = jsonable_encoder(library_service.get_library(source, compact=compact))
        enriched = _enrich_compact_payload(payload) if compact else _enrich_payload(payload)
        _apply_watch_status_filter(enriched, watch_status_filter)
        _LIBRARY_RESPONSE_CACHE[key] = enriched
        return deepcopy(enriched)


@router.get("/diagnostics")
def get_library_diagnostics(source: Optional[str] = None):
    """只读检查 LibraryIndex 与 ScrapeMap 是否一致."""
    return diagnose_library_consistency(source)


@router.post("/rescan")
def rescan_library(req: RescanRequest):
    """后台重扫媒体库."""
    manager = get_task_manager()
    try:
        record = manager.submit(
            "library_rescan",
            req.source or "all",
            library_service.rescan_library,
            req.source,
            message="重扫媒体库",
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"task_id": record.task_id, "status": record.status}


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    """查询媒体库任务状态."""
    record = get_task_manager().get_task(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _to_plain(record)


def _get_work_detail_impl(work_id: str):
    """获取作品详情."""
    with _RESPONSE_CACHE_LOCK:
        signature = _prepare_response_cache()
        key = (signature, work_id)
        cached = _WORK_RESPONSE_CACHE.get(key)
        if cached is not None:
            return deepcopy(cached)

        payload = jsonable_encoder(library_service.get_work_detail(work_id))
        if payload is None:
            raise HTTPException(status_code=404, detail=f"作品不存在: {work_id}")
        enriched = _enrich_payload(payload)
        _WORK_RESPONSE_CACHE[key] = enriched
        return deepcopy(enriched)


@router.get("/works/{work_id}")
def get_work_detail(work_id: str):
    return _get_work_detail_impl(work_id)


@router.get("/watch-status")
def list_watch_status():
    """列出 KumiPlayer 本地作品观看状态。"""
    return {"items": [_to_plain(item) for item in load_watch_statuses().values()]}


@router.get("/watch-status/{work_id}")
def get_work_watch_status(work_id: str):
    """获取单个作品的本地观看状态；未设置时 status 为空字符串。"""
    return _to_plain(get_watch_status(work_id))


@router.patch("/watch-status/{work_id}")
def patch_work_watch_status(work_id: str, req: WatchStatusPatchRequest):
    """设置 KumiPlayer 本地作品观看状态。

    status: "" / watching / watched / on_hold / dropped
    """
    if req.status not in VALID_WATCH_STATUS:
        raise HTTPException(status_code=400, detail="未知观看状态")
    item = set_watch_status(work_id, req.status, req.note, req.favorite)
    _clear_response_caches()
    return _to_plain(item)


@router.put("/works/{work_id}/artwork/{kind}")
async def upload_work_artwork(work_id: str, kind: str, file: UploadFile = File(...)):
    if kind not in {"poster", "fanart", "clearlogo"}:
        raise HTTPException(status_code=400, detail="图片类型只能是 poster、fanart 或 clearlogo")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=400, detail="只支持 JPG、PNG 和 WebP 图片")
    content = await file.read(12 * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(content) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片不能超过 12 MB")

    import hashlib
    from app.core.paths import get_data_dir, sanitize_filename
    from app.library.overrides import set_artwork_override
    safe_work = sanitize_filename(work_id)[:80]
    digest = hashlib.sha1(work_id.encode("utf-8")).hexdigest()[:8]
    directory = get_data_dir() / "user_assets" / f"{safe_work}_{digest}"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{kind}{suffix}"
    for old in directory.glob(f"{kind}.*"):
        if old != destination:
            old.unlink(missing_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)
    set_artwork_override(work_id, kind, str(destination))
    _clear_response_caches()
    return {"work_id": work_id, "kind": kind, "path": str(destination), "provenance": "manual"}


@router.delete("/works/{work_id}/artwork/{kind}")
def restore_work_artwork(work_id: str, kind: str):
    if kind not in {"poster", "fanart", "clearlogo"}:
        raise HTTPException(status_code=400, detail="未知图片类型")
    from app.library.overrides import clear_artwork_override, get_work_override
    current = get_work_override(work_id)
    path = getattr(current, f"{kind}_path", "") if current else ""
    if path:
        Path(path).unlink(missing_ok=True)
    clear_artwork_override(work_id, kind)
    _clear_response_caches()
    return {"work_id": work_id, "kind": kind, "restored": True}


@router.patch("/works/{work_id}/title")
def patch_work_title(work_id: str, req: WorkTitlePatchRequest):
    if library_service.get_work_detail(work_id) is None:
        raise HTTPException(status_code=404, detail=f"作品不存在: {work_id}")
    from app.library.overrides import set_title_override
    try:
        override = set_title_override(work_id, req.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _clear_response_caches()
    return {"work_id": work_id, "title": (override.metadata or {}).get("title", ""), "provenance": "manual"}


@router.delete("/works/{work_id}/title")
def restore_work_title(work_id: str):
    if library_service.get_work_detail(work_id) is None:
        raise HTTPException(status_code=404, detail=f"作品不存在: {work_id}")
    from app.library.overrides import clear_title_override
    clear_title_override(work_id)
    _clear_response_caches()
    return {"work_id": work_id, "restored": True}


@router.post("/works/{work_id}/episodes/preview")
def preview_manual_episodes(work_id: str, req: ManualEpisodePreviewRequest):
    from app.library.manual_import import preview_manual_episodes as build_preview
    try:
        return build_preview(work_id, req.paths, req.season_number)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/works/{work_id}/episodes/commit")
def commit_manual_episodes(work_id: str, req: ManualEpisodeCommitRequest):
    from app.library.manual_import import commit_manual_episode_plan, validate_manual_episode_preview
    try:
        plan = validate_manual_episode_preview(req.plan_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if plan.summary.get("target_work_id") not in {None, "", work_id}:
        raise HTTPException(status_code=400, detail="追加计划不属于当前作品")
    from app.tasks.registry import get_task_manager
    try:
        task = get_task_manager().submit(
            "manual_episode_import", work_id, commit_manual_episode_plan,
            work_id, req.plan_id, include_scrape=req.auto_scrape,
            message="追加剧集",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task.task_id, "status": task.status, "plan_id": plan.plan_id}


@router.post("/delete/library/preview")
def delete_library_preview(req: DeleteLibraryPreviewRequest):
    """预览一键清空生成媒体库。

    只清空 mirror root 内的生成文件，不删除真实网盘 / 本地源文件。
    """
    try:
        preview = build_library_clear_preview(req.source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _DELETE_PREVIEWS[preview.preview_id] = preview
    save_delete_preview(preview)
    return _to_plain(preview)


@router.post("/works/{work_id}/delete/preview")
def delete_work_preview(work_id: str):
    """预览删除当前作品；只枚举生成镜像，不触碰真实媒体。"""
    preview = build_delete_preview(work_id)
    _DELETE_PREVIEWS[preview.preview_id] = preview
    save_delete_preview(preview)
    return _to_plain(preview)


@router.post("/works/{work_id}/delete/confirm")
def delete_work_confirm(work_id: str, req: DeleteConfirmRequest):
    preview = _DELETE_PREVIEWS.get(req.preview_id) or load_delete_preview(req.preview_id)
    if preview is None:
        raise HTTPException(status_code=404, detail=f"删除预览不存在: {req.preview_id}")
    if preview.scope != "work" or preview.work_id != work_id:
        raise HTTPException(status_code=400, detail="删除预览不属于当前作品")
    if preview.blocked:
        raise HTTPException(status_code=409, detail="删除预览已阻断，请重新预览")
    manager = get_task_manager()
    try:
        with manager.maintenance("删除当前作品"):
            result = execute_delete(preview)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"{exc}，请等待任务完成后重试") from exc
    save_delete_log(result, source=preview.source, scope=preview.scope)
    _DELETE_PREVIEWS.pop(req.preview_id, None)
    _clear_response_caches()
    return _to_plain(result)


@router.post("/delete/library/confirm")
def delete_library_confirm(req: DeleteConfirmRequest):
    preview = _DELETE_PREVIEWS.get(req.preview_id) or load_delete_preview(req.preview_id)
    if preview is None:
        raise HTTPException(status_code=404, detail=f"删除预览不存在: {req.preview_id}")
    if preview.scope != "library":
        raise HTTPException(status_code=400, detail="preview_id 不是整库删除预览")
    if preview.blocked:
        raise HTTPException(status_code=409, detail="删除预览已阻断，请重新预览")
    manager = get_task_manager()
    try:
        with manager.maintenance("删除全部媒体库"):
            result = execute_delete(preview)
    except CatalogCleanupBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"{exc}，请等待任务完成后重试") from exc
    save_delete_log(result, source=preview.source, scope=preview.scope)
    _DELETE_PREVIEWS.pop(req.preview_id, None)
    return _to_plain(result)
def _to_plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return jsonable_encoder(value)


def _prepare_response_cache() -> str:
    """Clear API response caches when the library index changes on disk."""
    global _CACHE_SIGNATURE
    signature = get_library_index_signature()
    if signature != _CACHE_SIGNATURE:
        _CACHE_SIGNATURE = signature
        _LIBRARY_RESPONSE_CACHE.clear()
        _WORK_RESPONSE_CACHE.clear()
    return signature


def _clear_response_caches() -> None:
    global _CACHE_SIGNATURE
    with _RESPONSE_CACHE_LOCK:
        _CACHE_SIGNATURE = ""
        _LIBRARY_RESPONSE_CACHE.clear()
        _WORK_RESPONSE_CACHE.clear()


def _enrich_payload(payload: Any) -> Any:
    """Add UI-safe asset and episode-title fallbacks recursively."""
    if isinstance(payload, dict):
        _enrich_work_like(payload)
        for value in payload.values():
            _enrich_payload(value)
    elif isinstance(payload, list):
        for item in payload:
            _enrich_payload(item)
    return payload


def _enrich_compact_payload(payload: Any) -> Any:
    """Add cheap UI fields for list pages without filesystem probing."""
    if not isinstance(payload, dict):
        return payload
    works = payload.get("works")
    if not isinstance(works, list):
        return payload
    statuses = load_watch_statuses()
    from app.tracking.store import get_tracking_binding
    from app.library.overrides import get_work_override
    for work in works:
        if not isinstance(work, dict):
            continue
        card_type = str(work.get("card_type") or "").lower()
        work["ui_hide_card_type_badge"] = card_type in {
            "main_series",
            "main-series",
            "主系列",
            "standalone",
            "standalone_card",
            "独立卡片",
        }
        work_id = str(work.get("work_id") or "")
        work["watch_status"] = _to_plain(statuses.get(work_id) or WorkWatchStatus(work_id=work_id))
        tracking = get_tracking_binding(work_id) if work_id else None
        work["tracking"] = _to_plain(tracking) if tracking else None
        work["metadata_state"] = tracking.attention_state if tracking else work.get("metadata_state", "ready")
        override = get_work_override(work_id) if work_id else None
        if override:
            manual_title = str((override.metadata or {}).get("title") or "").strip()
            if manual_title:
                work["title"] = manual_title
                work["title_provenance"] = "manual"
            for kind in ("poster", "fanart", "clearlogo"):
                value = getattr(override, f"{kind}_path", "")
                if value:
                    work[f"{kind}_path"] = value
                    # 手动 override 是受控本地文件；local_* 只同步真实存在且非远程的路径，
                    # 保证 compact 卡片本地优先、canonical 远程兜底的展示契约一致。
                    local_value = ""
                    if not str(value).lower().startswith(("http://", "https://")):
                        local_value = value if Path(value).is_file() else ""
                    work[f"local_{kind}_path"] = local_value
            work["artwork_provenance"] = {
                kind: "manual" if getattr(override, f"{kind}_path", "") else "online"
                for kind in ("poster", "fanart", "clearlogo")
            }
        _apply_nfo_title_fallback(work, [])
    return payload


def _enrich_work_like(obj: dict) -> None:
    if not isinstance(obj, dict):
        return

    card_type = str(obj.get("card_type") or "").lower()
    obj["ui_hide_card_type_badge"] = card_type in {
        "main_series",
        "main-series",
        "主系列",
        "standalone",
        "standalone_card",
        "独立卡片",
    }
    if _looks_like_work(obj):
        from app.tracking.store import get_tracking_binding
        from app.library.overrides import get_work_override
        tracking = get_tracking_binding(str(obj.get("work_id") or ""))
        obj["tracking"] = _to_plain(tracking) if tracking else None
        obj["metadata_state"] = tracking.attention_state if tracking else obj.get("metadata_state", "ready")
        override = get_work_override(str(obj.get("work_id") or ""))
        if override:
            manual_title = str((override.metadata or {}).get("title") or "").strip()
            if manual_title:
                obj["title"] = manual_title
                obj["title_provenance"] = "manual"
            provenance = dict(obj.get("artwork_provenance") or {})
            for kind in ("poster", "fanart", "clearlogo"):
                value = getattr(override, f"{kind}_path", "")
                if value:
                    obj[f"{kind}_path"] = value
                    provenance[kind] = "manual"
            obj["artwork_provenance"] = provenance

    if _looks_like_work(obj):
        _apply_nfo_title_fallback(obj, _collect_episode_paths(obj))

    poster = obj.get("poster_path") or obj.get("poster")
    fanart = obj.get("fanart_path") or obj.get("fanart") or obj.get("backdrop_path")
    logo = obj.get("clearlogo_path") or obj.get("clearlogo")

    if poster:
        obj["poster_path"] = poster
        obj["poster"] = poster
        obj["hero_poster_path"] = poster
    if fanart:
        obj["fanart_path"] = fanart
        obj["backdrop_path"] = fanart
        obj["fanart"] = fanart
        obj["backdrop"] = fanart
        obj["hero_backdrop_path"] = fanart
        obj["header_backdrop_path"] = fanart
    if logo:
        obj["clearlogo_path"] = logo
        obj["clearlogo"] = logo
        obj["logo_path"] = logo
        obj["logo"] = logo
        obj["hero_logo_path"] = logo
        obj["header_logo_path"] = logo

    if _looks_like_work(obj):
        _enrich_episode_artwork(obj, fanart or poster or "")

    work_title = obj.get("title") or obj.get("work_title") or obj.get("local_title") or obj.get("series_group")
    if _looks_like_work(obj):
        obj["watch_status"] = _to_plain(get_watch_status(str(obj.get("work_id") or "")))
    _enrich_episode_titles(obj, work_title)


def _enrich_episode_artwork(work: dict, fallback_path: str) -> None:
    """让缺失或失效的单集剧照回退到当前作品背景图。"""
    if not fallback_path:
        return
    episode_groups = [work.get("episodes") or []]
    for season in work.get("seasons") or []:
        if isinstance(season, dict):
            episode_groups.append(season.get("episodes") or [])
    for episodes in episode_groups:
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            thumb = str(episode.get("thumb_path") or "").strip()
            if not _asset_reference_available(thumb):
                episode["thumb_path"] = fallback_path


def _asset_reference_available(value: str) -> bool:
    return bool(str(value or "").strip())


def _looks_like_work(obj: dict) -> bool:
    if not obj.get("work_id") or _looks_like_episode(obj):
        return False
    return any(key in obj for key in ("seasons", "episodes", "card_type", "poster_path", "fanart_path"))


def _normalize_watch_status_filter(value: Optional[str]) -> Optional[str]:
    if value is None or value == "" or value == "all":
        return None
    if value == "empty":
        return ""
    if value not in VALID_WATCH_STATUS:
        raise HTTPException(status_code=400, detail="未知观看状态筛选")
    return value


def _apply_watch_status_filter(payload: Any, watch_status_filter: Optional[str]) -> None:
    if watch_status_filter is None or not isinstance(payload, dict):
        return
    works = payload.get("works")
    if not isinstance(works, list):
        return
    filtered = [
        work for work in works
        if isinstance(work, dict)
        and (work.get("watch_status") or {}).get("status", "") == watch_status_filter
    ]
    payload["works"] = filtered
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary["work_count"] = len(filtered)
        summary["episode_count"] = sum(
            len(work.get("episodes", [])) for work in filtered if isinstance(work, dict)
        )


def _collect_episode_paths(obj: Any) -> list[Path]:
    paths: list[Path] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            raw = value.get("strm_path") or value.get("path") or value.get("file_path")
            if raw and str(raw).lower().endswith(".strm"):
                paths.append(Path(str(raw)))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return paths


def _asset_search_roots(obj: dict, episode_paths: list[Path]) -> list[Path]:
    roots: list[Path] = []
    for key in ("target_dir", "work_dir", "dir_path", "dir", "path"):
        raw = obj.get(key)
        if raw:
            p = Path(str(raw))
            roots.append(p if p.suffix == "" else p.parent)
    for ep in episode_paths:
        roots.append(ep.parent)
        # Episode parents for normal TV are usually Season folders, whose
        # parent is the work directory. Standalone movies live directly under
        # the source namespace, so ep.parent.parent would be the global "115"
        # root and could leak another movie's poster into this card.
        if _is_season_like_dir(ep.parent):
            roots.append(ep.parent.parent)

    deduped: list[Path] = []
    seen = set()
    for root in roots:
        if _is_source_namespace_root(root):
            continue
        key = str(root)
        if key and key not in seen:
            deduped.append(root)
            seen.add(key)
    return deduped


def _is_season_like_dir(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith("season ")
        or name in {"sps", "sp", "special", "specials", "op-ed", "op_ed"}
        or bool(re.fullmatch(r"s\d{1,2}", name))
    )


def _is_source_namespace_root(path: Path) -> bool:
    name = path.name.lower()
    parent = path.parent.name.lower()
    return parent == "mirror" and name in {"115", "pan115", "baidu", "local", "openlist"}


def _find_first_asset(roots: list[Path], names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    for root in roots:
        if not root.exists():
            continue
        for parent in (root, *list(root.glob("*"))):
            if not parent.is_dir():
                continue
            for child in parent.iterdir():
                if child.is_file() and child.name.lower() in wanted:
                    return str(child)
    return ""


def _work_nfo_candidates(roots: list[Path]) -> list[Path]:
    candidates = []
    for root in roots:
        candidates.append(root / "tvshow.nfo")
        candidates.append(root / "movie.nfo")
        for child in root.iterdir() if root.exists() else []:
            if child.is_dir():
                candidates.append(child / "tvshow.nfo")
                candidates.append(child / "movie.nfo")
    return candidates


def _read_work_nfo_field(roots: list[Path], field: str) -> str:
    if field not in {"title", "plot"}:
        return ""

    seen = set()
    for candidate in _work_nfo_candidates(roots):
        key = str(candidate)
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = re.search(rf"<{field}>(.*?)</{field}>", text, re.I | re.S)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip()
        if value:
            return value
    return ""


def _read_work_title_from_nfo(roots: list[Path]) -> str:
    return _read_work_nfo_field(roots, "title")


def _read_work_plot_from_nfo(roots: list[Path]) -> str:
    return _read_work_nfo_field(roots, "plot")


def _enrich_episode_titles(obj: Any, work_title: Optional[str]) -> None:
    if isinstance(obj, dict):
        if _looks_like_episode(obj):
            _enrich_one_episode_title(obj, work_title)
        next_title = work_title or obj.get("title") or obj.get("work_title") or obj.get("local_title")
        for child in obj.values():
            _enrich_episode_titles(child, next_title)
    elif isinstance(obj, list):
        for child in obj:
            _enrich_episode_titles(child, work_title)


def _looks_like_episode(obj: dict) -> bool:
    return (
        ("episode_number" in obj or "episode" in obj)
        and ("season_number" in obj or "season" in obj)
        and bool(obj.get("strm_path") or obj.get("path") or obj.get("file_path"))
    )


def _season_like_dir_path(obj: dict) -> Path | None:
    """返回作品 dir_path 中季度目录（Season N / Sxx / SPs）对应的路径。

    只有刮削把 tvshow.nfo 写进季度目录的场景才允许读取 API 消费 NFO 标题；
    作品根目录属于镜像布局，标题应在扫描或刮削阶段写回索引，不在读取时探测。
    """
    raw = obj.get("dir_path") or obj.get("target_dir") or ""
    if not raw:
        return None
    path = Path(str(raw))
    name = path.name.lower()
    if (
        name.startswith("season ")
        or name in {"sps", "sp", "special", "specials", "op-ed", "op_ed"}
        or bool(re.fullmatch(r"s\d{1,2}", name))
    ):
        return path
    return None


def _apply_nfo_title_fallback(obj: dict, episode_paths: list[Path]) -> None:
    """季度目录存在 tvshow.nfo 时，用其中本地化标题覆盖识别标题。"""
    if obj.get("title_provenance"):
        return
    if _season_like_dir_path(obj) is None:
        return
    roots = _asset_search_roots(obj, episode_paths)
    nfo_title = _read_work_title_from_nfo(roots)
    if nfo_title:
        obj["title"] = nfo_title
        obj["title_provenance"] = "nfo"


def _enrich_one_episode_title(ep: dict, work_title: Optional[str]) -> None:
    group_type = str(ep.get("group_type") or "").lower()
    raw_path = ep.get("strm_path") or ep.get("path") or ep.get("file_path")

    if group_type in {"special", "sps", "op_ed", "movie"}:
        original = (
            ep.get("original_title")
            or ep.get("display_title")
            or _stem_from_path(raw_path)
            or ep.get("title")
            or group_type.upper()
        )
        ep["display_title"] = original
        ep["episode_title"] = original
        ep["full_title"] = original
        ep["title"] = original
        return

    season = ep.get("season_number") or ep.get("season") or 1
    episode = ep.get("episode_number") or ep.get("episode") or 0
    try:
        season_int = int(season)
        episode_int = int(episode)
        ep_code = f"S{season_int:02d}E{episode_int:02d}"
    except (TypeError, ValueError):
        return

    indexed_title = _clean_episode_display_title(ep.get("nfo_title"), season_int, episode_int)
    indexed_plot = ep.get("episode_plot") or ep.get("plot") or ""
    base_title = work_title or ep.get("work_title") or ep.get("series_group") or ""
    local_title = (
        _clean_episode_display_title(ep.get("episode_title"), season_int, episode_int)
        or _clean_episode_display_title(ep.get("title"), season_int, episode_int)
        or _clean_episode_display_title(ep.get("display_title"), season_int, episode_int)
        or _extract_episode_title_from_standard_filename(raw_path, season_int, episode_int)
    )
    display_title = indexed_title or local_title

    ep["episode_code"] = ep_code
    ep["episode_title"] = display_title or ""
    ep["episode_plot"] = indexed_plot
    ep["display_title"] = display_title or ep_code
    ep["full_title"] = f"{base_title} - {ep_code} - {display_title}" if display_title and base_title else (
        f"{ep_code} - {display_title}" if display_title else (f"{base_title} - {ep_code}" if base_title else ep_code)
    )
    ep["title"] = ep["display_title"]


def _clean_episode_display_title(raw_title: Any, season: int, episode: int) -> str:
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip(" ._-　")
    if not title:
        return ""
    title = _strip_episode_code_prefix(title, season, episode).strip(" ._-　")
    if not title:
        return ""
    if re.fullmatch(rf"(?i).*\bS0?{season}\s*E0?{episode}", title):
        return ""
    if _looks_like_generic_episode_title(title, season, episode):
        return ""
    if _looks_like_raw_filename_title(title):
        return ""
    return title


def _strip_episode_code_prefix(title: str, season: int, episode: int) -> str:
    patterns = [
        rf"(?i)^S0?{season}\s*E0?{episode}\s*[-_:：]\s*",
        rf"(?i)^E0?{episode}\s*[-_:：]\s*",
        rf"^第\s*0?{episode}\s*[集话話]\s*[-_:：]\s*",
    ]
    for pattern in patterns:
        title = re.sub(pattern, "", title).strip()
    return title


def _looks_like_generic_episode_title(title: str, season: int, episode: int) -> bool:
    normalized = re.sub(r"\s+", "", title).casefold()
    generic_values = {
        f"s{season}e{episode}",
        f"s{season:02d}e{episode:02d}",
        f"e{episode}",
        f"e{episode:02d}",
        f"ep{episode}",
        f"ep{episode:02d}",
        f"episode{episode}",
        f"episode{episode:02d}",
        f"第{episode}集",
        f"第{episode:02d}集",
        f"第{episode}话",
        f"第{episode:02d}话",
        f"第{episode}話",
        f"第{episode:02d}話",
    }
    return normalized in generic_values


def _looks_like_raw_filename_title(title: str) -> bool:
    lowered = title.casefold()
    if re.search(r"\.(?:mkv|mp4|avi|mov|wmv|flv|webm|m2ts|ts|strm)$", lowered):
        return True
    if re.search(r"\[[^\]]*(?:\d{3,4}p|x26[45]|h\.?26[45]|hevc|avc|flac|aac|bdrip|webrip|web-dl|ma10p|hi10p|10bit)[^\]]*\]", lowered):
        return True
    if len(re.findall(r"\[[^\]]+\]", title)) >= 2:
        return True
    if re.search(r"(?i)(?:vcb-studio|loli(?:house)?|nekomoe|airota|bdrip|webrip|x264|x265|flac)", title):
        return True
    return False


def _extract_episode_title_from_standard_filename(raw_path: Optional[str], season: int, episode: int) -> str:
    stem = _stem_from_path(raw_path)
    if not stem:
        return ""
    match = re.search(rf"(?i)S0?{season}\s*E0?{episode}(?=$|[\s._\-:：])", stem)
    if not match:
        return ""
    title = stem[match.end():].strip(" ._-:：　")
    if not title:
        return ""
    title = re.sub(
        r"[\[【(（][^\]】)）]*(?:\d{3,4}p|x26[45]|h\.?26[45]|hevc|aac|flac|opus|ma\d+p|hi\d+p|chs|cht|jpn|gb|big5)[^\]】)）]*[\]】)）]",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s+", " ", title).strip(" ._-:：　")
    return _clean_episode_display_title(title, season, episode)


def _stem_from_path(raw_path: Optional[str]) -> str:
    if not raw_path:
        return ""
    try:
        return Path(str(raw_path)).stem
    except (TypeError, ValueError):
        return ""


def _read_episode_nfo(raw_path: Optional[str], season: Any, episode: Any) -> dict:
    if not raw_path:
        return {"title": "", "plot": ""}
    strm_path = Path(str(raw_path))
    candidates = [strm_path.with_suffix(".nfo")]
    try:
        candidates.append(strm_path.parent / f"S{int(season):02d}E{int(episode):02d}.nfo")
    except (TypeError, ValueError):
        pass

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        title_match = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        plot_match = re.search(r"<plot>(.*?)</plot>", text, re.I | re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
        plot = re.sub(r"\s+", " ", plot_match.group(1)).strip() if plot_match else ""
        if title or plot:
            return {"title": title, "plot": plot}
    return {"title": "", "plot": ""}
