"""V4 media import API: SourceEvidence → preview → confirmed revision → jobs。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import load_config, resolve_openlist_credentials
from app.core.paths import get_data_dir
from app.integrations.openlist.client import normalize_remote_path
from app.integrations.openlist.models import OpenListError
from app.integrations.openlist.providers import derive_local_path, provider_for_remote
from app.media_v4.domain.models import SourceEvidence
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.jobs.scrape import V4ScrapeService
from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.playback.store import V4PlaybackStore
from app.media_v4.projection.library import V4LibraryProjection
from app.media_v4.revisions.service import RevisionBlockedError, V4RevisionService
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
from app.media_v4.sources.incremental import (
    activate_scan_state,
    build_full_scan_state,
    build_tree_baseline_state,
    load_active_state,
    scan_openlist_incremental,
    stage_scan_state,
)
from app.media_v4.sources.scan_validation import (
    delete_tree_scan_validation,
    load_tree_scan_validation,
    upsert_tree_scan_validation,
)
from app.media_v4.sources.scanner import (
    DirectoryTreeReadError,
    build_directory_tree_evidence,
    openlist_root_id,
    read_directory_tree_text,
    scan_local_directory,
    scan_openlist_directory,
)
from app.media_v4.sources.tree_root import (
    TreePlaybackRootResolver,
    TreeRootResolution,
    tree_scan_root_id,
)
from app.media_v4.tracking.store import V4TrackingStore

router = APIRouter(prefix="/api/v4", tags=["media-v4"])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()

# Tests and the desktop bootstrap may inject an isolated handle.  The default
# path is resolved lazily so importing the router never creates data files.
_database: V4Database | None = None


class EntryRequest(BaseModel):
    """One observed source entry; it contains no parsed work identity."""

    relative_path: str = Field(min_length=1)
    provider: str = "local"
    ingest_method: str = "local_scan"
    source_key: str = ""
    source_locator: str = ""
    playback_locator: str = ""
    raw_file_id: str = ""
    source_route_id: str = ""
    size: int | None = None
    mtime: float | None = None
    fingerprint: str = ""
    entry_kind: str = "video"
    tmdb_hint_id: str = ""
    tmdb_hint_type: str = ""
    import_family: str = "anime"
    target_filename: str = ""
    observed_at: str = ""


class PreviewRequest(BaseModel):
    revision_id: str = Field(min_length=1)
    root_id: str = Field(min_length=1)
    scan_id: str = Field(min_length=1)
    entries: list[EntryRequest] = Field(default_factory=list)
    allow_empty: bool = False
    source_display_name: str = ""
    source_locator: str = ""
    playback_locator: str = ""
    source_route_id: str = ""
    source_mode: str = ""


class PlaybackProgressRequest(BaseModel):
    work_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    position: float = Field(ge=0)
    duration: float = Field(ge=0)
    completed: bool = False


class TrackingStateRequest(BaseModel):
    work_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_id: str = ""
    last_watched_episode: int | None = Field(default=None, ge=0)
    metadata: dict = Field(default_factory=dict)


class SourceScanRequest(BaseModel):
    source: Literal["local", "tree", "openlist", "hybrid"] = "local"
    root_path: str = ""
    tree_file: str = ""
    provider: str = "local"
    source_root: str = ""
    scan_mode: Literal["auto", "full", "incremental"] = "auto"
    revision_id: str = ""
    source_display_name: str = ""


class SourceCardRenameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)




class WorkTitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class WorkDeleteConfirmRequest(BaseModel):
    preview_id: str = Field(min_length=1)
    digest: str = Field(min_length=32)


class ArtworkUploadRequest(BaseModel):
    kind: Literal["poster", "fanart", "clearlogo"]
    data_base64: str = Field(min_length=1)
class MaintenancePreviewRequest(BaseModel):
    scope: Literal["local", "pan115", "baidu", "quark", "all"] = "all"
    root_ids: list[str] | None = None


class MaintenanceResumeRequest(BaseModel):
    preview_id: str = Field(min_length=1)


class MaintenanceConfirmRequest(BaseModel):
    preview_id: str = Field(min_length=1)
    scope: Literal["local", "pan115", "baidu", "quark", "all"] = "all"
    digest: str = Field(min_length=32)


class IdentityRepairApplyRequest(BaseModel):
    preview_id: str = Field(min_length=1)
    digest: str = Field(min_length=32)


class IdentityRepairResumeRequest(BaseModel):
    operation_id: str = Field(min_length=1)


def _configured_mirror_root():
    from app.core.paths import get_mirror_root

    try:
        return get_mirror_root()
    except Exception:
        return None


class OverrideRequest(BaseModel):
    changes: dict


def get_database() -> V4Database:
    global _database
    if _database is None:
        _database = V4Database(get_data_dir() / "kumiplayer.db")
        _database.initialize()
    return _database


def _graph_to_dict(graph) -> dict:
    return {
        "works": [asdict(work) for work in graph.works],
        "episodes": [asdict(episode) for episode in graph.episodes],
        "work_assets": [asdict(asset) for asset in graph.work_assets],
        "relations": [asdict(relation) for relation in graph.relations],
        "issues": [asdict(issue) for issue in graph.issues],
    }


def _make_entries(request: PreviewRequest):
    return [
        to_source_evidence(
            SourceEntry(
                root_id=request.root_id,
                scan_id=request.scan_id,
                provider=entry.provider,
                ingest_method=entry.ingest_method,
                relative_path=entry.relative_path,
                source_key=entry.source_key,
                source_locator=entry.source_locator,
                playback_locator=entry.playback_locator,
                raw_file_id=entry.raw_file_id,
                source_route_id=entry.source_route_id,
                size=entry.size,
                mtime=entry.mtime,
                fingerprint=entry.fingerprint,
                entry_kind=entry.entry_kind,
                tmdb_hint_id=entry.tmdb_hint_id,
                tmdb_hint_type=entry.tmdb_hint_type,
                import_family=entry.import_family,
                target_filename=entry.target_filename,
                observed_at=entry.observed_at,
            )
        )
        for entry in request.entries
    ]


def _completed_scan_evidence(database: V4Database, *, root_id: str, scan_id: str) -> list[SourceEvidence] | None:
    """读取耐久扫描的权威证据；未完成扫描仍不能进入 preview。"""

    with database.connect() as conn:
        row = conn.execute(
            "SELECT root_id, status FROM source_scans WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
    if row is None:
        return None
    if str(row["root_id"]) != root_id:
        raise HTTPException(status_code=409, detail="扫描证据与当前来源根不一致，请重新扫描")
    if str(row["status"]) != "completed":
        return None
    return V4Repository(database).list_scan_evidence(scan_id)


def _confirmed_source_evidence(root_id: str):
    """读取来源根当前 confirmed revision 的完整证据快照。"""

    database = get_database()
    return V4Repository(database).list_confirmed_source_evidence(root_id)


def _source_root_mode(root_id: str) -> str:
    """读取来源根级模式；没有记录时返回空串，由调用方决定默认值。"""

    with get_database().connect() as conn:
        row = conn.execute(
            "SELECT source_mode FROM source_roots WHERE root_id = ?",
            (root_id,),
        ).fetchone()
    return str(row["source_mode"] or "") if row else ""


def _configured_cloud_roots(config) -> list[str]:
    roots = [
        str(getattr(config, "pan115_root", "") or ""),
        str(getattr(config, "baidu_root", "") or ""),
        str(getattr(config, "openlist_mount_root", "") or ""),
    ]
    for route in getattr(config, "openlist_routes", ()) or ():
        local_path = route.get("local_path", "") if isinstance(route, dict) else getattr(route, "local_path", "")
        roots.append(str(local_path or ""))
    return [root for root in roots if root.strip()]


def _configured_tree_roots(config, provider: str) -> list[str]:
    roots: list[str] = []
    if provider == "pan115":
        roots.append(str(getattr(config, "pan115_root", "") or ""))
    elif provider == "baidu":
        roots.append(str(getattr(config, "baidu_root", "") or ""))
    mount_root = str(getattr(config, "openlist_mount_root", "") or "").strip()
    remote_root = normalize_remote_path(str(getattr(config, "openlist_remote_root", "") or "/"))
    if mount_root:
        for route in getattr(config, "openlist_routes", ()) or ():
            route_provider = route.get("provider_id", "") if isinstance(route, dict) else getattr(route, "provider_id", "")
            enabled = route.get("enabled", True) if isinstance(route, dict) else getattr(route, "enabled", True)
            prefix = route.get("remote_prefix", "") if isinstance(route, dict) else getattr(route, "remote_prefix", "")
            if enabled and route_provider == provider and prefix:
                roots.append(derive_local_path(mount_root, remote_root, str(prefix)))
    unique: dict[str, str] = {}
    for root in roots:
        value = root.strip().rstrip("\\/")
        if value:
            unique.setdefault(value.replace("/", "\\").casefold(), value)
    return list(unique.values())


def _container_name(path: str) -> str:
    """来源根目录的最后一个有效段名；通用容器段返回空（不当作作品身份）。"""

    from app.media_v4.generic_container import is_generic_container_name

    value = (path or "").strip().rstrip("/\\")
    if not value:
        return ""
    name = value.replace("\\", "/").split("/")[-1]
    return "" if is_generic_container_name(name) else name


def _ensure_root_container(
    database,
    *,
    root_id: str,
    provider: str,
    ingest_method: str,
    locator: str = "",
    source_locator: str = "",
    playback_locator: str = "",
    route_id: str = "",
    root_container: str = "",
    source_mode: str = "",
    last_scan_mode: str = "",
    conn=None,
) -> None:
    """为非目录树来源写入来源根上下文，preview 据此传入解析器。

    source_mode 是来源根级权威模式，非空时覆盖、空时保留既有值；
    last_scan_mode 只记录最近一次扫描方式。
    """

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    effective_source_locator = source_locator or locator
    effective_playback_locator = playback_locator or locator

    def write(connection) -> None:
        connection.execute(
            """
            INSERT INTO source_roots(
                root_id, provider, ingest_method, source_locator, playback_locator,
                route_id, display_name, root_container, source_mode, last_scan_mode,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)
            ON CONFLICT(root_id) DO UPDATE SET
                provider = excluded.provider,
                ingest_method = excluded.ingest_method,
                source_locator = CASE
                    WHEN excluded.source_locator != '' THEN excluded.source_locator
                    ELSE source_roots.source_locator
                END,
                playback_locator = CASE
                    WHEN excluded.playback_locator != '' THEN excluded.playback_locator
                    ELSE source_roots.playback_locator
                END,
                route_id = CASE
                    WHEN excluded.route_id != '' THEN excluded.route_id
                    ELSE source_roots.route_id
                END,
                root_container = CASE
                    WHEN excluded.root_container != '' THEN excluded.root_container
                    ELSE source_roots.root_container
                END,
                source_mode = CASE
                    WHEN excluded.source_mode != '' THEN excluded.source_mode
                    ELSE source_roots.source_mode
                END,
                last_scan_mode = excluded.last_scan_mode,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (
                root_id,
                provider,
                ingest_method,
                effective_source_locator,
                effective_playback_locator,
                route_id,
                root_container,
                source_mode,
                last_scan_mode,
                now,
                now,
            ),
        )


    if conn is not None:
        write(conn)
    else:
        with database.connect() as connection:
            write(connection)


