# -*- coding: utf-8 -*-
"""手动删除逻辑

删除预览和确认执行。
只删除 mirror root 内文件，绝不删除真实网盘源文件。
"""

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from app.catalog.lifecycle import CatalogCleanupBusyError  # noqa: F401
from app.core.paths import get_cache_dir, get_data_dir, get_mirror_root


_SOURCE_NAMESPACES = {
    "pan115": "115",
    "baidu": "baidu",
    "local": "local",
    "openlist": "openlist",
}


# ============================================================
# 数据结构
# ============================================================

@dataclass
class DeleteFile:
    """待删除文件"""
    path: str = ""
    kind: str = ""  # strm / nfo / poster / fanart / clearlogo / other
    exists: bool = True
    allowed: bool = True
    reason: str = ""


@dataclass
class DeletePreview:
    """删除预览"""
    preview_id: str = ""
    source: str = ""
    scope: str = ""  # library / work / season / episode
    work_id: str = ""
    files: List[DeleteFile] = field(default_factory=list)
    empty_dirs: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blocked: bool = False
    retained_work_ids: List[str] = field(default_factory=list)
    library_work_count: int = 0
    media_preset_count: int = 0
    tracking_binding_count: int = 0
    tracking_scan_run_count: int = 0
    history_count: int = 0
    progress_count: int = 0
    related_reference_count: int = 0
    catalog_root_count: int = 0
    catalog_batch_count: int = 0
    catalog_directory_count: int = 0
    catalog_node_count: int = 0
    catalog_unit_count: int = 0
    catalog_revision_count: int = 0
    catalog_job_count: int = 0
    catalog_active_job_count: int = 0


@dataclass
class DeleteFailure:
    """删除失败记录"""
    path: str = ""
    reason: str = ""


@dataclass
class DeleteResult:
    """删除结果"""
    preview_id: str = ""
    status: str = ""  # succeeded / partial_failed / failed
    deleted: List[str] = field(default_factory=list)
    failed: List[DeleteFailure] = field(default_factory=list)
    skipped: List[DeleteFailure] = field(default_factory=list)
    empty_dirs_removed: List[str] = field(default_factory=list)
    library_rescanned: bool = False
    deleted_library_work_count: int = 0
    deleted_preset_ids: List[str] = field(default_factory=list)
    deleted_tracking_binding_count: int = 0
    deleted_tracking_scan_run_count: int = 0
    cancelled_tracking_task_count: int = 0
    deleted_catalog_root_count: int = 0
    deleted_catalog_batch_count: int = 0
    deleted_catalog_unit_count: int = 0
    deleted_catalog_revision_count: int = 0
    deleted_catalog_job_count: int = 0


# ============================================================
# 辅助函数
# ============================================================

def _make_preview_id(work_id: str, scope: str) -> str:
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    content = f"{work_id}:{scope}:{now}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:16]


def _classify_asset(path: Path) -> str:
    """分类 asset 类型"""
    name = path.name.lower()
    if name.endswith(".strm"):
        return "strm"
    if name == "tvshow.nfo" or name == "movie.nfo":
        return "nfo"
    if name in ("poster.jpg", "poster.png"):
        return "poster"
    if name in ("fanart.jpg", "fanart.png"):
        return "fanart"
    if name == "clearlogo.png":
        return "clearlogo"
    return "other"


def _is_under_mirror_root(path: Path, mirror_root: Optional[Path] = None) -> bool:
    """检查路径是否在 mirror root 下"""
    mirror_root = (mirror_root or get_mirror_root()).resolve()
    try:
        path.resolve().relative_to(mirror_root)
        return True
    except ValueError:
        return False


def _sort_dirs_deep_first(paths: List[str]) -> List[str]:
    """按路径深度从深到浅排序，兼容 Windows / POSIX 分隔符。"""
    return sorted(set(paths), key=lambda d: len(Path(d).parts), reverse=True)


def _configured_source_roots() -> list[str]:
    """读取一次配置并规范化所有真实源根目录。

    OpenList 的真实媒体位于本地挂载根（WebDAV / WinFSP 挂载盘），
    必须纳入保护，清理时绝不触碰。
    """
    from app.core.config import load_config

    config = load_config()
    return [
        os.path.normcase(_safe_resolve_str(Path(root)))
        for root in (
            config.pan115_root,
            config.baidu_root,
            config.local_root,
            getattr(config, "openlist_mount_root", "") or "",
        )
        if root
    ]


def _is_source_path(path: Path, source_roots: Optional[list[str]] = None) -> bool:
    """检查路径是否是真实源路径（网盘挂载路径）"""
    source_roots = source_roots if source_roots is not None else _configured_source_roots()
    path_str = os.path.normcase(_safe_resolve_str(path))
    for root_str in source_roots:
        try:
            if os.path.commonpath([path_str, root_str]) == root_str:
                return True
        except ValueError:
            continue
        if path_str == root_str:
            return True
    return False


def _safe_resolve_str(path: Path) -> str:
    """Resolve a path for safety checks without failing on offline drives."""
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


# ============================================================
# 删除预览
# ============================================================

