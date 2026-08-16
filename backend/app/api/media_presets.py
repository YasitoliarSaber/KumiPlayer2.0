from dataclasses import asdict
from threading import Lock

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.api.imports import _diff_to_dict, _preview_to_dict
from app.import_plan.service import build_preview
from app.media_presets.service import (
    activate_version,
    archive_local_tree,
    archive_upload,
    build_plan_for_snapshot,
    create_preset_from_folder,
    create_preset_record,
    discard_archived_version,
    discard_candidate_snapshot,
    find_matching_preset,
    find_version_by_sha256,
    move_archived_version,
    parse_archived_tree,
    preset_to_dict,
    rebind_preset_source_root,
    scan_local_preset,
    snapshots_have_same_media,
    update_preset_from_tree,
)
from app.media_presets.store import get_preset, list_presets, save_preset
from app.scrape.review_queue import get_pending_review_items
from app.tasks.registry import get_task_manager

router = APIRouter(prefix="/api/media-presets", tags=["media-presets"])


class SourceRootRequest(BaseModel):
    source_root: str


class FolderScanRequest(BaseModel):
    source: str
    source_root: str
    import_family: str
    import_scope: str = ""


class LocalTreeImportRequest(BaseModel):
    tree_path: str
    expected_source: str | None = None
    import_family: str = "anime"
    import_scope: str = ""


class PresetDeletePreviewRequest(BaseModel):
    pass


class PresetDeleteConfirmRequest(BaseModel):
    preview_id: str


_FOLDER_SCAN_LOCK = Lock()


@router.get("")
def get_media_presets():
    presets = list_presets()
    scrape_tasks = [
        task for task in get_task_manager().list_tasks()
        if task.task_type.startswith("scrape_")
    ]
    pending_review = get_pending_review_items()
    payload = []
    for preset in presets:
        data = preset_to_dict(preset)
        task = next(
            (item for item in scrape_tasks if (item.result or {}).get("plan_id") == preset.current_plan_id),
            None,
        )
        data["scrape_task"] = _task_to_dict(task) if task else None
        review_items = [item for item in pending_review if item.import_plan_id == preset.current_plan_id]
        if preset.lifecycle_status == "needs_attention" and not review_items:
            # 兼容旧版 review_queue：旧记录没有 import_plan_id 时，按当前计划目标补齐归属。
            try:
                from app.scrape.service import get_targets
                targets, error = get_targets(preset.source, preset.current_plan_id)
                target_ids = {item.scrape_target_id for item in targets} if not error else set()
                review_items = [
                    item for item in pending_review
                    if item.source == preset.source and item.scrape_target_id in target_ids
                ]
            except Exception:
                review_items = []
        data["review_count"] = len(review_items)
        payload.append(data)
    return {"presets": payload}


def _task_to_dict(task):
    data = asdict(task)
    data["started_at"] = data.get("started_at") or ""
    data["finished_at"] = data.get("finished_at") or ""
    data["error"] = data.get("error") or ""
    return data


@router.post("/scan-folder")
def scan_media_preset_folder(req: FolderScanRequest):
    """以受限元数据扫描从明确选择的新番文件夹建立媒体库。"""
    if not _FOLDER_SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有文件夹扫描正在进行，请完成后再试")
    try:
        preset, version, plan, reused = create_preset_from_folder(
            req.source,
            req.source_root.strip(),
            req.import_family,
            req.import_scope,
        )
        return {
            "preset": preset_to_dict(preset),
            "version": asdict(version),
            "preview": _preview_to_dict(build_preview(plan)),
            "reused_preset": reused,
        }
    finally:
        _FOLDER_SCAN_LOCK.release()