def _register_durable_source_scan(
    database,
    *,
    root: dict,
    scan: dict,
) -> str:
    """在同一事务内发布来源根和 queued 扫描，提交后才允许唤醒 runner。"""

    from app.media_v4.sources.source_scan_runner import register_source_scan

    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_root_container(database, conn=conn, **root)
            result = register_source_scan(database, conn=conn, **scan)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def _tree_validation_dict(resolution: TreeRootResolution) -> dict:
    return {
        "ok": resolution.ok,
        "root": resolution.root,
        "hits": resolution.hits,
        "total": resolution.total,
        "reason": resolution.reason,
    }


def _persist_tree_scan(
    database,
    *,
    root_id: str,
    provider: str,
    scan_id: str,
    route_id: str,
    effective_root: str,
    source_locator: str | None = None,
    playback_locator: str | None = None,
    root_container: str = "",
    evidence: list,
    resolution: TreeRootResolution,
    source_mode: str = "",
    last_scan_mode: str = "",
    durable: bool = False,
    save_evidence: bool = True,
) -> None:
    """把目录树扫描的证据与验证事实写入事务约束的 SQLite。

    source_roots / source_scans / source_evidence / tree_scan_validation 一起
    成为 preview 与 confirm 的后端权威，前端回传的逐条 locator 不再被采信。
    source_mode 显式区分纯目录树快照（tree_snapshot）与混合 TXT 基线
    （tree_openlist），不随证据顺序变化。
    """

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    effective_source_locator = effective_root if source_locator is None else source_locator
    effective_playback_locator = effective_root if playback_locator is None else playback_locator
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_roots(
                root_id, provider, ingest_method, source_locator, playback_locator,
                route_id, display_name, root_container, source_mode, last_scan_mode,
                created_at, updated_at
            ) VALUES (?, ?, 'directory_tree', ?, ?, ?, '', ?, ?, ?, ?, ?)
            ON CONFLICT(root_id) DO UPDATE SET
                provider = excluded.provider,
                ingest_method = excluded.ingest_method,
                source_locator = CASE
                    WHEN excluded.source_locator != '' THEN excluded.source_locator
                    ELSE source_roots.source_locator
                END,
                playback_locator = CASE
                    WHEN excluded.playback_locator != '' THEN excluded.playback_locator
                    ELSE source_roots.playback_locator
                END,
                route_id = CASE
                    WHEN excluded.route_id != '' THEN excluded.route_id
                    ELSE source_roots.route_id
                END,
                root_container = CASE
                    WHEN excluded.root_container != '' THEN excluded.root_container
                    ELSE source_roots.root_container
                END,
                source_mode = CASE
                    WHEN excluded.source_mode != '' THEN excluded.source_mode
                    ELSE source_roots.source_mode
                END,
                last_scan_mode = excluded.last_scan_mode,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (
                root_id,
                provider,
                effective_source_locator,
                effective_playback_locator,
                route_id,
                root_container,
                source_mode,
                last_scan_mode,
                now,
                now,
            ),
        )
        existing = conn.execute(
            "SELECT generation FROM source_scans WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        if existing is None:
            generation = conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM source_scans WHERE root_id = ?",
                (root_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at, finished_at) "
                "VALUES (?, ?, ?, 'validated', ?, ?)",
                (scan_id, root_id, generation, now, now),
            )
        elif not durable:
            conn.execute(
                "UPDATE source_scans SET status = 'validated', finished_at = ? WHERE scan_id = ?",
                (now, scan_id),
            )
    if save_evidence:
        V4Repository(database).save_scan_evidence_bulk(evidence)
    upsert_tree_scan_validation(
        database,
        scan_id=scan_id,
        root_id=root_id,
        effective_root=effective_root,
        ok=resolution.ok,
        hits=0,
        total=0,
        reason=resolution.reason,
        # 新合同：hits/total=0、samples=[]，说明只是词法映射，不做样本可达性断言。
        samples=[],
        candidates=list(resolution.candidates),
    )