def build_delete_preview(
    work_id: str,
    episode_ids: Optional[List[str]] = None,
    strm_paths: Optional[List[str]] = None,
    delete_assets: bool = True,
    remove_empty_dirs: bool = True,
) -> DeletePreview:
    """生成删除预览

    参数:
        work_id: 作品 ID（从 LibraryIndex 获取）
        episode_ids: 要删除的 episode ID 列表
        strm_paths: 要删除的 .strm 路径列表（必须在 LibraryIndex 中）
        delete_assets: 是否同时删除 NFO / 图片
        remove_empty_dirs: 是否删除空目录
    """
    from app.library.store import load_library_index

    preview_id = _make_preview_id(work_id, "episode" if episode_ids else "work")
    mirror_root = get_mirror_root()
    warnings = []
    files: List[DeleteFile] = []
    blocked = False

    # 加载 LibraryIndex
    index = load_library_index()
    if index is None:
        return DeletePreview(
            preview_id=preview_id,
            work_id=work_id,
            blocked=True,
            warnings=["LibraryIndex 不存在，请先 rescan"],
        )

    # 查找 work
    work = None
    for w in index.works:
        if w.work_id == work_id:
            work = w
            break
    if work is None:
        return DeletePreview(
            preview_id=preview_id,
            work_id=work_id,
            blocked=True,
            warnings=[f"work_id 不存在: {work_id}"],
        )

    # 收集要删除的 strm 路径
    target_strm_paths: List[str] = []
    scope = "work"

    if episode_ids:
        scope = "episode"
        requested = set(episode_ids)
        found = set()
        for ep in work.episodes:
            if ep.episode_id in episode_ids:
                found.add(ep.episode_id)
                if ep.strm_path:
                    target_strm_paths.append(ep.strm_path)
        missing_episode_ids = sorted(requested - found)
        if missing_episode_ids:
            blocked = True
            warnings.append(f"episode_id 不存在: {', '.join(missing_episode_ids)}")
    elif strm_paths:
        scope = "episode"
        # 校验 strm_path 属于该 work
        work_strm_set = {ep.strm_path for ep in work.episodes if ep.strm_path}
        for p in strm_paths:
            if p in work_strm_set:
                target_strm_paths.append(p)
            else:
                blocked = True
                warnings.append(f"strm_path 不属于 work {work_id}: {p}")
    else:
        # 删除整个 work
        scope = "work"
        for ep in work.episodes:
            if ep.strm_path:
                target_strm_paths.append(ep.strm_path)
        target_strm_paths.extend(_work_plan_strm_paths(work))

    if not target_strm_paths and scope != "work":
        blocked = True
        warnings.append("未找到可删除的 LibraryIndex 条目")
    elif not target_strm_paths:
        warnings.append("未找到生成的播放文件，将只清理作品索引和观看状态")

    # 构建删除文件列表（去重）
    seen_paths: set = set()

    for strm_path in target_strm_paths:
        p = Path(strm_path)

        # 路径安全检查
        if not _is_under_mirror_root(p):
            if str(p) not in seen_paths:
                files.append(DeleteFile(
                    path=str(p),
                    kind="strm",
                    exists=p.exists(),
                    allowed=False,
                    reason="路径不在 mirror root 下",
                ))
                seen_paths.add(str(p))
            blocked = True
            continue

        if _is_source_path(p):
            if str(p) not in seen_paths:
                files.append(DeleteFile(
                    path=str(p),
                    kind="strm",
                    exists=p.exists(),
                    allowed=False,
                    reason="不允许删除真实源路径",
                ))
                seen_paths.add(str(p))
            blocked = True
            continue

        if str(p) not in seen_paths:
            files.append(DeleteFile(
                path=str(p),
                kind="strm",
                exists=p.exists(),
                allowed=True,
            ))
            seen_paths.add(str(p))

        # 同目录下的 asset 文件
        if delete_assets and p.parent.exists():
            for asset_name in ("tvshow.nfo", "movie.nfo", "poster.jpg", "poster.png",
                               "fanart.jpg", "fanart.png", "clearlogo.png"):
                asset_path = p.parent / asset_name
                if str(asset_path) in seen_paths:
                    continue
                if asset_path.exists():
                    allowed = _is_under_mirror_root(asset_path) and not _is_source_path(asset_path)
                    files.append(DeleteFile(
                        path=str(asset_path),
                        kind=_classify_asset(asset_path),
                        exists=True,
                        allowed=allowed,
                        reason="" if allowed else "asset 路径不在 mirror root 下或属于真实源路径",
                    ))
                    if not allowed:
                        blocked = True
                    seen_paths.add(str(asset_path))

    # 空目录预览
    empty_dirs: List[str] = []
    if remove_empty_dirs:
        dirs_to_check: set[str] = set()
        for f in files:
            if f.allowed:
                current = Path(f.path).parent
                mirror_root_resolved = mirror_root.resolve()
                while True:
                    if current.resolve() == mirror_root_resolved or current.parent == current:
                        break
                    if _is_under_mirror_root(current):
                        dirs_to_check.add(str(current))
                    current = current.parent

        # 删除执行后这些目录可能变空，预览阶段先列出候选目录。
        empty_dirs = _sort_dirs_deep_first(list(dirs_to_check))

    history_count, progress_count, related_reference_count = _count_work_state(index, work_id)
    return DeletePreview(
        preview_id=preview_id,
        source=work.source,
        scope=scope,
        work_id=work_id,
        files=files,
        empty_dirs=empty_dirs,
        warnings=warnings,
        blocked=blocked,
        history_count=history_count,
        progress_count=progress_count,
        related_reference_count=related_reference_count,
    )