@router.post("/import-local-tree")
def import_local_tree(req: LocalTreeImportRequest):
    """从 Tauri 原生选择器 / 拖放提供的本机绝对 TXT 路径创建现有媒体库预设。

    新根路径合同：用户选择的 TXT 绝对路径是权威事实，TXT 父目录即本次 source_root。
    后端不再通过文件名、配置根组合或多候选试探猜测真实根。
    """
    expected_source = (req.expected_source or "").strip()
    temporary_preset = create_preset_record(
        "pan115",
        "",
        req.import_family,
        req.import_scope,
    )
    try:
        source, version, archive, source_root = archive_local_tree(
            temporary_preset.preset_id,
            req.tree_path,
        )
    except HTTPException as exc:
        _discard_temporary_preset(temporary_preset.preset_id)
        raise exc
    except Exception as exc:
        # 解析失败/路径不可达时不留下预设、快照或归档残留
        _discard_temporary_preset(temporary_preset.preset_id)
        raise HTTPException(status_code=400, detail=f"目录树读取失败: {exc}") from exc
    if expected_source and source != expected_source:
        _discard_temporary_preset(temporary_preset.preset_id)
        raise HTTPException(
            status_code=400,
            detail=f"目录树来源不匹配：当前来源卡选择的是 {expected_source}，但 TXT 正文是 {source} 格式",
        )
    temporary_preset.source = source
    from app.integrations.openlist.providers import compat_ingest, compat_provider
    temporary_preset.provider_id = compat_provider(source)
    temporary_preset.ingest_method = compat_ingest(source)
    try:
        snapshot, _, path_validation = parse_archived_tree(
            source,
            archive,
            source_root,
            req.import_family,
            req.import_scope,
            build_plan=False,
            tree_name=version.original_name,
            source_root_is_exact=True,
        )
    except Exception as exc:
        _discard_temporary_preset(temporary_preset.preset_id)
        raise HTTPException(status_code=400, detail=f"目录树解析失败: {exc}") from exc
    version.path_validation = path_validation.to_dict()
    temporary_preset.source_root = source_root
    temporary_preset.import_family = snapshot.import_family
    temporary_preset.import_scope = snapshot.import_scope
    if snapshot.import_scope != req.import_scope:
        from app.media_presets.service import next_preset_name
        temporary_preset.name = next_preset_name(
            temporary_preset.import_family,
            snapshot.import_scope,
        )
    preset, selected_version, plan, reused, unchanged = _activate_or_reuse_tree(
        temporary_preset,
        version,
        snapshot,
    )
    return {
        "preset": preset_to_dict(preset),
        "version": asdict(selected_version),
        "preview": _preview_to_dict(build_preview(plan)),
        "reused_preset": reused,
        "unchanged": unchanged,
    }


def _discard_temporary_preset(preset_id: str) -> None:
    """清理解析失败/来源不匹配时临时创建的预设与归档目录，避免残留。

    临时预创建后可能尚未保存到索引，delete_preset 会因找不到条目而跳过归档清理，
    因此这里先尝试索引删除，再直接清理受控归档目录。
    """
    try:
        from app.media_presets.store import delete_preset, get_presets_root

        delete_preset(preset_id, delete_archives=True)
        import shutil

        presets_root = get_presets_root()
        archive_dir = presets_root / preset_id
        try:
            archive_dir.relative_to(presets_root)
        except ValueError:
            return
        if archive_dir != presets_root and archive_dir.exists():
            shutil.rmtree(archive_dir)
    except Exception:
        pass


@router.get("/{preset_id}")
def get_media_preset(preset_id: str):
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="媒体库预设不存在")
    return {"preset": preset_to_dict(preset)}


@router.delete("/{preset_id}")
def remove_media_preset(preset_id: str):
    if get_preset(preset_id) is None:
        raise HTTPException(status_code=404, detail="导入卡片不存在")
    raise HTTPException(status_code=409, detail="请先生成删除预览，再确认删除导入卡片")


@router.post("/{preset_id}/delete/preview")
def preview_media_preset_delete(preset_id: str, req: PresetDeletePreviewRequest):
    from app.media_presets.delete import build_preset_delete_preview

    return build_preset_delete_preview(preset_id).to_dict()


@router.post("/{preset_id}/delete/confirm")
def confirm_media_preset_delete(preset_id: str, req: PresetDeleteConfirmRequest):
    from app.media_presets.delete import execute_preset_delete

    return execute_preset_delete(preset_id, req.preview_id)


@router.post("")
async def create_media_preset(
    source: str = Form(...),
    source_root: str = Form(""),
    import_family: str = Form(...),
    import_scope: str = Form(""),
    tree_file: UploadFile = File(...),
):
    preset = create_preset_record(source, source_root, import_family, import_scope)
    version, archive = await archive_upload(preset.preset_id, tree_file)
    snapshot, _, path_validation = parse_archived_tree(
        source,
        archive,
        source_root,
        import_family,
        import_scope,
        build_plan=False,
        tree_name=version.original_name,
    )
    version.path_validation = path_validation.to_dict()
    preset.source_root = snapshot.source_root
    preset.import_family = snapshot.import_family
    preset.import_scope = snapshot.import_scope
    if snapshot.import_scope != import_scope:
        from app.media_presets.service import next_preset_name
        preset.name = next_preset_name(preset.import_family, snapshot.import_scope)
    preset, selected_version, plan, reused, unchanged = _activate_or_reuse_tree(
        preset,
        version,
        snapshot,
    )
    # RWK-17：现有 115/百度 TXT 导入也建立 Provider SourceRoot 并关联 preset，
    # 来源卡才能出现"启用 OpenList 增量"入口（不要求用户手填内部 root_id）。
    if preset.source in {"pan115", "baidu"}:
        try:
            from app.media_presets.service import ensure_provider_source_root, now_iso
            from app.media_presets.store import save_preset as _save_preset
            root_id = ensure_provider_source_root(
                provider=preset.source,
                local_mount_root=preset.source_root or source_root,
                import_family=preset.import_family,
                import_scope=preset.import_scope,
            )
            if root_id and preset.catalog_root_id != root_id:
                preset.catalog_root_id = root_id
                preset.updated_at = now_iso()
                _save_preset(preset)
        except Exception:
            # Provider root 建立失败不阻塞 TXT 导入主流程（旧路径继续可用），
            # 但必须留痕，避免来源卡静默缺失"增量入口"且用户无提示。
            import logging

            logging.getLogger(__name__).warning(
                "TXT 导入时 Provider SourceRoot 关联失败（preset=%s）",
                getattr(preset, "preset_id", ""),
                exc_info=True,
            )
    return {
        "preset": preset_to_dict(preset),
        "version": asdict(selected_version),
        "preview": _preview_to_dict(build_preview(plan)),
        "reused_preset": reused,
        "unchanged": unchanged,
    }