def _tree_sample_paths(evidence: list) -> list[str]:
    """从头/中/尾取有界样本相对路径，供 confirm 时重新校验可达性。"""

    paths = [str(item.relative_path) for item in evidence]
    if not paths:
        return []
    if len(paths) <= 3:
        return paths
    indexes = sorted({0, len(paths) - 1, len(paths) // 2})
    return [paths[index] for index in indexes]


def _directory_tree_error_message(exc: DirectoryTreeReadError) -> str:
    if exc.kind == "unreadable":
        return "目录树文件无法打开，请确认文件未被占用或损坏后重新选择"
    if exc.kind == "empty":
        return "目录树文件为空，请重新导出 TXT"
    if exc.kind == "too_large":
        return "目录树文件过大，请拆分后重新导出"
    return str(exc)


@router.post("/sources/scan")
def scan_source(request: SourceScanRequest):
    if request.source == "local" and not request.root_path.strip():
        raise HTTPException(status_code=400, detail="本地媒体目录不能为空")
    if request.source in {"tree", "hybrid"} and not request.tree_file:
        detail = "目录树 + OpenList 增量需要先选择 TXT 基线文件" if request.source == "hybrid" else "目录树 TXT 文件不能为空"
        raise HTTPException(status_code=400, detail=detail)
    try:
        effective_scan_mode: str = request.source
        root_source_mode: str = ""
        scan_stats: dict[str, int] = {}
        resolution: TreeRootResolution = TreeRootResolution("", True, "", 0, 0, ())
        effective_playback_root = ""
        content_provider = (
            request.provider
            if request.provider in {"local", "pan115", "baidu", "quark", "other"}
            else "unknown"
        )
        if request.source == "hybrid":
            effective_scan_mode = "tree_baseline"
            root_source_mode = "tree_openlist"
            from app.api.openlist_v4 import _configured_routes, _remote_root

            config = load_config()
            username, _password, credential_state = resolve_openlist_credentials()
            if credential_state == "unavailable":
                raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
            if not config.openlist_server_url or not username:
                raise HTTPException(status_code=400, detail="请先在设置页完成 OpenList 连接配置")
            remote_root = normalize_remote_path(request.root_path.strip() or _remote_root(config))
            routes = _configured_routes(config)
            route_id, routed_provider = provider_for_remote(routes, remote_root)
            if not route_id:
                raise HTTPException(status_code=409, detail="当前 OpenList 目录未匹配已保存的内容来源路由")
            content_provider = routed_provider
            root_id = openlist_root_id(config.openlist_server_url, username, remote_root)
            if not config.openlist_mount_root:
                raise HTTPException(status_code=409, detail="当前内容来源尚未配置本地挂载路径，请先在设置页完成来源映射")
            local_root = derive_local_path(config.openlist_mount_root, _remote_root(config), remote_root)
            # 同一份已解码文本同时交给根解析与 evidence 构建，只读取一次。
            tree_text = read_directory_tree_text(request.tree_file)
            resolution = TreePlaybackRootResolver(
                request.tree_file,
                configured_roots=[local_root],
            ).resolve(tree_text)
            scan_id, evidence = build_directory_tree_evidence(
                tree_text,
                root_id=root_id,
                provider=content_provider,
                source_root=resolution.root,
                source_route_id=route_id,
            )
            _persist_tree_scan(
                get_database(),
                root_id=root_id,
                provider=content_provider,
                scan_id=scan_id,
                route_id=route_id,
                effective_root=resolution.root,
                source_locator=remote_root,
                playback_locator=resolution.root or local_root,
                root_container=_container_name(resolution.root or local_root),
                evidence=evidence,
                resolution=resolution,
                source_mode=root_source_mode,
                last_scan_mode=effective_scan_mode,
            )
            effective_playback_root = resolution.root
            stage_scan_state(
                scan_id,
                build_tree_baseline_state(root_id, remote_root, evidence),
            )
        elif request.source == "tree":
            effective_scan_mode = "tree_snapshot"
            root_source_mode = "tree_snapshot"
            config = load_config()
            configured_roots = _configured_tree_roots(config, content_provider)
            if not configured_roots:
                raise HTTPException(status_code=409, detail="当前内容来源尚未配置本地挂载路径，请先在设置页完成来源映射")
            tree_text = read_directory_tree_text(request.tree_file)
            resolution = TreePlaybackRootResolver(
                request.tree_file,
                configured_roots=configured_roots,
            ).resolve(tree_text)
            effective_root = resolution.root
            root_id = tree_scan_root_id(
                provider=content_provider,
                configured_roots=configured_roots,
                resolution=resolution,
                tree_file=request.tree_file,
            )
            scan_id, evidence = build_directory_tree_evidence(
                tree_text,
                root_id=root_id,
                provider=content_provider,
                source_root=effective_root,
            )
            _persist_tree_scan(
                get_database(),
                root_id=root_id,
                provider=content_provider,
                scan_id=scan_id,
                route_id="",
                effective_root=effective_root,
                root_container=_container_name(effective_root),
                evidence=evidence,
                resolution=resolution,
                source_mode=root_source_mode,
                last_scan_mode=effective_scan_mode,
            )
            effective_playback_root = resolution.root
        elif request.source == "openlist":
            from app.api.openlist_v4 import _client, _configured_routes, _remote_root

            config = load_config()
            username, _password, credential_state = resolve_openlist_credentials()
            if credential_state == "unavailable":
                raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
            if not config.openlist_mount_root:
                raise HTTPException(status_code=409, detail="OpenList 导入需要先配置本地挂载根，才能建立可播放 Asset")
            remote_root = normalize_remote_path(request.root_path.strip() or _remote_root(config))
            routes = _configured_routes(config)
            route_id, routed_provider = provider_for_remote(routes, remote_root)
            if not route_id:
                raise HTTPException(status_code=409, detail="当前 OpenList 目录未匹配已保存的内容来源路由")
            content_provider = routed_provider
            root_id = openlist_root_id(config.openlist_server_url, username, remote_root)
            openlist_playback_root = derive_local_path(
                config.openlist_mount_root,
                _remote_root(config),
                remote_root,
            )
            baseline = _confirmed_source_evidence(root_id)
            state = load_active_state(root_id)
            existing_mode = _source_root_mode(root_id)
            if request.scan_mode == "incremental" and not baseline:
                raise HTTPException(
                    status_code=409,
                    detail="此 OpenList 目录尚无已确认基线，请先完成并确认首次完整扫描，或使用 TXT 建立大库基线",
                )
            if request.scan_mode == "incremental" or (request.scan_mode == "auto" and baseline):
                if not state or state.get("remote_root") != remote_root:
                    state = build_tree_baseline_state(root_id, remote_root, baseline)
                effective_scan_mode = "incremental"
                # 增量属于 OpenList 共同能力：已有来源模式保持不变（TXT 混合根仍是
                # tree_openlist），未知根才默认回落到 openlist_full。
                root_source_mode = existing_mode or "openlist_full"
                scan_id, evidence, next_state, scan_stats = scan_openlist_incremental(
                    _client(config),
                    baseline=baseline,
                    state=state,
                    mapping_root=_remote_root(config),
                    mount_root=config.openlist_mount_root,
                    default_provider=content_provider,
                    routes=routes,
                )
                stage_scan_state(scan_id, next_state)
            else:
                effective_scan_mode = "full"
                root_source_mode = "openlist_full"
                directory_observations: dict[str, float | None] = {}
                scan_id, evidence = scan_openlist_directory(
                    _client(config),
                    remote_root=remote_root,
                    mapping_root=_remote_root(config),
                    mount_root=config.openlist_mount_root,
                    root_id=root_id,
                    default_provider=content_provider,
                    routes=routes,
                    directory_observations=directory_observations,
                )
                stage_scan_state(
                    scan_id,
                    build_full_scan_state(root_id, remote_root, directory_observations),
                )
            _ensure_root_container(
                get_database(),
                root_id=root_id,
                provider=content_provider,
                ingest_method="openlist_scan",
                source_locator=remote_root,
                playback_locator=openlist_playback_root,
                route_id=route_id,
                root_container=_container_name(remote_root),
                source_mode=root_source_mode,
                last_scan_mode=effective_scan_mode,
            )
            effective_playback_root = openlist_playback_root
        else:
            config = load_config()
            root_id, scan_id, evidence = scan_local_directory(
                request.root_path,
                excluded_roots=_configured_cloud_roots(config),
            )
            _ensure_root_container(
                get_database(),
                root_id=root_id,
                provider="local",
                ingest_method="local_scan",
                locator=str(Path(request.root_path).expanduser()),
                route_id="",
                root_container=_container_name(request.root_path),
                source_mode="local",
                last_scan_mode="local",
            )
            effective_playback_root = str(Path(request.root_path).expanduser())
    except DirectoryTreeReadError as exc:
        raise HTTPException(status_code=400, detail=_directory_tree_error_message(exc)) from exc
    except (FileNotFoundError, NotADirectoryError, OSError, OpenListError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"来源扫描失败: {exc}") from exc
    validation = {
        "ok": resolution.ok,
        "root": resolution.root,
        "hits": resolution.hits,
        "total": resolution.total,
        "reason": resolution.reason,
    }
    return {
        "root_id": root_id,
        "scan_id": scan_id,
        "entries": [asdict(item) for item in evidence],
        "scan_mode": effective_scan_mode,
        "source_mode": root_source_mode,
        "last_scan_mode": effective_scan_mode,
        "scan_stats": scan_stats,
        "effective_playback_root": effective_playback_root,
        "path_validation": validation,
        "root_container": _container_name(resolution.root or request.root_path),
    }


@router.post("/imports/preview")
def preview(request: PreviewRequest):
    database = get_database()
    # 新导入链会在 durable SourceScan 后台阶段完成识别与候选解析。这里优先
    # 读取已落库草稿，绝不能因为用户打开预览而再次发起 TMDB 请求。
    with database.connect() as conn:
        existing_revision = conn.execute(
            "SELECT root_id, scan_id, status FROM import_revisions WHERE revision_id = ?",
            (request.revision_id,),
        ).fetchone()
    if existing_revision is not None:
        if (
            str(existing_revision["root_id"]) != request.root_id
            or str(existing_revision["scan_id"]) != request.scan_id
        ):
            raise HTTPException(status_code=409, detail="这次扫描结果已过期，请重新扫描。")
        if str(existing_revision["status"]) != "draft":
            raise HTTPException(status_code=409, detail="当前识别结果已经不是可编辑草稿")
        graph = V4RevisionService(database).load_draft_graph(request.revision_id)
        return {
            "revision_id": request.revision_id,
            "status": "draft",
            **_graph_to_dict(graph),
        }
    durable_evidence = _completed_scan_evidence(
        database,
        root_id=request.root_id,
        scan_id=request.scan_id,
    )
    validation = load_tree_scan_validation(database, request.scan_id)
    # 取消合同 B：cancelled/failed scan 的证据只能作审计，永远不能进入
    # preview / draft / 确认链。目录树验证行不能绕过这一状态门。
    with database.connect() as conn:
        scan_row = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = ?",
            (request.scan_id,),
        ).fetchone()
    if scan_row is not None and str(scan_row["status"]) not in {"completed", "validated"}:
        raise HTTPException(
            status_code=409,
            detail=f"扫描未完成（当前状态 {scan_row['status']}），请重新扫描后再生成识别预览",
        )
    # 完整 durable 扫描和目录树验证记录都代表后端已经持久化了证据；
    # 后者不能因为没有 completed 状态而退回前端提交路径。
    evidence_is_persisted = durable_evidence is not None or validation is not None
    if durable_evidence is not None:
        # 所有耐久扫描完成后，证据已经以 scan_id 持久化。preview 只能消费
        # 这份后端快照，不接收前端回传的 entries，避免重复写入或篡改。
        if not durable_evidence and not request.allow_empty:
            raise HTTPException(status_code=409, detail="空来源必须由用户明确确认后才能替代当前 revision")
        evidence = durable_evidence
        if validation is not None:
            # 耐久目录树同样必须遵守 P-008 的验证事实，不能因为扫描已完成就
            # 跳过精确播放根校验。
            if validation["root_id"] != request.root_id:
                raise HTTPException(status_code=409, detail="扫描证据与当前请求不一致，请重新扫描")
            source_locator = validation["effective_root"]
            playback_locator = validation["effective_root"]
            source_route_id = ""
        else:
            source_locator = request.source_locator
            playback_locator = request.playback_locator
            source_route_id = request.source_route_id
    elif validation is not None:
        # 目录树/混合：以后端持久化的证据重建，忽略前端逐条改写。
        evidence = V4Repository(database).list_scan_evidence(request.scan_id)
        if validation["root_id"] != request.root_id or not evidence:
            raise HTTPException(status_code=409, detail="扫描证据与当前请求不一致，请重新扫描")
        source_locator = validation["effective_root"]
        playback_locator = validation["effective_root"]
        # route 已随权威树扫描写入 source_roots，不能再由 preview 请求覆盖。
        source_route_id = ""
    else:
        if not request.entries and not request.allow_empty:
            raise HTTPException(status_code=409, detail="空来源必须由用户明确确认后才能替代当前 revision")
        evidence = _make_entries(request)
        source_locator = request.source_locator
        playback_locator = request.playback_locator
        source_route_id = request.source_route_id
    parser = V4Parser()
    # P-001 7.4 阶段2：来源根上下文是后端派生的权威；preview 从 source_roots
    # 读取并传入解析器，避免「Season 1」等结构容器成为作品名。
    root_container = ""
    with database.connect() as conn:
        root_row = conn.execute(
            "SELECT source_locator, playback_locator, route_id, root_container FROM source_roots WHERE root_id = ?",
            (request.root_id,),
        ).fetchone()
        if root_row is not None:
            root_container = str(root_row["root_container"] or "")
            if evidence_is_persisted:
                source_locator = str(root_row["source_locator"] or source_locator)
                playback_locator = str(root_row["playback_locator"] or playback_locator)
                source_route_id = str(root_row["route_id"] or source_route_id)
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container=root_container)) for item in evidence]
    )
    service = V4RevisionService(database)
    try:
        graph = service.create_draft(
            request.revision_id,
            parsed,
            root_id=request.root_id,
            scan_id=request.scan_id,
            source_metadata={
                "display_name": request.source_display_name,
                "source_locator": source_locator,
                "playback_locator": playback_locator,
                "route_id": source_route_id,
                "root_container": root_container,
            },
            source_mode=request.source_mode,
            _evidence_already_persisted=evidence_is_persisted,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "revision_id": request.revision_id,
        "status": "draft",
        **_graph_to_dict(graph),
    }