def build_library_clear_preview(source: Optional[str] = None) -> DeletePreview:
    """生成整库清空预览。

    只扫描 mirror root 内的生成文件。这里的“媒体库”指 KumiPlayer 生成的
    mirror / STRM / NFO / 图片文件，不包含真实网盘或本地源文件。
    """
    clear_source = _normalize_clear_source(source)
    library_work_count = _count_library_works_for_clear(clear_source)
    media_preset_count = _count_media_presets_for_clear(clear_source)
    tracking_counts = _count_tracking_state_for_clear(clear_source)
    # Source Catalog 统计独立于镜像目录：即使镜像不存在/为空，
    # 数据库中的来源根与派生事实也必须预告并清理。
    try:
        from app.catalog import lifecycle
        catalog_preview = lifecycle.preview_catalog_cleanup(clear_source)
    except Exception:
        catalog_preview = lifecycle.CatalogCleanupPreview()
    # DeletePreview.catalog_* ← CatalogCleanupPreview 短字段名映射
    _catalog_preview_fields = {
        "catalog_root_count": "root_count",
        "catalog_batch_count": "batch_count",
        "catalog_directory_count": "directory_count",
        "catalog_node_count": "node_count",
        "catalog_unit_count": "unit_count",
        "catalog_revision_count": "revision_count",
        "catalog_job_count": "job_count",
        "catalog_active_job_count": "active_job_count",
    }

    def _catalog_kwargs() -> dict:
        return {
            target: getattr(catalog_preview, source)
            for target, source in _catalog_preview_fields.items()
        }

    preview_id = _make_preview_id(f"__library__:{clear_source}", "library")
    mirror_root = get_mirror_root().resolve()
    target_root = _clear_target_root(mirror_root, clear_source)
    source_roots = _configured_source_roots()
    files: List[DeleteFile] = []
    warnings: List[str] = []
    blocked = False

    if not target_root.exists():
        return DeletePreview(
            preview_id=preview_id,
            source=clear_source,
            scope="library",
            work_id="__library__",
            warnings=[f"生成媒体库目录不存在: {target_root}，确认后会清理可重建缓存"],
            blocked=False,
            library_work_count=library_work_count,
            media_preset_count=media_preset_count,
            tracking_binding_count=tracking_counts["binding_count"],
            tracking_scan_run_count=tracking_counts["scan_run_count"],
            **_catalog_kwargs(),
        )

    if not target_root.is_dir():
        return DeletePreview(
            preview_id=preview_id,
            source=clear_source,
            scope="library",
            work_id="__library__",
            warnings=[f"生成媒体库目录不是目录: {target_root}"],
            blocked=True,
            library_work_count=library_work_count,
            media_preset_count=media_preset_count,
            tracking_binding_count=tracking_counts["binding_count"],
            tracking_scan_run_count=tracking_counts["scan_run_count"],
            **_catalog_kwargs(),
        )

    for path in sorted((p for p in target_root.rglob("*") if p.is_file()), key=lambda p: str(p)):
        allowed = _is_under_mirror_root(path, mirror_root) and not _is_source_path(path, source_roots)
        files.append(DeleteFile(
            path=str(path),
            kind=_classify_asset(path),
            exists=path.exists(),
            allowed=allowed,
            reason="" if allowed else "路径不在 mirror root 下或属于真实源路径",
        ))
        if not allowed:
            blocked = True

    if not files:
        warnings.append("媒体库镜像目录为空，确认后仍会清理可重建缓存")

    empty_dirs = _sort_dirs_deep_first([
        str(p)
        for p in target_root.rglob("*")
        if p.is_dir() and p.resolve() != target_root
    ])

    return DeletePreview(
        preview_id=preview_id,
        source=clear_source,
        scope="library",
        work_id="__library__",
        files=files,
        empty_dirs=empty_dirs,
        warnings=warnings,
        blocked=blocked,
        library_work_count=library_work_count,
        media_preset_count=media_preset_count,
        tracking_binding_count=tracking_counts["binding_count"],
        tracking_scan_run_count=tracking_counts["scan_run_count"],
        **_catalog_kwargs(),
    )


def _normalize_clear_source(source: Optional[str]) -> str:
    value = (source or "all").strip().lower()
    if value in {"", "all"}:
        return "all"
    if value not in _SOURCE_NAMESPACES:
        raise ValueError(f"不支持的媒体库来源: {source}")
    return value


def _clear_target_root(mirror_root: Path, source: str) -> Path:
    if source == "all":
        return mirror_root
    return mirror_root / _SOURCE_NAMESPACES[source]


# ============================================================
# 删除确认
# ============================================================

def execute_delete(preview: DeletePreview) -> DeleteResult:
    """执行删除

    只删除 preview 中 allowed=true 的路径。
    每个文件单独删除并记录结果。
    删除后重建 LibraryIndex。
    """
    deleted: List[str] = []
    failed: List[DeleteFailure] = []
    skipped: List[DeleteFailure] = []
    empty_dirs_removed: List[str] = []

    if preview.blocked:
        return DeleteResult(
            preview_id=preview.preview_id,
            status="failed",
            skipped=[DeleteFailure(path="", reason="删除预览已被 blocked，拒绝执行")],
            library_rescanned=False,
        )

    unsafe_paths = _validate_preview_paths_for_execution(preview)
    if unsafe_paths:
        return DeleteResult(
            preview_id=preview.preview_id,
            status="failed",
            skipped=unsafe_paths,
            library_rescanned=False,
        )

    if preview.scope == "library":
        return _execute_library_clear(preview)
    # 删除文件
    for f in preview.files:
        if not f.allowed:
            skipped.append(DeleteFailure(path=f.path, reason=f.reason or "不允许删除"))
            continue

        if not f.exists:
            skipped.append(DeleteFailure(path=f.path, reason="文件不存在"))
            continue

        p = Path(f.path)
        try:
            p.unlink()
            deleted.append(f.path)
        except (OSError, PermissionError) as e:
            failed.append(DeleteFailure(path=f.path, reason=str(e)))

    # 删除空目录（从深到浅）
    mirror_root = get_mirror_root().resolve()
    for dir_path in _sort_dirs_deep_first(preview.empty_dirs):
        dp = Path(dir_path)
        if dp.resolve(strict=False) != mirror_root and dp.exists() and dp.is_dir():
            try:
                if not any(dp.iterdir()):
                    dp.rmdir()
                    empty_dirs_removed.append(dir_path)
            except OSError:
                pass

    # 确定状态
    if not failed and not skipped:
        status = "succeeded"
    elif deleted:
        status = "partial_failed"
    else:
        status = "failed"

    # 整作删除直接移除卡片和所有引用；分集删除仍按原流程重建索引。
    library_rescanned = False
    deleted_library_work_count = 0
    try:
        if preview.scope == "work":
            state_failures, removed = _delete_work_state(preview.work_id, preview.source)
            failed.extend(state_failures)
            deleted_library_work_count = 1 if removed else 0
        else:
            deleted_library_work_count = 0
            if preview.work_id:
                failed.extend(_clear_work_scrape_state(preview.source, preview.work_id))
            from app.library import service as library_service
            library_service.rescan_library(preview.source or None)
        library_rescanned = True
    except Exception as e:
        failed.append(DeleteFailure(path="LibraryIndex", reason=f"重建 LibraryIndex 失败: {e}"))
        if status == "succeeded":
            status = "partial_failed"

    if failed:
        status = "partial_failed" if deleted or library_rescanned else "failed"
    elif preview.scope == "work" and deleted_library_work_count:
        # 缺失的生成文件属于可接受的幂等状态；本地索引和观看状态已完整删除。
        status = "succeeded"

    return DeleteResult(
        preview_id=preview.preview_id,
        status=status,
        deleted=deleted,
        failed=failed,
        skipped=skipped,
        empty_dirs_removed=empty_dirs_removed,
        library_rescanned=library_rescanned,
        deleted_library_work_count=deleted_library_work_count,
    )