def _activate_or_reuse_tree(provisional, version, snapshot):
    existing = find_matching_preset(
        snapshot.source,
        snapshot.source_root,
        snapshot.import_family,
        snapshot.import_scope,
        tree_name=version.original_name,
        sha256=version.sha256,
    )
    if existing is None:
        plan = build_plan_for_snapshot(snapshot)
        activate_version(provisional, version, snapshot, plan)
        return provisional, version, plan, False, False

    duplicate = find_version_by_sha256(existing, version.sha256)
    if duplicate is not None:
        discard_archived_version(version)
        discard_candidate_snapshot(snapshot.snapshot_id)
        from app.import_plan.store import load_import_plan
        plan = load_import_plan(plan_id=existing.current_plan_id)
        if plan is None:
            raise HTTPException(status_code=409, detail="已有媒体库缺少当前导入计划，请使用该卡片导入新版")
        return existing, duplicate, plan, True, True

    from app.raw.store import load_raw_snapshot
    current_snapshot = load_raw_snapshot(existing.current_snapshot_id)
    if snapshots_have_same_media(current_snapshot, snapshot):
        discard_archived_version(version)
        discard_candidate_snapshot(snapshot.snapshot_id)
        from app.import_plan.store import load_import_plan
        plan = load_import_plan(plan_id=existing.current_plan_id)
        if plan is None:
            raise HTTPException(status_code=409, detail="已有媒体库缺少当前导入计划，请使用该卡片导入新版")
        selected_version = next(
            (item for item in existing.versions if item.version_id == existing.current_version_id),
            existing.versions[-1],
        )
        return existing, selected_version, plan, True, True

    move_archived_version(version, existing.preset_id)
    plan, _ = update_preset_from_tree(existing, version, snapshot)
    return existing, version, plan, True, False


@router.post("/{preset_id}/source-root")
def rebind_media_preset_source_root(preset_id: str, req: SourceRootRequest):
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="媒体库预设不存在")
    _, plan, version, _ = rebind_preset_source_root(preset, req.source_root.strip())
    return {
        "preset": preset_to_dict(preset),
        "version": asdict(version),
        "preview": _preview_to_dict(build_preview(plan)),
    }


@router.post("/{preset_id}/revalidate")
def revalidate_media_preset_source_root(preset_id: str):
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="媒体库预设不存在")
    _, plan, version, _ = rebind_preset_source_root(preset, preset.source_root)
    return {
        "preset": preset_to_dict(preset),
        "version": asdict(version),
        "preview": _preview_to_dict(build_preview(plan)),
    }


@router.post("/{preset_id}/rescan-local")
def rescan_local_media_preset(preset_id: str):
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="媒体库预设不存在")
    if preset.source != "local" or preset.update_mode != "local_scan":
        raise HTTPException(status_code=409, detail="该媒体库不是本地扫描卡片")
    if not _FOLDER_SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有本地目录扫描正在进行，请完成后再试")
    try:
        updated, version, plan, diff, reused, unchanged = scan_local_preset(
            preset.source_root,
            preset.import_family,
            preset.import_scope,
            preset=preset,
        )
        response = {
            "preset": preset_to_dict(updated),
            "version": asdict(version),
            "preview": _preview_to_dict(build_preview(plan)),
            "reused_preset": reused,
            "unchanged": unchanged,
        }
        if diff is not None:
            response["diff"] = _diff_to_dict(diff)
        return response
    finally:
        _FOLDER_SCAN_LOCK.release()