@router.post("/imports/{revision_id}/confirm")
def confirm(revision_id: str):
    database = get_database()
    service = V4RevisionService(database)
    with database.connect() as conn:
        row = conn.execute(
            "SELECT scan_id, root_id, status FROM import_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}")
    if row["status"] == "confirmed":
        # 幂等：已确认 revision 直接返回成功，不再要求验证记录（确认后会清理）。
        return {
            "revision_id": revision_id,
            "status": "confirmed",
            "jobs": service.list_jobs(revision_id),
        }
    _require_tree_validation(
        database,
        row["scan_id"],
        row["root_id"],
        require=_revision_has_tree_evidence(database, revision_id),
    )
    try:
        service.confirm(revision_id)
    except (RevisionBlockedError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}") from exc
    # 非权威清理：验证记录与扫描加速数据删除失败不能让接口报错。
    try:
        delete_tree_scan_validation(database, row["scan_id"])
    except Exception:
        pass
    try:
        activate_scan_state(row["scan_id"])
    except OSError:
        # 检查点是可重建的扫描加速数据，发布失败不能反转已确认 revision。
        pass
    return {
        "revision_id": revision_id,
        "status": service.get_status(revision_id),
        "jobs": service.list_jobs(revision_id),
    }


@router.post("/imports/{revision_id}/cancel")
def cancel_confirmed_import(revision_id: str):
    """请求终止已确认 revision 的后台执行。

    运行中的文件/网络步骤不会被强杀，而是在下一安全边界读取标记后收口；
    尚未开始的依赖任务立即取消，避免来源卡永久停在“等待中”。
    """

    from app.media_v4.jobs.runner import V4JobRunner

    try:
        return V4JobRunner(get_database()).cancel_revision(revision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}") from exc