def _count_work_state(index, work_id: str) -> tuple[int, int, int]:
    try:
        from app.playback.history import load_history
        history_count = sum(item.work_id == work_id for item in load_history())
    except Exception:
        history_count = 0
    try:
        from app.playback.progress import load_progress
        progress_count = sum(item.work_id == work_id for item in load_progress())
    except Exception:
        progress_count = 0
    related_count = sum(
        relation.work_id == work_id
        for work in index.works
        if work.work_id != work_id
        for relation in work.related_works
    )
    return history_count, progress_count, related_count


def _delete_work_state(work_id: str, source: str) -> tuple[list[DeleteFailure], bool]:
    """清理作品卡片的全部本地引用；不读取或删除真实媒体文件。"""
    from dataclasses import asdict

    from app.core.atomic_json import write_json_atomic
    from app.library.deleted_works import mark_work_deleted, refresh_source_summary
    from app.library.store import load_library_index, save_library_index

    failures: list[DeleteFailure] = []
    index = load_library_index()
    if index is None:
        return [DeleteFailure(path="LibraryIndex", reason="LibraryIndex 不存在")], False
    target = next((work for work in index.works if work.work_id == work_id), None)
    if target is None:
        return [DeleteFailure(path="LibraryIndex", reason=f"作品不存在: {work_id}")], False

    target_ids = {season.scrape_target_id for season in target.seasons if season.scrape_target_id}
    removed_scrape_target_ids: set[str] = set()
    failures.extend(_clear_work_scrape_state(
        source,
        work_id,
        target_ids=target_ids,
        target_paths=_work_scrape_paths(target),
        removed_target_ids_out=removed_scrape_target_ids,
    ))

    try:
        from app.playback.history import _get_history_path, load_history
        remaining = [item for item in load_history() if item.work_id != work_id]
        write_json_atomic(_get_history_path(), [asdict(item) for item in remaining])
    except Exception as exc:
        failures.append(DeleteFailure(path="playback/history.json", reason=str(exc)))

    try:
        from app.playback.progress import _write_progress, load_progress
        _write_progress([item for item in load_progress() if item.work_id != work_id])
    except Exception as exc:
        failures.append(DeleteFailure(path="playback/progress.json", reason=str(exc)))

    try:
        from app.library.watch_status import load_watch_statuses, save_watch_statuses
        statuses = load_watch_statuses()
        statuses.pop(work_id, None)
        save_watch_statuses(statuses)
    except Exception as exc:
        failures.append(DeleteFailure(path="watch_status.json", reason=str(exc)))

    try:
        from app.integrations.bangumi import load_state, save_state
        state = load_state()
        state.matches = [item for item in state.matches if item.work_id != work_id]
        state.episode_sync = [item for item in state.episode_sync if item.work_id != work_id]
        save_state(state)
    except Exception as exc:
        failures.append(DeleteFailure(path="bangumi_state.json", reason=str(exc)))

    failures.extend(_clear_work_database_state(work_id, removed_scrape_target_ids))
    failures.extend(_remove_work_user_assets(work_id))

    if failures:
        return failures, False

    try:
        mark_work_deleted(work_id)
        index.works = [work for work in index.works if work.work_id != work_id]
        for work in index.works:
            work.related_works = [item for item in work.related_works if item.work_id != work_id]
        refresh_source_summary(index, removed_works=[target])
        save_library_index(index)
    except Exception as exc:
        failures.append(DeleteFailure(path="LibraryIndex", reason=str(exc)))
        return failures, False
    return failures, True