@router.post("/{preset_id}/updates")
async def update_media_preset(preset_id: str, tree_file: UploadFile = File(...)):
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="媒体库预设不存在")
    version, archive = await archive_upload(preset.preset_id, tree_file)
    snapshot, _, path_validation = parse_archived_tree(
        preset.source,
        archive,
        preset.source_root,
        preset.import_family,
        preset.import_scope,
        build_plan=False,
        tree_name=version.original_name,
    )
    version.path_validation = path_validation.to_dict()
    cumulative, diff = update_preset_from_tree(preset, version, snapshot)
    return {
        "preset": preset_to_dict(preset),
        "version": asdict(version),
        "diff": _diff_to_dict(diff),
        "preview": _preview_to_dict(build_preview(cumulative)),
    }


class LocalTreeUpdateRequest(BaseModel):
    tree_path: str
    expected_source: str | None = None


@router.post("/{preset_id}/updates-from-path")
def update_media_preset_from_path(preset_id: str, req: LocalTreeUpdateRequest):
    """从本机绝对 TXT 路径更新已有预设（新根路径合同：TXT 父目录为新 source_root）。

    状态机：
    1. 读取 TXT 源码（正文识别来源），校验 source == preset.source，不一致则 400 并清理归档；
    2. 相同 SHA-256 更新返回 unchanged=true，不创建重复版本，清理新归档；
    3. 解析、安全阻断失败时，preset.source_root / current_version_id / version_count 均不得变化，
       刚归档的版本与候选快照必须清理；
    4. 全部通过后激活新版本，并仅在此刻把 preset.source_root 持久化为新 TXT 父目录。
    """
    from app.core.error_log import log_error
    from app.import_plan.store import load_import_plan
    from app.media_presets.service import (
        discard_archived_version,
        find_version_by_sha256,
    )

    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="媒体库预设不存在")
    expected_source = (req.expected_source or "").strip()
    try:
        source, version, archive, source_root = archive_local_tree(
            preset.preset_id,
            req.tree_path,
        )
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"目录树读取失败: {exc}") from exc

    def _cleanup_archived_version() -> None:
        try:
            discard_archived_version(version)
        except Exception:
            pass

    # 强制 TXT 来源等于预设来源（不匹配即 400，且不留下版本）
    if source != preset.source:
        _cleanup_archived_version()
        log_error(
            stage="media_presets",
            category="source_mismatch",
            message=f"更新来源不匹配：预设为 {preset.source}，TXT 为 {source}",
            level="warning",
            source=preset.source,
        )
        raise HTTPException(
            status_code=400,
            detail=f"目录树来源不匹配：预设为 {preset.source}，但 TXT 正文是 {source} 格式",
        )

    # expected_source 与预设来源不一致时也拒绝（前端已用预设来源，此为主线程要求的双保险）
    if expected_source and source != expected_source:
        _cleanup_archived_version()
        raise HTTPException(
            status_code=400,
            detail=f"目录树来源不匹配：预期 {expected_source}，但 TXT 正文是 {source} 格式",
        )

    # 相同 SHA-256 去重：不改预设、不新增版本、不创建重复归档
    duplicate = find_version_by_sha256(preset, version.sha256)
    if duplicate is not None:
        _cleanup_archived_version()
        duplicate_preview = None
        if duplicate.plan_id:
            import_plan = load_import_plan(duplicate.plan_id)
            if import_plan is not None:
                duplicate_preview = _preview_to_dict(build_preview(import_plan))
        return {
            "preset": preset_to_dict(preset),
            "version": asdict(duplicate),
            "preview": duplicate_preview,
            "reused_preset": True,
            "unchanged": True,
        }

    # 解析新快照（失败不改变预设状态）
    try:
        snapshot, _, path_validation = parse_archived_tree(
            preset.source,
            archive,
            source_root,
            preset.import_family,
            preset.import_scope,
            build_plan=False,
            tree_name=version.original_name,
            source_root_is_exact=True,
        )
    except HTTPException as exc:
        _cleanup_archived_version()
        raise exc
    except Exception as exc:
        _cleanup_archived_version()
        raise HTTPException(status_code=400, detail=f"目录树解析失败: {exc}") from exc
    version.path_validation = path_validation.to_dict()

    # 计算差异与安全阻断（失败不持久化任何 preset 变更）——
    # persist_on_block=False 时安全阻断不会 append 版本或 save。
    try:
        cumulative, diff = update_preset_from_tree(
            preset,
            version,
            snapshot,
            persist_on_block=False,
        )
    except HTTPException as exc:
        _cleanup_archived_version()
        if exc.status_code == 409:
            raise exc
        raise

    # 成功激活新版本后才持久化新的 source_root
    preset.source_root = source_root
    preset.updated_at = version.created_at
    save_preset(preset)
    return {
        "preset": preset_to_dict(preset),
        "version": asdict(version),
        "diff": _diff_to_dict(diff),
        "preview": _preview_to_dict(build_preview(cumulative)),
    }