def _revision_has_tree_evidence(database, revision_id: str) -> bool:
    """revision 是否包含目录树来源证据（用于 fail-closed 门控判定）。"""

    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM revision_evidence re
            JOIN source_evidence se ON se.evidence_id = re.evidence_id
            WHERE re.revision_id = ? AND se.ingest_method = 'directory_tree'
            LIMIT 1
            """,
            (revision_id,),
        ).fetchone()
    return row is not None





def _require_tree_validation(database, scan_id: str, root_id: str, *, require: bool) -> None:
    """目录树 confirm 必须 fail-closed：验证缺失/损坏/根变化都 409。

    只有 revision 包含 directory_tree 证据时才要求验证记录；本地/OpenList 不适用。
    映射记录只证明“播放路径已按配置与目录树词法生成”，源盘是否在线由真实播放
    或用户显式诊断负责，不以 24 小时 TTL 或样本可达性作为确认门控。
    """

    if not require:
        return
    validation = load_tree_scan_validation(database, scan_id)
    if validation is None:
        raise HTTPException(
            status_code=409,
            detail="目录树扫描缺少验证记录，请重新扫描后再确认",
        )
    if not validation["ok"]:
        raise HTTPException(
            status_code=409,
            detail="目录树媒体路径未验证通过，请检查来源范围后重新扫描",
        )
    if validation["root_id"] != root_id:
        raise HTTPException(
            status_code=409,
            detail="扫描根与当前 revision 不一致，请重新扫描",
        )


@router.patch("/imports/{revision_id}/evidence/{evidence_id}")
def override_evidence(revision_id: str, evidence_id: str, request: OverrideRequest):
    try:
        graph = V4RevisionService(get_database()).apply_override(
            revision_id,
            evidence_id,
            request.changes,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="revision 或 evidence 不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"revision_id": revision_id, "status": "draft", **_graph_to_dict(graph)}


@router.get("/imports/{revision_id}")
def get_import(revision_id: str):
    service = V4RevisionService(get_database())
    try:
        status = service.get_status(revision_id)
        progress = service.get_execution_progress(revision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}") from exc
    return {
        "revision_id": revision_id,
        "status": status,
        "jobs": service.list_jobs(revision_id),
        "progress": progress,
    }


@router.get("/revisions/{revision_id}/works/{work_id}/execution-detail")
def get_work_execution_detail(
    revision_id: str,
    work_id: str,
    episode_offset: int = Query(default=0, ge=0),
    episode_limit: int = Query(default=50, ge=1, le=100),
):
    """作品级执行详情（3.1）：只读投影，展开时按需读取，不启动任务。"""

    service = V4RevisionService(get_database())
    try:
        return service.get_work_execution_detail(
            revision_id, work_id, episode_offset=episode_offset, episode_limit=episode_limit
        )

    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品执行详情不存在") from exc


def _source_evidence_from_row(row) -> SourceEvidence:
    return SourceEvidence(
        evidence_id=str(row["evidence_id"]),
        scan_id=str(row["scan_id"]),
        root_id=str(row["root_id"]),
        provider=str(row["provider"] or ""),
        source_key=str(row["source_key"] or ""),
        relative_path=str(row["relative_path"] or ""),
        entry_kind=str(row["entry_kind"] or "video"),
        size=row["size"],
        mtime=row["mtime"],
        fingerprint=str(row["fingerprint"] or ""),
        raw_file_id=str(row["raw_file_id"] or ""),
        ingest_method=str(row["ingest_method"] or ""),
        source_route_id=str(row["source_route_id"] or ""),
        source_locator=str(row["source_locator"] or ""),
        playback_locator=str(row["playback_locator"] or ""),
        tmdb_hint_id=str(row["tmdb_hint_id"] or ""),
        tmdb_hint_type=str(row["tmdb_hint_type"] or ""),
        import_family=str(row["import_family"] or "anime"),
        target_filename=str(row["target_filename"] or ""),
        observed_at=str(row["observed_at"] or ""),
        presence_state=str(row["presence_state"] or "present"),
    )


@router.get("/sources/drafts")


@router.get("/sources/drafts")
def list_drafts():
    """列出未确认 draft revision，供“待继续导入”恢复入口使用。

    正式来源卡只代表 confirmed root；draft 是独立草稿，不冒充已建立媒体库。
    """

    with get_database().connect() as conn:
        rows = conn.execute(
            """
            SELECT
                ir.revision_id, ir.root_id, ir.scan_id, ir.created_at,
                sr.provider, sr.source_mode,
                sr.source_locator, sr.playback_locator,
                (
                    SELECT COUNT(*) FROM revision_evidence re
                    WHERE re.revision_id = ir.revision_id
                ) AS evidence_count,
                (
                    SELECT COUNT(*) FROM revision_issues ri
                    WHERE ri.revision_id = ir.revision_id AND ri.resolved = 0
                ) AS issue_count
            FROM import_revisions ir
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE ir.status = 'draft'
              AND (
                  sr.retired_at = ''
                  OR julianday(ir.created_at) > julianday(sr.retired_at)
              )
            ORDER BY ir.created_at DESC, ir.revision_id
            """
        ).fetchall()
    return {"drafts": [dict(row) for row in rows]}


@router.get("/imports/{revision_id}/evidence")
def get_revision_evidence(revision_id: str):
    """返回 draft revision 的完整证据快照，供前端重建识别预览。

    只读 authoritative tables；不改变 ParsedFacts 或 revision 状态。
    """

    with get_database().connect() as conn:
        row = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}")
        rows = conn.execute(
            """
            SELECT se.*
            FROM revision_evidence re
            JOIN source_evidence se ON se.evidence_id = re.evidence_id
            WHERE re.revision_id = ?
            ORDER BY se.relative_path COLLATE NOCASE, se.evidence_id
            """,
            (revision_id,),
        ).fetchall()
    return {"revision_id": revision_id, "status": row["status"], "entries": [asdict(_source_evidence_from_row(item)) for item in rows]}


@router.get("/sources/libraries")
def list_source_libraries():
    """列出活动来源根及其最新 revision 的来源卡 read model。

    由 source_libraries read model 一次组装（时间、作品规模、作品预览、
    P-003 作品级进度），来源卡不再由前端拼接数据库计数与 raw job 汇总；
    退役来源统一排除。
    """

    from app.media_v4.projection.source_libraries import list_source_cards

    return {"cards": list_source_cards(get_database())}


@router.delete("/sources/libraries/{root_id}")
def hide_source_library_card(root_id: str):
    """隐藏一个已停止来源的卡片，不清理它已建立的媒体库。"""

    from app.media_v4.projection.source_libraries import hide_source_card

    try:
        return hide_source_card(get_database(), root_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="来源卡不存在或已移除") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/sources/libraries/{root_id}")
def rename_source_library_card(root_id: str, request: SourceCardRenameRequest):
    """重命名已登记来源卡，不改变来源路径、媒体库或任务。"""

    from app.media_v4.projection.source_libraries import rename_source_card

    try:
        return rename_source_card(get_database(), root_id, request.display_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="来源卡不存在或已移除") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _local_root_identity(path: str) -> tuple[str, str]:
    """为耐久本地扫描预先登记稳定来源根，不枚举目录内容。"""

    root = Path(path).expanduser().resolve()
    root_id = "root_" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
    return root_id, str(root)


def _durable_draft_finalizer(
    database: V4Database,
    request: SourceScanRequest,
    *,
    root_id: str,
    scan_id: str,
):
    """把识别和候选解析绑定到耐久来源任务；旧调用方未传 revision 时兼容跳过。

    实现已抽到 sources.scan_finalize，与 worker 的恢复路径共用同一份幂等
    语义；这里只是 SourceScanRequest 形参的兼容包装。
    """

    from app.media_v4.sources.scan_finalize import draft_finalizer

    return draft_finalizer(
        database,
        revision_id=request.revision_id.strip(),
        source_display_name=request.source_display_name or "",
        root_id=root_id,
        scan_id=scan_id,
    )


def _openlist_scan_request(request: SourceScanRequest, provider: str, remote_root: str, config, routes) -> dict:
    """序列化 OpenList 扫描参数；只含非敏感路由与根信息，凭据执行期解析。"""

    from app.api.openlist_v4 import _remote_root

    return {
        "remote_root": remote_root,
        "mapping_root": _remote_root(config),
        "mount_root": config.openlist_mount_root,
        "provider": provider,
        "routes": [
            {
                "route_id": str(getattr(route, "route_id", "") or ""),
                "remote_prefix": str(getattr(route, "remote_prefix", "") or ""),
                "provider_id": str(getattr(route, "provider_id", "") or ""),
                "enabled": bool(getattr(route, "enabled", True)),
            }
            for route in routes
        ],
        "revision_id": request.revision_id.strip(),
        "source_display_name": request.source_display_name or "",
    }


def _start_durable_local_scan(request: SourceScanRequest) -> dict:
    from app.media_v4.sources.source_scan_runner import get_source_scan_runner

    if not request.root_path.strip():
        raise HTTPException(status_code=400, detail="本地媒体目录不能为空")
    database = get_database()
    root_id, locator = _local_root_identity(request.root_path)
    scan_id = "scan_" + uuid.uuid4().hex
    config = load_config()
    _register_durable_source_scan(
        database,
        root={
            "root_id": root_id,
            "provider": "local",
            "ingest_method": "local_scan",
            "locator": locator,
            "route_id": "",
            "root_container": _container_name(locator),
            "source_mode": "local",
            "last_scan_mode": "local",
        },
        scan={
            "scan_id": scan_id,
            "root_id": root_id,
            "scan_kind": "local",
            "source_mode": "local",
            "request": {
                "root_path": request.root_path,
                "excluded_roots": _configured_cloud_roots(config),
                "revision_id": request.revision_id.strip(),
                "source_display_name": request.source_display_name or "",
            },
        },
    )
    get_source_scan_runner(database).wake()
    return {"scan_id": scan_id, "root_id": root_id, "scan_mode": "local", "source_mode": "local", "status": "queued"}


def _start_durable_tree_scan(request: SourceScanRequest) -> dict:
    """目录树与 TXT+OpenList 基线共享后台读取/证据持久化路径。"""

    from app.media_v4.sources.input_archive import archive_tree_input
    from app.media_v4.sources.source_scan_runner import (
        InputArchive,
        get_source_scan_runner,
    )

    if not request.tree_file:
        detail = "目录树 + OpenList 增量需要先选择 TXT 基线文件" if request.source == "hybrid" else "目录树 TXT 文件不能为空"
        raise HTTPException(status_code=400, detail=detail)
    config = load_config()
    content_provider = request.provider if request.provider in {"pan115", "baidu", "quark"} else "unknown"
    source_mode = "tree_openlist" if request.source == "hybrid" else "tree_snapshot"
    scan_mode = "tree_baseline" if request.source == "hybrid" else "tree_snapshot"
    route_id = ""
    remote_root = ""
    configured_roots = _configured_tree_roots(config, content_provider)

    if request.source == "hybrid":
        from app.api.openlist_v4 import _configured_routes, _remote_root

        username, _password, credential_state = resolve_openlist_credentials()
        if credential_state == "unavailable":
            raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
        if not config.openlist_server_url or not username:
            raise HTTPException(status_code=400, detail="请先在设置页完成 OpenList 连接配置")
        remote_root = normalize_remote_path(request.root_path.strip() or _remote_root(config))
        routes = _configured_routes(config)
        route_id, content_provider = provider_for_remote(routes, remote_root)
        if not route_id:
            raise HTTPException(status_code=409, detail="当前 OpenList 目录未匹配已保存的内容来源路由")
        if not config.openlist_mount_root:
            raise HTTPException(status_code=409, detail="当前内容来源尚未配置本地挂载路径，请先在设置页完成来源映射")
        configured_roots = [derive_local_path(config.openlist_mount_root, _remote_root(config), remote_root)]
        root_id = openlist_root_id(config.openlist_server_url, username, remote_root)
    else:
        if not configured_roots:
            raise HTTPException(status_code=409, detail="当前内容来源尚未配置本地挂载路径，请先在设置页完成来源映射")

    # 身份与词法解析必须在创建 root/scan 之前完成：只读用户选定的 TXT，
    # 不触源盘。解析结果随 request 持久化，恢复路径复用同一结论。
    tree_text = read_directory_tree_text(request.tree_file)
    resolution = TreePlaybackRootResolver(
        request.tree_file,
        configured_roots=configured_roots,
    ).resolve(tree_text)
    if request.source != "hybrid":
        root_id = tree_scan_root_id(
            provider=content_provider,
            configured_roots=configured_roots,
            resolution=resolution,
            tree_file=request.tree_file,
        )

    database = get_database()
    identity_root = resolution.root or configured_roots[0]
    scan_id = "scan_" + uuid.uuid4().hex
    fact = archive_tree_input(root_id, request.tree_file)
    archive = InputArchive(
        archive_path=str(fact["archive_path"]),
        sha256=str(fact["sha256"]),
        original_filename=str(fact["original_filename"]),
    )
    _register_durable_source_scan(
        database,
        root={
            "root_id": root_id,
            "provider": content_provider,
            "ingest_method": "directory_tree",
            "locator": identity_root,
            "source_locator": remote_root if source_mode == "tree_openlist" else "",
            "playback_locator": identity_root if source_mode == "tree_openlist" else "",
            "route_id": route_id,
            "root_container": _container_name(identity_root),
            "source_mode": source_mode,
            "last_scan_mode": scan_mode,
        },
        scan={
            "scan_id": scan_id,
            "root_id": root_id,
            "scan_kind": scan_mode,
            "source_mode": source_mode,
            "request": {
                "provider": content_provider,
                "route_id": route_id,
                "revision_id": request.revision_id.strip(),
                "source_display_name": request.source_display_name or "",
                "configured_roots": configured_roots,
                "identity_root": identity_root,
                "effective_root": resolution.root,
                "resolution_ok": resolution.ok,
                "resolution_reason": resolution.reason,
                "resolution_candidates": list(resolution.candidates),
                "remote_root": remote_root,
                "scan_mode": scan_mode,
            },
            "archive": archive,
        },
    )
    get_source_scan_runner(database).wake()
    return {
        "scan_id": scan_id,
        "root_id": root_id,
        "scan_mode": scan_mode,
        "source_mode": source_mode,
        "status": "queued",
        "effective_playback_root": resolution.root,
        "path_validation": _tree_validation_dict(resolution),
    }


@router.post("/sources/scans")
def start_durable_scan(request: SourceScanRequest):
    """为所有来源创建 durable SourceScan 并立即返回任务身份。

    大库扫描可离开、可查询、可取消；完成后 evidence 持久化到该 scan 下，
    preview 只消费 completed scan。SourceScan 不进入 confirmed revision jobs。
    """

    if request.source == "local":
        return _start_durable_local_scan(request)
    if request.source in {"tree", "hybrid"}:
        return _start_durable_tree_scan(request)

    from app.api.openlist_v4 import _configured_routes, _remote_root
    from app.media_v4.sources.source_scan_runner import get_source_scan_runner

    config = load_config()
    username, _password, credential_state = resolve_openlist_credentials()
    if credential_state == "unavailable":
        raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
    if not config.openlist_server_url or not username:
        raise HTTPException(status_code=400, detail="请先在设置页完成 OpenList 连接配置")
    if not config.openlist_mount_root:
        raise HTTPException(status_code=409, detail="OpenList 导入需要先配置本地挂载根，才能建立可播放 Asset")
    remote_root = normalize_remote_path(request.root_path.strip() or _remote_root(config))
    routes = _configured_routes(config)
    route_id, routed_provider = provider_for_remote(routes, remote_root)
    if not route_id:
        raise HTTPException(status_code=409, detail="当前 OpenList 目录未匹配已保存的内容来源路由")
    root_id = openlist_root_id(config.openlist_server_url, username, remote_root)
    database = get_database()
    scan_mode = request.scan_mode or "full"
    playback_root = derive_local_path(
        config.openlist_mount_root,
        _remote_root(config),
        remote_root,
    )
    if scan_mode == "incremental":
        baseline = _confirmed_source_evidence(root_id)
        if not baseline:
            raise HTTPException(
                status_code=409,
                detail="此 OpenList 目录尚无已确认基线，请先完成并确认首次完整扫描，或使用 TXT 建立大库基线",
            )
        scan_id = "scan_" + uuid.uuid4().hex
        source_mode = _source_root_mode(root_id) or "openlist_full"
        _register_durable_source_scan(
            database,
            root={
                "root_id": root_id,
                "provider": routed_provider,
                "ingest_method": "openlist_scan",
                "source_locator": remote_root,
                "playback_locator": playback_root,
                "route_id": route_id,
                "root_container": _container_name(remote_root),
                "source_mode": source_mode,
                "last_scan_mode": "incremental",
            },
            scan={
                "scan_id": scan_id,
                "root_id": root_id,
                "scan_kind": "openlist_incremental",
                "source_mode": source_mode,
                "request": _openlist_scan_request(request, routed_provider, remote_root, config, routes),
            },
        )
        get_source_scan_runner(database).wake()
        return {"scan_id": scan_id, "root_id": root_id, "scan_mode": "incremental", "status": "queued"}
    scan_id = "scan_" + uuid.uuid4().hex
    _register_durable_source_scan(
        database,
        root={
            "root_id": root_id,
            "provider": routed_provider,
            "ingest_method": "openlist_scan",
            "source_locator": remote_root,
            "playback_locator": playback_root,
            "route_id": route_id,
            "root_container": _container_name(remote_root),
            "source_mode": "openlist_full",
            "last_scan_mode": "full",
        },
        scan={
            "scan_id": scan_id,
            "root_id": root_id,
            "scan_kind": "openlist_full",
            "source_mode": "openlist_full",
            "request": _openlist_scan_request(request, routed_provider, remote_root, config, routes),
        },
    )
    get_source_scan_runner(database).wake()
    return {"scan_id": scan_id, "root_id": root_id, "scan_mode": "full", "status": "queued"}


@router.get("/sources/scans/{scan_id}")
def get_durable_scan(scan_id: str, include_entries: bool = True):
    from app.media_v4.sources.durable_scan import get_durable_scan

    try:
        result = get_durable_scan(
            get_database(),
            scan_id,
            include_entries=include_entries,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"扫描任务不存在: {scan_id}") from exc
    return result


@router.post("/sources/scans/{scan_id}/cancel")
def cancel_durable_scan(scan_id: str):
    from app.media_v4.sources.durable_scan import cancel_durable_scan, get_durable_scan

    try:
        current = get_durable_scan(get_database(), scan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"扫描任务不存在: {scan_id}") from exc
    if current["status"] not in {"running", "queued"}:
        return {"scan_id": scan_id, "status": current["status"]}
    cancel_durable_scan(scan_id, database=get_database())
    return {"scan_id": scan_id, "status": "cancelling"}


@router.patch("/works/{work_id}/title")
def set_work_title(work_id: str, request: WorkTitleRequest):
    """作品标题用户覆盖层：写入 work_overrides，不覆盖抓取事实。"""

    import json as _json

    now = _now_iso()
    with get_database().connect() as conn:
        existing = conn.execute(
            "SELECT override_json FROM work_overrides WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        payload = _json.loads(existing["override_json"]) if existing else {}
        payload["title"] = request.title.strip()
        conn.execute(
            """
            INSERT INTO work_overrides(work_id, override_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET override_json = excluded.override_json, updated_at = excluded.updated_at
            """,
            (work_id, _json.dumps(payload, ensure_ascii=False), now, now),
        )
    return {"work_id": work_id, "title": request.title.strip()}


@router.delete("/works/{work_id}/title")
def restore_work_title(work_id: str):
    """恢复默认标题：清除用户覆盖。"""

    with get_database().connect() as conn:
        conn.execute("DELETE FROM work_overrides WHERE work_id = ?", (work_id,))
    return {"work_id": work_id, "restored": True}


@router.post("/works/{work_id}/artwork")
async def upload_work_artwork(work_id: str, request: ArtworkUploadRequest):
    """上传作品图片覆盖层；写入受管镜像根内 work 专属目录，返回本地路径。"""

    import base64 as _base64
    from pathlib import Path as _Path

    mirror_root = _configured_mirror_root()
    if mirror_root is None:
        raise HTTPException(status_code=409, detail="尚未配置镜像根目录，无法保存图片")
    if request.kind not in {"poster", "fanart", "clearlogo"}:
        raise HTTPException(status_code=400, detail="不支持的图片类型")
    try:
        raw = _base64.b64decode(request.data_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="图片数据不是有效 Base64") from exc
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 5MB")
    directory = _Path(mirror_root) / "artwork" / work_id
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".png" if request.kind == "poster" else ".jpg"
    target = directory / f"{request.kind}{suffix}"
    target.write_bytes(raw)
    now = _now_iso()
    with get_database().connect() as conn:
        existing = conn.execute(
            "SELECT override_json FROM work_overrides WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        payload = json.loads(existing["override_json"]) if existing else {}
        payload[f"local_{request.kind}_path"] = str(target)
        conn.execute(
            """
            INSERT INTO work_overrides(work_id, override_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET override_json = excluded.override_json, updated_at = excluded.updated_at
            """,
            (work_id, json.dumps(payload, ensure_ascii=False), now, now),
        )
    return {"path": str(target)}


@router.delete("/works/{work_id}/artwork/{kind}")
def restore_work_artwork(work_id: str, kind: str):
    """恢复默认图片：清除用户覆盖并删除受控文件。"""

    from pathlib import Path as _Path

    mirror_root = _configured_mirror_root()
    if kind not in {"poster", "fanart", "clearlogo"}:
        raise HTTPException(status_code=400, detail="不支持的图片类型")
    with get_database().connect() as conn:
        existing = conn.execute(
            "SELECT override_json FROM work_overrides WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        if existing is not None:
            payload = json.loads(existing["override_json"])
            old_path = payload.pop(f"local_{kind}_path", "")
            conn.execute(
                "UPDATE work_overrides SET override_json = ?, updated_at = ? WHERE work_id = ?",
                (json.dumps(payload, ensure_ascii=False), _now_iso(), work_id),
            )
            if mirror_root is not None and old_path:
                try:
                    path = _Path(old_path).resolve(strict=False)
                    root = _Path(mirror_root).resolve(strict=False)
                    if root in path.parents and path.exists() and not path.is_dir():
                        path.unlink()
                except OSError:
                    pass
    return {"work_id": work_id, "restored": True}


@router.get("/works/{work_id}/folder")
def work_folder(work_id: str):
    """返回该作品在已确认 Asset 中的安全播放目录；不可用时说明原因。"""

    with get_database().connect() as conn:
        row = conn.execute(
            """
            SELECT a.playback_locator
            FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            JOIN assets a ON a.asset_id = rb.asset_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed' AND sr.retired_at = ''
              AND a.playback_locator != ''
            ORDER BY a.playback_locator LIMIT 1
            """,
            (work_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="该作品没有可打开的已确认媒体文件")
    folder = str(Path(row["playback_locator"]).parent)
    return {"folder": folder, "exists": Path(folder).is_dir()}


@router.post("/works/{work_id}/scrape")
def enqueue_work_scrape(work_id: str):
    """为指定作品创建/重跑刮削任务（进入同一 V4 job 链）。"""

    with get_database().connect() as conn:
        revision = conn.execute(
            """
            SELECT ir.revision_id FROM import_revisions ir
            JOIN revision_bindings rb ON rb.revision_id = ir.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            JOIN works w ON w.work_id = rb.work_id
            WHERE rb.work_id = ? AND w.status = 'active'
              AND ir.status = 'confirmed' AND sr.retired_at = ''
            ORDER BY ir.confirmed_at DESC LIMIT 1
            """,
            (work_id,),
        ).fetchone()
    if revision is None:
        raise HTTPException(status_code=404, detail="该作品不在任何活动媒体库中")
    job_id = "job_" + uuid.uuid4().hex
    now = _now_iso()
    with get_database().connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, status, attempts, last_error, created_at, updated_at)
            VALUES (?, 'scrape_work', ?, ?, ?, 'queued', 0, '', ?, ?)
            """,
            (job_id, revision["revision_id"], work_id, f"scrape_work:{revision['revision_id']}:{work_id}", now, now),
        )
    return {"work_id": work_id, "job_id": job_id, "status": "queued"}


@router.post("/works/{work_id}/delete-preview")
def work_delete_preview(work_id: str):
    """单作品删除预览（只读计算，不删除任何内容）。"""

    from app.media_v4.maintenance.service import compute_work_delete_preview

    try:
        return compute_work_delete_preview(get_database(), work_id=work_id, mirror_root=_configured_mirror_root())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/works/{work_id}/delete-confirm")
def work_delete_confirm(work_id: str, request: WorkDeleteConfirmRequest):
    """校验并执行单作品删除；digest 不一致返回 409。"""

    from app.media_v4.maintenance.service import confirm_work_delete

    try:
        return confirm_work_delete(
            get_database(),
            work_id=work_id,
            preview_id=request.preview_id,
            digest=request.digest,
            mirror_root=_configured_mirror_root(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sources/openlist/status")
def openlist_status(remote_root: str = ""):
    """查询所选 OpenList 目录的来源根级状态：root_id、来源模式与是否已有已确认基线。

    只读查询，不发起 OpenList 网络请求；前端据此决定展示“完整扫描并建立基线”
    “增量扫描”还是“完整校验”，不再从 ingest_method 或路由存在性猜测入口。
    """

    from app.api.openlist_v4 import _remote_root

    config = load_config()
    username, _password, credential_state = resolve_openlist_credentials()
    if credential_state == "unavailable":
        raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
    if not config.openlist_server_url or not username:
        raise HTTPException(status_code=400, detail="请先在设置页完成 OpenList 连接配置")
    remote_root = normalize_remote_path(remote_root.strip() or _remote_root(config))
    root_id = openlist_root_id(config.openlist_server_url, username, remote_root)
    source_mode = ""
    last_scan_mode = ""
    with get_database().connect() as conn:
        row = conn.execute(
            "SELECT source_mode, last_scan_mode FROM source_roots WHERE root_id = ?",
            (root_id,),
        ).fetchone()
        if row is not None:
            source_mode = str(row["source_mode"] or "")
            last_scan_mode = str(row["last_scan_mode"] or "")
    return {
        "root_id": root_id,
        "remote_root": remote_root,
        "source_mode": source_mode,
        "last_scan_mode": last_scan_mode,
        "has_confirmed_baseline": bool(_confirmed_source_evidence(root_id)),
    }


@router.get("/jobs")
def list_jobs(status: str | None = None):
    query = "SELECT * FROM jobs"
    params: tuple[str, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at, job_id"
    with get_database().connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"jobs": [dict(row) for row in rows]}


@router.post("/jobs/{job_id}/run")
def run_job(job_id: str):
    try:
        result = V4JobRunner(get_database()).process_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": result.job_id,
        "job_type": result.job_type,
        "status": result.status,
        "generation_id": result.snapshot.generation_id if result.snapshot else "",
        "artifact_paths": list(result.materialized.artifact_paths) if result.materialized else [],
        "warnings": list(result.materialized.errors) if result.materialized else [],
    }


@router.post("/imports/{revision_id}/scrape")
def enqueue_scrape(revision_id: str):
    try:
        jobs = V4ScrapeService(get_database()).enqueue_for_revision(revision_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"revision_id": revision_id, "jobs": jobs}


@router.post("/library-maintenance/delete-preview")
def library_delete_preview(request: MaintenancePreviewRequest):
    """按来源清理预览（只读计算，不删除任何内容）。"""

    from app.media_v4.maintenance.service import compute_delete_preview

    try:
        preview = compute_delete_preview(
            get_database(),
            provider=request.scope,
            root_ids=request.root_ids,
            mirror_root=_configured_mirror_root(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return preview


@router.post("/library-maintenance/delete-confirm")
def library_delete_confirm(request: MaintenanceConfirmRequest):
    """校验并执行按来源清理；digest 不一致或活动任务存在时返回 409。"""

    from app.media_v4.maintenance.service import confirm_delete_preview

    try:
        result = confirm_delete_preview(
            get_database(),
            preview_id=request.preview_id,
            scope=request.scope,
            digest=request.digest,
            mirror_root=_configured_mirror_root(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@router.post("/library-maintenance/delete-resume")
def library_delete_resume(request: MaintenanceResumeRequest):
    """恢复同一 operation 的未完成清理（partial_failed / projection_failed）。"""

    from app.media_v4.maintenance.service import resume_operation

    try:
        return resume_operation(
            get_database(),
            preview_id=request.preview_id,
            mirror_root=_configured_mirror_root(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/library")
def library():
    projection = V4LibraryProjection(get_database())
    snapshot = projection.ensure_current()
    return {
        "generation_id": snapshot.generation_id,
        "digest": snapshot.digest,
        "cards": list(snapshot.cards),
    }


@router.post("/playback/progress")
def save_playback_progress(request: PlaybackProgressRequest):
    try:
        V4PlaybackStore(get_database()).save_progress(
            request.work_id,
            request.episode_id,
            request.asset_id,
            request.position,
            request.duration,
            request.completed,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="播放 Asset 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return V4PlaybackStore(get_database()).get_progress(request.episode_id, request.asset_id)


@router.get("/playback/progress/{episode_id}/{asset_id}")
def get_playback_progress(episode_id: str, asset_id: str):
    try:
        return V4PlaybackStore(get_database()).get_progress(episode_id, asset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="播放进度不存在") from exc


@router.post("/tracking/state")
def save_tracking_state(request: TrackingStateRequest):
    try:
        V4TrackingStore(get_database()).save_state(
            request.work_id,
            request.provider,
            provider_id=request.provider_id,
            last_watched_episode=request.last_watched_episode,
            metadata=request.metadata,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="追踪作品不存在或已经失效") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return V4TrackingStore(get_database()).get_state(request.work_id, request.provider)


@router.get("/tracking/state/{work_id}/{provider}")
def get_tracking_state(work_id: str, provider: str):
    try:
        return V4TrackingStore(get_database()).get_state(work_id, provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="追踪状态不存在") from exc


class MetadataSearchRequest(BaseModel):
    work_id: str = Field(min_length=1)
    query: str = ""
    media_type: str = ""


class MetadataConfirmRequest(BaseModel):
    work_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)


class MetadataRetryRequest(BaseModel):
    work_id: str = Field(min_length=1)


@router.post("/metadata/search")
def metadata_search(request: MetadataSearchRequest):
    """V4 手动元数据恢复：搜索端先创建服务端候选记录，供 confirm 选择。"""

    database = get_database()
    with database.connect() as conn:
        work = conn.execute(
            "SELECT * FROM works WHERE work_id = ?", (request.work_id,)
        ).fetchone()
    if work is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    from app.media_v4.jobs.metadata import enrich_candidate_aliases, search_tmdb_candidates

    media_type = request.media_type or ("tv" if work["work_type"] == "series" else "movie")
    query = request.query or work["preferred_title"]
    candidates = search_tmdb_candidates(query, media_type, work["year"])
    if candidates is None:
        raise HTTPException(status_code=409, detail="未配置 TMDB API Token，无法搜索在线候选")
    if not candidates:
        return {"work_id": request.work_id, "candidates": []}
    # R16：人工搜索同样补全 provider 别名（预算内），并持久化 original/aliases。
    candidates = enrich_candidate_aliases(candidates, [query], max_details=8)
    # D4：共享 CandidateRanker 排序；候选按 score 稳定落库并返回推荐标记。
    from app.media_v4.resolution.ranker import rank_candidates

    ranked = rank_candidates(
        {
            "preferred_title": str(work["preferred_title"] or ""),
            "queries": [query],
            "media_type": media_type,
            "year": work["year"],
            "show_type": str(work["show_type"] or ""),
        },
        candidates,
    )

    now = _now_iso()
    with database.connect() as conn:
        revision_row = conn.execute(
            """
            SELECT ir.revision_id FROM import_revisions ir
            JOIN revision_bindings rb ON rb.revision_id = ir.revision_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed'
            ORDER BY ir.confirmed_at DESC, ir.revision_id DESC LIMIT 1
            """,
            (request.work_id,),
        ).fetchone()
        owner_revision = str(revision_row["revision_id"]) if revision_row else ""
        conn.execute(
            "DELETE FROM revision_work_candidates WHERE work_id = ? AND evidence = 'manual_search'",
            (request.work_id,),
        )
        stored = []
        for item in ranked:
            candidate_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO revision_work_candidates(
                    candidate_id, revision_id, work_id, draft_work_key, provider,
                    provider_id, media_type, title, original_title, aliases_json, year,
                    evidence, confidence, status, score, reasons_json, popularity,
                    recommended, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_search', 'high', 'proposed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    owner_revision,
                    request.work_id,
                    str(work["identity_key"]),
                    item.provider,
                    item.provider_id,
                    item.media_type,
                    item.title,
                    item.original_title,
                    json.dumps([str(a) for a in item.aliases], ensure_ascii=False),
                    item.year,
                    item.score,
                    json.dumps(list(item.reasons), ensure_ascii=False),
                    item.popularity,
                    1 if item.recommended else 0,
                    now,
                    now,
                ),
            )
            stored.append({
                "candidate_id": candidate_id,
                "provider": item.provider,
                "provider_id": item.provider_id,
                "media_type": item.media_type,
                "title": item.title,
                "original_title": item.original_title,
                "aliases": list(item.aliases),
                "year": item.year,
                "score": item.score,
                "reasons": list(item.reasons),
                "recommended": item.recommended,
            })
    return {"work_id": request.work_id, "candidates": stored}


@router.post("/metadata/artifacts/retry")
def metadata_artifacts_retry(request: MetadataRetryRequest):
    """只重新下载缺失的图片产物，不重新搜索在线资料。

    与 ``/metadata/retry`` 的区别：本端点复用已保存的 provider 资料，只重发
    poster/fanart/clearlogo，绝不再打 TMDB 搜索或详情接口。
    """

    database = get_database()
    with database.connect() as conn:
        work = conn.execute(
            "SELECT work_id FROM works WHERE work_id = ? AND status = 'active'",
            (request.work_id,),
        ).fetchone()
        revision = conn.execute(
            """
            SELECT ir.revision_id
            FROM import_revisions ir
            JOIN revision_bindings rb ON rb.revision_id = ir.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed' AND sr.retired_at = ''
            ORDER BY ir.confirmed_at DESC, ir.revision_id DESC LIMIT 1
            """,
            (request.work_id,),
        ).fetchone()
    if work is None:
        raise HTTPException(status_code=409, detail="作品不存在或已退出媒体库")
    if revision is None:
        raise HTTPException(status_code=409, detail="该作品没有已确认的 revision，无法重试刮削")
    mirror_root = _configured_mirror_root()
    if mirror_root is None:
        raise HTTPException(status_code=409, detail="请先在设置页配置有效的镜像目录")
    try:
        return V4ScrapeService(database).retry_artifacts(
            str(revision["revision_id"]), request.work_id, mirror_root=mirror_root,
        )
    except KeyError:
        raise HTTPException(status_code=409, detail="该作品没有可重试的刮削任务") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/metadata/retry")
def metadata_retry(request: MetadataRetryRequest):
    """只重排可恢复的资料结果，不把人工候选冲突伪装成普通重试。"""

    database = get_database()
    with database.connect() as conn:
        work = conn.execute(
            "SELECT work_id FROM works WHERE work_id = ? AND status = 'active'",
            (request.work_id,),
        ).fetchone()
        revision = conn.execute(
            """
            SELECT ir.revision_id
            FROM import_revisions ir
            JOIN revision_bindings rb ON rb.revision_id = ir.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed' AND sr.retired_at = ''
            ORDER BY ir.confirmed_at DESC, ir.revision_id DESC LIMIT 1
            """,
            (request.work_id,),
        ).fetchone()
        binding = conn.execute(
            """
            SELECT status, metadata_json
            FROM scrape_bindings
            WHERE revision_id = ? AND work_id = ?
            ORDER BY updated_at DESC, binding_id DESC LIMIT 1
            """,
            (str(revision["revision_id"]) if revision else "", request.work_id),
        ).fetchone() if revision else None
    if work is None:
        raise HTTPException(status_code=409, detail="作品不存在或已退出媒体库")
    if revision is None:
        raise HTTPException(status_code=409, detail="该作品没有已确认的 revision，无法重试刮削")

    metadata: dict = {}
    if binding is not None:
        try:
            decoded = json.loads(str(binding["metadata_json"] or "{}"))
            metadata = decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
    from app.media_v4.revisions.service import metadata_recovery_policy

    policy = metadata_recovery_policy(
        metadata,
        binding_status=str(binding["status"] or "") if binding is not None else "",
    )
    action = policy["action"]
    reason_code = str(metadata.get("reason_code") or "")
    state = str(metadata.get("metadata_state") or (binding["status"] if binding is not None else "") or "")
    if action in {"review_identity", "choose_candidate"}:
        raise HTTPException(status_code=409, detail="请先检查识别结果或选择正确的在线作品")
    if action == "check_settings":
        config = load_config()
        if not config.tmdb_bearer_token:
            raise HTTPException(status_code=409, detail="请先在设置页配置有效的 TMDB Token")
        if reason_code == "mirror_root_missing" and _configured_mirror_root() is None:
            raise HTTPException(status_code=409, detail="请先在设置页配置有效的镜像目录")
    if action == "none" and state:
        raise HTTPException(status_code=409, detail="当前资料状态没有可用的自动重试动作")

    try:
        job = V4ScrapeService(database).requeue_work(str(revision["revision_id"]), request.work_id)
    except KeyError:
        raise HTTPException(status_code=409, detail="该作品没有可重试的刮削任务") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "work_id": request.work_id,
        "revision_id": str(revision["revision_id"]),
        "job_id": str(job["job_id"]),
        "status": str(job["status"]),
        "metadata_recovery_action": action,
    }


@router.post("/works/{work_id}/identity-repair-preview")
def identity_repair_preview(work_id: str):
    """生成作品身份恢复预览；不修改媒体关系。"""

    from app.media_v4.maintenance.identity_repair import build_identity_repair_preview

    try:
        return build_identity_repair_preview(get_database(), work_id=work_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/identity-repair/apply")
def identity_repair_apply(request: IdentityRepairApplyRequest):
    """校验服务端预览摘要后应用身份恢复。"""

    from app.media_v4.maintenance.identity_repair import apply_identity_repair

    try:
        return apply_identity_repair(
            get_database(),
            preview_id=request.preview_id,
            digest=request.digest,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="身份恢复预览不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/identity-repair/resume")
def identity_repair_resume(request: IdentityRepairResumeRequest):
    """继续同一份身份恢复操作，已完成时返回原结果。"""

    from app.media_v4.maintenance.identity_repair import resume_identity_repair

    try:
        return resume_identity_repair(get_database(), operation_id=request.operation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="身份恢复操作不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/metadata/confirm")
def metadata_confirm(request: MetadataConfirmRequest):
    """只接受服务端候选 candidate_id；确认后重排刮削（job 不再搜索）并刷新投影。"""

    database = get_database()
    with database.connect() as conn:
        work = conn.execute(
            "SELECT * FROM works WHERE work_id = ?", (request.work_id,)
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM revision_work_candidates WHERE candidate_id = ?",
            (request.candidate_id,),
        ).fetchone()
        revision = conn.execute(
            """
            SELECT ir.revision_id FROM import_revisions ir
            JOIN revision_bindings rb ON rb.revision_id = ir.revision_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed'
            ORDER BY ir.confirmed_at DESC, ir.revision_id DESC LIMIT 1
            """,
            (request.work_id,),
        ).fetchone()
    if work is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    if revision is None:
        raise HTTPException(status_code=409, detail="该作品没有已确认的 revision，无法恢复刮削")
    if candidate is None:
        raise HTTPException(status_code=404, detail="候选不存在，请先搜索生成候选")
    if str(candidate["work_id"]) != request.work_id:
        raise HTTPException(status_code=409, detail="候选不属于该作品，无法确认")
    if str(candidate["revision_id"]) != str(revision["revision_id"]):
        raise HTTPException(status_code=409, detail="候选不属于当前已确认 revision，请重新搜索")
    if candidate["status"] == "confirmed":
        with database.connect() as conn:
            binding = conn.execute(
                "SELECT provider, provider_id, status FROM scrape_bindings WHERE work_id = ?",
                (request.work_id,),
            ).fetchone()
        return {
            "work_id": request.work_id,
            "candidate_id": request.candidate_id,
            "status": "already_confirmed",
            "binding": dict(binding) if binding else None,
        }
    if candidate["status"] != "proposed":
        raise HTTPException(status_code=409, detail="候选状态不允许确认")
    from app.media_v4.resolution.candidates import supported_provider

    provider = str(candidate["provider"])
    if not supported_provider(provider) or provider == "local":
        raise HTTPException(status_code=409, detail="候选 Provider 不受支持")
    provider_id = str(candidate["provider_id"])
    media_type = str(candidate["media_type"]) or ("tv" if work["work_type"] == "series" else "movie")
    try:
        updated_at = datetime.fromisoformat(str(candidate["updated_at"]))
    except (ValueError, TypeError):
        updated_at = None
    if updated_at is None or datetime.now(UTC) - updated_at > timedelta(hours=24):
        raise HTTPException(status_code=409, detail="候选已过期，请重新搜索")

    with database.connect() as conn:
        identity_owner = conn.execute(
            """
            SELECT work_id FROM provider_bindings
            WHERE provider = ? AND media_type = ? AND provider_id = ?
            """,
            (provider, media_type, provider_id),
        ).fetchone()
        if identity_owner is not None and str(identity_owner["work_id"]) != request.work_id:
            raise HTTPException(status_code=409, detail="该 Provider 身份已经属于另一个作品")
        conn.execute(
            """
            INSERT INTO provider_bindings(work_id, provider, media_type, provider_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_id, provider, media_type) DO UPDATE SET provider_id = excluded.provider_id
            """,
            (request.work_id, provider, media_type, provider_id),
        )
        conn.execute(
            "UPDATE revision_work_candidates SET status = 'confirmed', updated_at = ? WHERE candidate_id = ?",
            (_now_iso(), request.candidate_id),
        )
        conn.execute(
            """
            UPDATE revision_work_candidates SET status = 'rejected', updated_at = ?
            WHERE work_id = ? AND status = 'proposed' AND candidate_id != ?
            """,
            (_now_iso(), request.work_id, request.candidate_id),
        )
    from app.media_v4.jobs.metadata import default_metadata_provider

    scrape = V4ScrapeService(database)
    try:
        job = scrape.requeue_work(str(revision["revision_id"]), request.work_id)
    except KeyError:
        raise HTTPException(status_code=409, detail="该作品没有可重试的刮削任务") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    scrape.process(job["job_id"], default_metadata_provider)
    V4LibraryProjection(database).rebuild()
    return {
        "work_id": request.work_id,
        "candidate_id": request.candidate_id,
        "provider": provider,
        "provider_id": provider_id,
        "status": "confirmed",
    }