def _clear_work_database_state(
    work_id: str,
    scrape_target_ids: set[str] | None = None,
) -> list[DeleteFailure]:
    try:
        from app.db.database import close_connection, get_connection, init_db

        init_db()
        conn = get_connection()
        try:
            binding_rows = conn.execute(
                "SELECT binding_id FROM tracking_bindings WHERE work_id = ?", (work_id,)
            ).fetchall()
            binding_ids = [str(row[0]) for row in binding_rows]
            if binding_ids:
                marks = ",".join("?" for _ in binding_ids)
                conn.execute(f"DELETE FROM tracking_scan_runs WHERE binding_id IN ({marks})", binding_ids)
            conn.execute("DELETE FROM tracking_scan_runs WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM tracking_bindings WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM playback_history WHERE work_id = ?", (work_id,))
            conn.execute("DELETE FROM work_overrides WHERE work_id = ?", (work_id,))
            # V3 刮削事实随作品一起清除：reviews/failures 先按 binding 归属删除，
            # 避免 bindings 删除后留下孤儿行（学习查询与跨 revision 归属不再读到）
            conn.execute(
                "DELETE FROM scrape_failures WHERE binding_id IN "
                "(SELECT binding_id FROM scrape_bindings WHERE work_id = ?)",
                (work_id,),
            )
            conn.execute(
                "DELETE FROM scrape_reviews WHERE binding_id IN "
                "(SELECT binding_id FROM scrape_bindings WHERE work_id = ?)",
                (work_id,),
            )
            conn.execute("DELETE FROM scrape_bindings WHERE work_id = ?", (work_id,))
            target_ids = sorted(scrape_target_ids or set())
            if target_ids:
                marks = ",".join("?" for _ in target_ids)
                conn.execute(
                    f"DELETE FROM scrape_review_queue WHERE scrape_target_id IN ({marks})",
                    target_ids,
                )
                conn.execute(
                    f"DELETE FROM scrape_candidate_cache WHERE scrape_target_id IN ({marks})",
                    target_ids,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            close_connection()
    except Exception as exc:
        return [DeleteFailure(path="state.db", reason=str(exc))]
    return []


def _remove_work_user_assets(work_id: str) -> list[DeleteFailure]:
    import hashlib

    from app.core.paths import sanitize_filename

    root = (get_data_dir() / "user_assets").resolve()
    safe_work = sanitize_filename(work_id)[:80]
    digest = hashlib.sha1(work_id.encode("utf-8")).hexdigest()[:8]
    directory = root / f"{safe_work}_{digest}"
    if not directory.exists():
        return []
    try:
        directory.resolve().relative_to(root)
        shutil.rmtree(directory)
    except (OSError, ValueError) as exc:
        return [DeleteFailure(path=str(directory), reason=str(exc))]
    return []


def _validate_preview_paths_for_execution(preview: DeletePreview) -> List[DeleteFailure]:
    """确认时依据当前配置重新检查所有文件和空目录候选。"""
    mirror_root = get_mirror_root().resolve()
    source_roots = _configured_source_roots()
    failures: List[DeleteFailure] = []
    for item in preview.files:
        path = Path(item.path)
        if not item.allowed or not _is_under_mirror_root(path, mirror_root) or _is_source_path(path, source_roots):
            failures.append(DeleteFailure(path=item.path, reason="确认阶段文件路径安全检查失败"))
    for value in preview.empty_dirs:
        path = Path(value)
        if path.resolve(strict=False) == mirror_root:
            continue
        if not _is_under_mirror_root(path, mirror_root) or _is_source_path(path, source_roots):
            failures.append(DeleteFailure(path=value, reason="确认阶段空目录路径安全检查失败"))
    return failures


def _clear_work_scrape_state(
    source: str,
    work_id: str,
    target_ids: set[str] | None = None,
    target_paths: set[str] | None = None,
    removed_target_ids_out: set[str] | None = None,
) -> list[DeleteFailure]:
    """Remove scrape/review state for a deleted work card."""
    failures: list[DeleteFailure] = []
    if not work_id:
        return failures

    try:
        from app.scrape.store import load_scrape_map
        scrape_map = load_scrape_map()
        requested_target_ids = set(target_ids or set())
        normalized_target_paths = {
            os.path.normcase(_safe_resolve_str(Path(path)))
            for path in (target_paths or set())
            if path
        }

        def matches_target_path(item) -> bool:
            if not item.nfo_path or not normalized_target_paths:
                return False
            item_path = Path(item.nfo_path)
            normalized_item = os.path.normcase(_safe_resolve_str(item_path))
            return normalized_item in normalized_target_paths

        removed_target_ids = requested_target_ids | {
            item.scrape_target_id
            for item in scrape_map.items
            if (
                item.scrape_target_id in requested_target_ids
                or (item.work_id == work_id and (not source or item.source == source))
                or matches_target_path(item)
            )
        }
        remaining_map_items = [
            item for item in scrape_map.items
            if item.scrape_target_id not in removed_target_ids
            and not (item.work_id == work_id and (not source or item.source == source))
            and not matches_target_path(item)
        ]
    except Exception as e:
        removed_target_ids = set()
        remaining_map_items = []
        failures.append(DeleteFailure(path="scrape_map.json", reason=str(e)))

    if failures:
        return failures

    try:
        from app.scrape.review_queue import load_review_queue, save_review_queue
        queue = load_review_queue()
        queue.items = [
            item for item in queue.items
            if item.scrape_target_id not in removed_target_ids
            and not ((not source or item.source == source) and item.scrape_target_id == work_id)
        ]
        save_review_queue(queue)
    except Exception as e:
        failures.append(DeleteFailure(path="review_queue.json", reason=str(e)))
        return failures

    try:
        from app.scrape.store import save_scrape_map

        scrape_map.items = remaining_map_items
        save_scrape_map(scrape_map)
        if removed_target_ids_out is not None:
            removed_target_ids_out.update(removed_target_ids)
    except Exception as e:
        failures.append(DeleteFailure(path="scrape_map.json", reason=str(e)))

    return failures


def _work_scrape_paths(work) -> set[str]:
    """收集仅属于当前卡片的镜像文件和直属目录，用于清理聚合前的刮削 ID。"""
    paths = {work.dir_path} if work.dir_path else set()
    for season in work.seasons:
        if season.nfo_path:
            paths.update({season.nfo_path, str(Path(season.nfo_path).parent)})
    for episode in work.episodes:
        if episode.nfo_path:
            paths.update({episode.nfo_path, str(Path(episode.nfo_path).parent)})
        if episode.strm_path:
            paths.add(str(Path(episode.strm_path).parent))
    for location in (work.source_locations or {}).values():
        if not isinstance(location, dict):
            continue
        strm_path = str(location.get("strm_path") or "")
        if strm_path:
            paths.add(str(Path(strm_path).parent))
    return {path for path in paths if path}


def _work_plan_strm_paths(work) -> set[str]:
    """从可用 ImportPlan 补齐跨来源去重后未直接显示的镜像播放文件。"""
    from app.import_plan.store import load_import_plan
    from app.library.index import _library_work_id

    known_items = {
        (
            episode.source or work.source,
            episode.episode_id,
            os.path.normcase(_safe_resolve_str(Path(episode.strm_path))),
        )
        for episode in work.episodes
        if episode.episode_id and episode.strm_path
    }
    known_items.update({
        (
            source,
            str(location.get("episode_id") or ""),
            os.path.normcase(_safe_resolve_str(Path(str(location.get("strm_path") or "")))),
        )
        for source, location in (work.source_locations or {}).items()
        if isinstance(location, dict)
        and location.get("episode_id")
        and location.get("strm_path")
    })
    plans_dir = get_data_dir() / "import_plans"
    if not plans_dir.exists():
        return set()

    occupied_paths: set[str] = set()
    try:
        from app.library.store import load_library_index

        index = load_library_index()
        for other in index.works if index is not None else []:
            if other.work_id == work.work_id:
                continue
            occupied_paths.update(
                os.path.normcase(_safe_resolve_str(Path(episode.strm_path)))
                for episode in other.episodes
                if episode.strm_path
            )
            occupied_paths.update(
                os.path.normcase(_safe_resolve_str(Path(str(location.get("strm_path") or ""))))
                for location in (other.source_locations or {}).values()
                if isinstance(location, dict) and location.get("strm_path")
            )
    except Exception:
        occupied_paths = set()

    result: set[str] = set()
    for path in plans_dir.glob("*.json"):
        if path.stem.endswith("_latest"):
            continue
        try:
            plan = load_import_plan(plan_id=path.stem)
        except (OSError, ValueError, TypeError):
            continue
        if plan is None or plan.status not in {"confirmed", "executed"}:
            continue
        seed_ids = {
            _library_work_id(item)
            for item in plan.items
            if _library_work_id(item) == work.work_id
            or (
                item.target_strm_path
                and (
                    item.source,
                    item.id,
                    os.path.normcase(_safe_resolve_str(Path(item.target_strm_path))),
                ) in known_items
            )
        }
        if not seed_ids:
            continue
        result.update(
            item.target_strm_path
            for item in plan.items
            if item.target_strm_path
            and item.resource_type == "video"
            and item.action == "generate_strm"
            and _library_work_id(item) in seed_ids
            and os.path.normcase(_safe_resolve_str(Path(item.target_strm_path))) not in occupied_paths
        )
    return result


def _execute_library_clear(preview: DeletePreview) -> DeleteResult:
    """整库清晰：全程持有维护屏障（阻止新任务入队/领取），随后门控、删除。"""
    from app.catalog import maintenance_guard

    with maintenance_guard.hold():
        return _execute_library_clear_guarded(preview)


def _execute_library_clear_guarded(preview: DeletePreview) -> DeleteResult:
    """Clear only the generated files captured by the approved preview.

    User-facing "delete library" means removing generated mirror files and
    rebuildable metadata, then transactionally removing the Source Catalog
    facts (source roots and derived data) for the same scope.  It never
    touches configured source roots on disk or the ``sources`` connection
    records (OpenList server config / routes / credentials stay intact).

    ``maintenance_guard.hold`` 已由外层 _execute_library_clear 持有：屏障生效
    期间新的 durable job 不能入队也不能被领取，从而消除初次门控到正式删除之间
    的任务竞争。
    """
    deleted: List[str] = []
    failed: List[DeleteFailure] = []
    skipped: List[DeleteFailure] = []
    empty_dirs_removed: List[str] = []
    deleted_preset_ids: List[str] = []
    deleted_library_work_count = 0
    deleted_tracking_binding_count = 0
    deleted_tracking_scan_run_count = 0
    cancelled_tracking_task_count = 0
    deleted_catalog_root_count = 0
    deleted_catalog_batch_count = 0
    deleted_catalog_unit_count = 0
    deleted_catalog_revision_count = 0
    deleted_catalog_job_count = 0

    source = _normalize_clear_source(preview.source)
    before_work_ids = _current_library_work_ids()
    mirror_root = get_mirror_root().resolve()
    target_root = _clear_target_root(mirror_root, source)
    source_roots = _configured_source_roots()
    unsafe_paths: List[DeleteFailure] = []
    for item in preview.files:
        path = Path(item.path)
        if not item.allowed or not _is_under_mirror_root(path, mirror_root) or _is_source_path(path, source_roots):
            unsafe_paths.append(DeleteFailure(path=item.path, reason="确认阶段文件路径安全检查失败"))
    for value in preview.empty_dirs:
        path = Path(value)
        if (
            path.resolve(strict=False) in {mirror_root, target_root.resolve(strict=False)}
            or not _is_under_mirror_root(path, mirror_root)
            or _is_source_path(path, source_roots)
        ):
            unsafe_paths.append(DeleteFailure(path=value, reason="确认阶段空目录路径安全检查失败"))
    if unsafe_paths:
        return DeleteResult(
            preview_id=preview.preview_id,
            status="failed",
            skipped=unsafe_paths,
            library_rescanned=False,
        )

    # 步骤 B（前置）：相关持久化任务门控必须在任何状态修改（追更、镜像、
    # 数据库）之前完成。queued 直接取消；running 置协作式取消后必须中止本次
    # 删除（409），不能在持久任务仍可能写库时删除来源根，也不能在 409 返回
    # 前误清追更/镜像/数据库状态。
    try:
        from app.catalog import lifecycle
        job_gate = lifecycle.prepare_catalog_cleanup(source)
    except Exception as exc:
        return DeleteResult(
            preview_id=preview.preview_id,
            status="failed",
            failed=[DeleteFailure(path="catalog_jobs", reason=str(exc))],
            library_rescanned=False,
        )
    if job_gate["running_job_ids"]:
        raise CatalogCleanupBusyError("相关后台任务正在停止，请稍后再次确认删除")

    try:
        tracking_result = _clear_tracking_state_for_clear(source)
        deleted_tracking_binding_count = tracking_result["binding_count"]
        deleted_tracking_scan_run_count = tracking_result["scan_run_count"]
        cancelled_tracking_task_count = tracking_result["cancelled_task_count"]
    except Exception as exc:
        return DeleteResult(
            preview_id=preview.preview_id,
            status="failed",
            failed=[DeleteFailure(path="tracking_state", reason=str(exc))],
            library_rescanned=False,
        )

    for item in preview.files:
        path = Path(item.path)
        if not path.is_file():
            skipped.append(DeleteFailure(path=item.path, reason="文件不存在"))
            continue
        try:
            path.unlink()
            deleted.append(item.path)
        except (OSError, PermissionError) as e:
            failed.append(DeleteFailure(path=item.path, reason=str(e)))

    for value in _sort_dirs_deep_first(preview.empty_dirs):
        directory = Path(value)
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
                empty_dirs_removed.append(value)
        except OSError:
            pass

    if source == "all":
        for path in _rebuildable_cache_paths():
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                    deleted.append(str(path))
                elif path.exists():
                    path.unlink()
                    deleted.append(str(path))
            except (OSError, PermissionError) as e:
                failed.append(DeleteFailure(path=str(path), reason=str(e)))
        failed.extend(_clear_import_plans("all"))
        failed.extend(_clear_error_logs("all"))
        failed.extend(_clear_scrape_database_state("all"))
    else:
        failed.extend(_clear_rebuildable_source_cache(source))
        failed.extend(_clear_import_plans(source))
        failed.extend(_clear_error_logs(source))
    failed.extend(_clear_openlist_manifests(source))

    cleared_preset_ids, preset_failures = _clear_media_presets(source)
    deleted_preset_ids.extend(cleared_preset_ids)
    failed.extend(preset_failures)

    library_rescanned = False
    try:
        if source == "all":
            from app.library.models import LibraryIndex
            from app.library.store import save_library_index
            save_library_index(LibraryIndex(works=[]))
        else:
            _remove_source_from_library_index(source)
        library_rescanned = True
        deleted_library_work_count = preview.library_work_count
    except Exception as e:
        failed.append(DeleteFailure(path="LibraryIndex", reason=f"写入空 LibraryIndex 失败: {e}"))

    if library_rescanned:
        removed_work_ids = before_work_ids - _current_library_work_ids()
        failed.extend(_clear_removed_work_state(
            removed_work_ids,
            clear_all=source == "all",
        ))

    # 步骤 D：镜像删除成功（无失败）后，事务化清理 Source Catalog 事实。
    # 镜像文件出现删除失败时暂不清除 catalog，保留数据库事实供用户重试。
    if not failed:
        try:
            catalog_result = lifecycle.delete_catalog_for_clear(source)
            deleted_catalog_root_count = catalog_result.deleted_root_count
            deleted_catalog_batch_count = catalog_result.deleted_batch_count
            deleted_catalog_unit_count = catalog_result.deleted_unit_count
            deleted_catalog_revision_count = catalog_result.deleted_revision_count
            deleted_catalog_job_count = catalog_result.deleted_job_count
        except CatalogCleanupBusyError:
            # 事务内复查发现新的 running 任务：整体回滚并传播为 409
            raise
        except Exception as exc:
            failed.append(DeleteFailure(path="catalog_cleanup", reason=str(exc)))
    elif deleted or preview.library_work_count or preview.catalog_root_count:
        # 部分/全部删除失败：保留 Source Catalog，避免删除语义不完整。
        failed.append(DeleteFailure(
            path="catalog_cleanup",
            reason="镜像文件删除未完全成功，已保留来源目录与导入记录供重试",
        ))

    made_progress = bool(
        deleted
        or deleted_preset_ids
        or deleted_tracking_binding_count
        or deleted_tracking_scan_run_count
        or deleted_catalog_root_count
        or deleted_catalog_unit_count
    )
    if failed and made_progress:
        status = "partial_failed"
    elif failed:
        status = "failed"
    else:
        status = "succeeded"

    return DeleteResult(
        preview_id=preview.preview_id,
        status=status,
        deleted=deleted,
        failed=failed,
        skipped=skipped,
        empty_dirs_removed=empty_dirs_removed,
        library_rescanned=library_rescanned,
        deleted_library_work_count=deleted_library_work_count,
        deleted_preset_ids=deleted_preset_ids,
        deleted_tracking_binding_count=deleted_tracking_binding_count,
        deleted_tracking_scan_run_count=deleted_tracking_scan_run_count,
        cancelled_tracking_task_count=cancelled_tracking_task_count,
        deleted_catalog_root_count=deleted_catalog_root_count,
        deleted_catalog_batch_count=deleted_catalog_batch_count,
        deleted_catalog_unit_count=deleted_catalog_unit_count,
        deleted_catalog_revision_count=deleted_catalog_revision_count,
        deleted_catalog_job_count=deleted_catalog_job_count,
    )


def _current_library_work_ids() -> set[str]:
    from app.library.store import load_library_index

    index = load_library_index()
    if index is None:
        return set()
    return {work.work_id for work in index.works if work.work_id}


def _clear_removed_work_state(work_ids: set[str], *, clear_all: bool = False) -> List[DeleteFailure]:
    """清理已从 LibraryIndex 消失作品的本地观看与收藏状态。"""
    from dataclasses import asdict

    from app.core.atomic_json import write_json_atomic

    failures: List[DeleteFailure] = []
    try:
        from app.playback.history import _get_history_path, load_history

        remaining = [] if clear_all else [item for item in load_history() if item.work_id not in work_ids]
        write_json_atomic(_get_history_path(), [asdict(item) for item in remaining])
    except Exception as exc:
        failures.append(DeleteFailure(path="playback/history.json", reason=str(exc)))

    try:
        from app.playback.progress import _write_progress, load_progress

        remaining = [] if clear_all else [item for item in load_progress() if item.work_id not in work_ids]
        _write_progress(remaining)
    except Exception as exc:
        failures.append(DeleteFailure(path="playback/progress.json", reason=str(exc)))

    try:
        from app.library.watch_status import load_watch_statuses, save_watch_statuses

        statuses = load_watch_statuses()
        save_watch_statuses({
            key: value
            for key, value in statuses.items()
            if not clear_all and key not in work_ids
        })
    except Exception as exc:
        failures.append(DeleteFailure(path="watch_status.json", reason=str(exc)))

    try:
        from app.integrations.bangumi import load_state, save_state

        state = load_state()
        state.matches = [] if clear_all else [item for item in state.matches if item.work_id not in work_ids]
        state.episode_sync = [] if clear_all else [
            item for item in state.episode_sync if item.work_id not in work_ids
        ]
        save_state(state)
    except Exception as exc:
        failures.append(DeleteFailure(path="bangumi_state.json", reason=str(exc)))

    conn = None
    try:
        from app.db.database import close_connection, get_connection, init_db

        init_db()
        conn = get_connection()
        if clear_all:
            conn.execute("DELETE FROM playback_history")
            conn.execute("DELETE FROM work_overrides")
        elif work_ids:
            values = tuple(work_ids)
            marks = ",".join("?" for _ in values)
            conn.execute(f"DELETE FROM playback_history WHERE work_id IN ({marks})", values)
            conn.execute(f"DELETE FROM work_overrides WHERE work_id IN ({marks})", values)
        conn.commit()
        close_connection()
    except Exception as exc:
        try:
            if conn is not None:
                conn.rollback()
            close_connection()
        except Exception:
            pass
        failures.append(DeleteFailure(path="state.db", reason=str(exc)))
    return failures


def _count_media_presets_for_clear(source: str) -> int:
    """返回正式清理时会同步移除的目录树导入档案数量。"""
    try:
        from app.media_presets.store import list_presets_for_clear
        return len(list_presets_for_clear(source))
    except Exception:
        # 预览数量读取失败不应绕过原有镜像路径安全检查；执行阶段会记录真实错误。
        return 0


def _count_tracking_state_for_clear(source: str) -> dict[str, int]:
    """返回正式清空时会同步移除的追更控制记录数量。"""
    from app.tracking.store import count_tracking_state_for_clear

    return count_tracking_state_for_clear(source)


def _count_library_works_for_clear(source: str) -> int:
    """统计当前清理范围内会从正式媒体库索引移除的作品。"""
    from app.library.store import load_library_index

    index = load_library_index()
    if index is None:
        return 0
    scoped = [
        work
        for work in index.works
        if source == "all"
        or source in set((work.sources or []) + [work.source])
        or any((episode.source or work.source) == source for episode in work.episodes)
    ]
    return len(scoped)


def _clear_tracking_state_for_clear(source: str) -> dict[str, int]:
    """停止追更任务并清除控制记录，不触碰任何真实媒体文件。"""
    from app.tasks.registry import get_task_manager
    from app.tracking.store import delete_tracking_state_for_clear

    cancelled = get_task_manager().cancel_running_tracking_tasks(source)
    deleted = delete_tracking_state_for_clear(source)
    return {
        **deleted,
        "cancelled_task_count": cancelled,
    }


def _clear_media_presets(
    source: str,
) -> tuple[List[str], List[DeleteFailure]]:
    """清除同来源的目录树导入档案，不触碰用户原始目录树文件。"""
    try:
        from app.media_presets.store import delete_presets_for_clear
        deleted_ids, raw_failures = delete_presets_for_clear(source)
        return deleted_ids, [DeleteFailure(path=path, reason=reason) for path, reason in raw_failures]
    except Exception as exc:
        return [], [DeleteFailure(path="media_presets/index.json", reason=str(exc))]


def _clear_error_logs(source: str) -> List[DeleteFailure]:
    try:
        from app.core.error_log import purge_errors
        purge_errors(source=None if source == "all" else source)
    except Exception as e:
        return [DeleteFailure(path="error_log", reason=str(e))]
    return []


def _clear_openlist_manifests(source: str) -> List[DeleteFailure]:
    """清理 OpenList 生成的 KumiPlayer 自有扫描清单。

    只删除 ``data/openlist_manifests`` 下的受控 JSON 清单，
    绝不触碰 OpenList 本地挂载盘中的真实媒体文件。
    """
    if source not in {"all", "openlist"}:
        return []
    failures: List[DeleteFailure] = []
    manifest_dir = get_data_dir() / "openlist_manifests"
    if not manifest_dir.exists():
        return failures
    try:
        for path in manifest_dir.glob("*.json"):
            path.unlink()
        try:
            if manifest_dir.is_dir() and not any(manifest_dir.iterdir()):
                manifest_dir.rmdir()
        except OSError:
            pass
    except (OSError, PermissionError) as e:
        failures.append(DeleteFailure(path=str(manifest_dir), reason=str(e)))
    return failures


def _rebuildable_cache_paths() -> List[Path]:
    data_dir = get_data_dir()
    cache_dir = get_cache_dir()
    return [
        cache_dir / "library_index.json",
        data_dir / "library" / "deleted_works.json",
        data_dir / "scrape" / "scrape_map.json",
        data_dir / "scrape" / "review_queue.json",
        data_dir / "scrape" / "failed_cases.json",
    ]


def _clear_rebuildable_source_cache(source: str) -> List[DeleteFailure]:
    failures: List[DeleteFailure] = []
    try:
        from app.scrape.store import load_scrape_map, save_scrape_map
        scrape_map = load_scrape_map()
        scrape_map.items = [item for item in scrape_map.items if item.source != source]
        save_scrape_map(scrape_map)
    except Exception as e:
        failures.append(DeleteFailure(path="scrape_map.json", reason=str(e)))

    try:
        from app.scrape.review_queue import load_review_queue, save_review_queue
        queue = load_review_queue()
        queue.items = [item for item in queue.items if item.source != source]
        save_review_queue(queue)
    except Exception as e:
        failures.append(DeleteFailure(path="review_queue.json", reason=str(e)))

    try:
        from app.scrape.store import load_failed_cases, _get_scrape_dir
        import json
        cases = [
            case for case in load_failed_cases()
            if case.get("source") != source
        ]
        path = _get_scrape_dir() / "failed_cases.json"
        path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        failures.append(DeleteFailure(path="failed_cases.json", reason=str(e)))

    failures.extend(_clear_scrape_database_state(source))
    return failures


def _clear_scrape_database_state(source: str) -> List[DeleteFailure]:
    """清理 V3 SQLite 刮削事实，与 JSON 双写侧对称。

    按来源清库后不留待处理 review_queue 行，也不留 bindings 被删后的
    孤儿 scrape_reviews / scrape_failures；candidate_cache 按 queue 的
    target 归属同步清除（须在 queue 删除前收集）。source 为 "all" 时整表清空。
    """
    try:
        from app.db.database import close_connection, get_connection, init_db

        init_db()
        conn = get_connection()
        try:
            scoped = source not in {"", "all"}
            if scoped:
                conn.execute(
                    "DELETE FROM scrape_candidate_cache WHERE scrape_target_id IN "
                    "(SELECT scrape_target_id FROM scrape_review_queue WHERE source = ?)",
                    (source,),
                )
                conn.execute("DELETE FROM scrape_review_queue WHERE source = ?", (source,))
                conn.execute(
                    "DELETE FROM scrape_failures WHERE binding_id IN "
                    "(SELECT binding_id FROM scrape_bindings WHERE source = ?)",
                    (source,),
                )
                conn.execute(
                    "DELETE FROM scrape_reviews WHERE binding_id IN "
                    "(SELECT binding_id FROM scrape_bindings WHERE source = ?)",
                    (source,),
                )
                conn.execute("DELETE FROM scrape_bindings WHERE source = ?", (source,))
            else:
                conn.execute("DELETE FROM scrape_candidate_cache")
                conn.execute("DELETE FROM scrape_review_queue")
                conn.execute("DELETE FROM scrape_failures")
                conn.execute("DELETE FROM scrape_reviews")
                conn.execute("DELETE FROM scrape_bindings")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            close_connection()
    except Exception as exc:
        return [DeleteFailure(path="scrape.db", reason=str(exc))]
    return []


def _clear_import_plans(source: str) -> List[DeleteFailure]:
    failures: List[DeleteFailure] = []
    plans_dir = get_data_dir() / "import_plans"
    if not plans_dir.exists():
        return failures

    for path in plans_dir.glob("*.json"):
        try:
            should_delete = source == "all"
            if not should_delete:
                try:
                    import json
                    data = json.loads(path.read_text(encoding="utf-8"))
                    should_delete = data.get("source") == source
                except Exception:
                    should_delete = path.name == f"{source}_latest.json"
            if should_delete:
                path.unlink()
        except (OSError, PermissionError) as e:
            failures.append(DeleteFailure(path=str(path), reason=str(e)))
    return failures


def _remove_source_from_library_index(source: str) -> None:
    from app.library.service import _without_source_contribution
    from app.library.store import load_library_index, save_library_index

    index = load_library_index()
    if index is None:
        return
    index.works = [
        retained
        for work in index.works
        if (retained := _without_source_contribution(work, source)) is not None
    ]
    index.source_summary.pop(source, None)
    save_library_index(index)
