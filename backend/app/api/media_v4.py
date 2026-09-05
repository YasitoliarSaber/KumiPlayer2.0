"""V4 media import API: SourceEvidence → preview → confirmed revision → jobs。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
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
    locator: str,
    route_id: str,
    root_container: str,
    source_mode: str = "",
    last_scan_mode: str = "",
) -> None:
    """为非目录树来源写入来源根上下文，preview 据此传入解析器。

    source_mode 是来源根级权威模式，非空时覆盖、空时保留既有值；
    last_scan_mode 只记录最近一次扫描方式。
    """

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        conn.execute(
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
                locator,
                locator,
                route_id,
                root_container,
                source_mode,
                last_scan_mode,
                now,
                now,
            ),
        )


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
    root_container: str,
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
                effective_root,
                effective_root,
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
                root_container=_container_name(resolution.root or local_root),
                evidence=evidence,
                resolution=resolution,
                source_mode=root_source_mode,
                last_scan_mode=effective_scan_mode,
            )
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
                locator=remote_root,
                route_id=route_id,
                root_container=_container_name(remote_root),
                source_mode=root_source_mode,
                last_scan_mode=effective_scan_mode,
            )
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
        "effective_playback_root": resolution.root,
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
    evidence_is_persisted = durable_evidence is not None
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
            if evidence_is_persisted and validation is None:
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
            WHERE ir.status = 'draft' AND sr.retired_at = ''
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
    """把识别和候选解析绑定到耐久来源任务；旧调用方未传 revision 时兼容跳过。"""

    revision_id = request.revision_id.strip()
    if not revision_id:
        return None

    def finalize(evidence: list[SourceEvidence], should_cancel=None, on_progress=None) -> None:
        with database.connect() as conn:
            root = conn.execute(
                """
                SELECT provider, source_locator, playback_locator, route_id,
                       display_name, root_container, source_mode
                FROM source_roots WHERE root_id = ?
                """,
                (root_id,),
            ).fetchone()
        if root is None:
            raise ValueError("来源根记录不存在，请重新扫描")
        root_container = str(root["root_container"] or "")
        if should_cancel is not None and should_cancel():
            from app.media_v4.sources.scanner import SourceScanCancelled

            raise SourceScanCancelled()
        parser = V4Parser()
        repository = V4Repository(database)
        # 大型目录树分批解析并持续上报进度，但编号归一化必须在完整来源批次
        # 上执行：128 条只是数据库写入边界，不能把同一季度切成两个语义批次。
        # 解析完成后再统一归一化并分批落盘；取消仍在每个解析/写入批次检查。
        raw_parsed = []
        total = len(evidence)
        if on_progress is not None:
            on_progress(stage="parsing", processed_count=0, total_count=total)
        progress_checkpoint_size = 16
        for offset in range(0, total, 128):
            if should_cancel is not None and should_cancel():
                from app.media_v4.sources.scanner import SourceScanCancelled

                # 已解析的前缀属于不可变审计事实。取消时先按当前已观察批次
                # 完成保守归一化并落盘，但绝不构建 revision；这样既不会把
                # 半成品导入媒体库，也不会让用户等待过的识别工作完全消失。
                if raw_parsed:
                    partial = normalize_batch_parsed_facts(raw_parsed)
                    repository.save_parsed_facts_bulk(
                        [facts for _evidence, facts in partial]
                    )
                raise SourceScanCancelled()
            batch = []
            for index, item in enumerate(evidence[offset : offset + 128], start=1):
                batch.append((item, parser.parse(item, root_container=root_container)))
                if on_progress is not None and (
                    index % progress_checkpoint_size == 0 or offset + index == total
                ):
                    on_progress(
                        stage="parsing",
                        processed_count=offset + index,
                        total_count=total,
                    )
            raw_parsed.extend(batch)
        if should_cancel is not None and should_cancel():
            from app.media_v4.sources.scanner import SourceScanCancelled

            raise SourceScanCancelled()
        # 进入完整归一化前显式上报 normalizing，让界面不误以为还在读取清单。
        if on_progress is not None:
            on_progress(stage="normalizing", processed_count=total, total_count=total)
        parsed = normalize_batch_parsed_facts(raw_parsed)
        for offset in range(0, total, 128):
            if should_cancel is not None and should_cancel():
                from app.media_v4.sources.scanner import SourceScanCancelled

                raise SourceScanCancelled()
            repository.save_parsed_facts_bulk(
                [facts for _evidence, facts in parsed[offset : offset + 128]]
            )
        if should_cancel is not None and should_cancel():
            from app.media_v4.sources.scanner import SourceScanCancelled

            raise SourceScanCancelled()
        if on_progress is not None:
            on_progress(stage="preparing_preview", processed_count=total, total_count=total)
        V4RevisionService(database).create_draft(
            revision_id,
            parsed,
            root_id=root_id,
            scan_id=scan_id,
            source_provider=str(root["provider"] or (evidence[0].provider if evidence else "local")),
            source_metadata={
                "display_name": request.source_display_name or str(root["display_name"] or ""),
                "source_locator": str(root["source_locator"] or ""),
                "playback_locator": str(root["playback_locator"] or ""),
                "route_id": str(root["route_id"] or ""),
                "root_container": root_container,
            },
            source_mode=str(root["source_mode"] or ""),
            _evidence_already_persisted=True,
            _facts_already_persisted=True,
        )

    return finalize


def _start_durable_local_scan(request: SourceScanRequest) -> dict:
    from app.media_v4.sources.durable_scan import create_durable_scan

    if not request.root_path.strip():
        raise HTTPException(status_code=400, detail="本地媒体目录不能为空")
    database = get_database()
    root_id, locator = _local_root_identity(request.root_path)
    _ensure_root_container(
        database,
        root_id=root_id,
        provider="local",
        ingest_method="local_scan",
        locator=locator,
        route_id="",
        root_container=_container_name(locator),
        source_mode="local",
        last_scan_mode="local",
    )
    scan_id = "scan_" + uuid.uuid4().hex
    config = load_config()
    create_durable_scan(
        database,
        scan_id=scan_id,
        root_id=root_id,
        kind="full",
        scan_fn=lambda should_cancel, on_evidence_batch=None, on_progress=None: scan_local_directory(
            request.root_path,
            excluded_roots=_configured_cloud_roots(config),
            should_cancel=should_cancel,
            on_evidence_batch=on_evidence_batch,
            on_progress=on_progress,
        )[1:],
        finalize_fn=_durable_draft_finalizer(
            database,
            request,
            root_id=root_id,
            scan_id=scan_id,
        ),
    )
    return {"scan_id": scan_id, "root_id": root_id, "scan_mode": "local", "source_mode": "local", "status": "running"}


def _start_durable_tree_scan(request: SourceScanRequest) -> dict:
    """目录树与 TXT+OpenList 基线共享后台读取/证据持久化路径。"""

    from app.media_v4.sources.durable_scan import create_durable_scan

    if not request.tree_file:
        detail = "目录树 + OpenList 增量需要先选择 TXT 基线文件" if request.source == "hybrid" else "目录树 TXT 文件不能为空"
        raise HTTPException(status_code=400, detail=detail)
    config = load_config()
    content_provider = request.provider if request.provider in {"pan115", "baidu", "quark"} else "unknown"
    source_mode = "tree_openlist" if request.source == "hybrid" else "tree_snapshot"
    scan_mode = "tree_baseline" if request.source == "hybrid" else "tree_snapshot"
    route_id = ""
    configured_roots = _configured_tree_roots(config, content_provider)

    tree_text: str | None = None
    resolution: TreeRootResolution | None = None
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
        # 身份必须在创建 root/scan 之前确定：先读取 TXT 并完成纯词法解析
        # （只读用户选定的 TXT，不触源盘），与同步入口共享同一解析后身份根，
        # 避免同一逻辑来源因入口不同分裂成不同 root。
        tree_text = read_directory_tree_text(request.tree_file)
        resolution = TreePlaybackRootResolver(
            request.tree_file,
            configured_roots=configured_roots,
        ).resolve(tree_text)
        root_id = tree_scan_root_id(
            provider=content_provider,
            configured_roots=configured_roots,
            resolution=resolution,
            tree_file=request.tree_file,
        )

    database = get_database()
    identity_root = (resolution.root if resolution is not None else "") or configured_roots[0]
    _ensure_root_container(
        database,
        root_id=root_id,
        provider=content_provider,
        ingest_method="directory_tree",
        locator=identity_root,
        route_id=route_id,
        root_container=_container_name(identity_root),
        source_mode=source_mode,
        last_scan_mode=scan_mode,
    )
    scan_id = "scan_" + uuid.uuid4().hex

    def scan_tree(should_cancel=None, on_evidence_batch=None, on_progress=None):
        try:
            if should_cancel is not None and should_cancel():
                from app.media_v4.sources.scanner import SourceScanCancelled

                raise SourceScanCancelled()
            nonlocal tree_text, resolution
            if tree_text is None or resolution is None:
                # hybrid 分支仍在任务创建后读取 TXT；tree 分支已在身份解析时读过。
                tree_text = read_directory_tree_text(request.tree_file)
                resolution = TreePlaybackRootResolver(
                    request.tree_file,
                    configured_roots=configured_roots,
                ).resolve(tree_text)
            _scan_id, evidence = build_directory_tree_evidence(
                tree_text,
                root_id=root_id,
                provider=content_provider,
                source_root=resolution.root,
                source_route_id=route_id,
                scan_id=scan_id,
                on_evidence_batch=on_evidence_batch,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
            _persist_tree_scan(
                database,
                root_id=root_id,
                provider=content_provider,
                scan_id=scan_id,
                route_id=route_id,
                effective_root=resolution.root,
                root_container=_container_name(resolution.root or identity_root),
                evidence=evidence,
                resolution=resolution,
                source_mode=source_mode,
                last_scan_mode=scan_mode,
                durable=True,
                save_evidence=False,
            )
            if request.source == "hybrid":
                stage_scan_state(scan_id, build_tree_baseline_state(root_id, remote_root, evidence))
            return scan_id, evidence
        except DirectoryTreeReadError as exc:
            raise ValueError(_directory_tree_error_message(exc)) from exc

    create_durable_scan(
        database,
        scan_id=scan_id,
        root_id=root_id,
        kind="full",
        scan_fn=scan_tree,
        finalize_fn=_durable_draft_finalizer(
            database,
            request,
            root_id=root_id,
            scan_id=scan_id,
        ),
    )
    result = {
        "scan_id": scan_id,
        "root_id": root_id,
        "scan_mode": scan_mode,
        "source_mode": source_mode,
        "status": "running",
    }
    if resolution is not None:
        # tree 分支的身份在请求内解析完成：与同步入口同字段返回映射结果。
        result["effective_playback_root"] = resolution.root
        result["path_validation"] = _tree_validation_dict(resolution)
    return result


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

    from app.api.openlist_v4 import _client, _configured_routes, _remote_root
    from app.media_v4.sources.durable_scan import create_durable_scan

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
    if scan_mode == "incremental":
        baseline = _confirmed_source_evidence(root_id)
        if not baseline:
            raise HTTPException(
                status_code=409,
                detail="此 OpenList 目录尚无已确认基线，请先完成并确认首次完整扫描，或使用 TXT 建立大库基线",
            )
        state = load_active_state(root_id)
        if not state or state.get("remote_root") != remote_root:
            state = build_tree_baseline_state(root_id, remote_root, baseline)
        _ensure_root_container(
            database,
            root_id=root_id,
            provider=routed_provider,
            ingest_method="openlist_scan",
            locator=remote_root,
            route_id=route_id,
            root_container=_container_name(remote_root),
            source_mode=_source_root_mode(root_id) or "openlist_full",
            last_scan_mode="incremental",
        )
        scan_id = "scan_" + uuid.uuid4().hex
        create_durable_scan(
            database,
            scan_id=scan_id,
            root_id=root_id,
            kind="incremental",
            scan_fn=lambda should_cancel, on_evidence_batch=None, on_progress=None: scan_openlist_incremental(
                _client(config),
                baseline=baseline,
                state=state,
                mapping_root=_remote_root(config),
                mount_root=config.openlist_mount_root,
                default_provider=routed_provider,
                routes=routes,
                should_cancel=should_cancel,
                on_evidence_batch=on_evidence_batch,
                on_progress=on_progress,
            ),
            state_fn=lambda: state,
            finalize_fn=_durable_draft_finalizer(
                database,
                request,
                root_id=root_id,
                scan_id=scan_id,
            ),
        )
        return {"scan_id": scan_id, "root_id": root_id, "scan_mode": "incremental", "status": "running"}
    directory_observations: dict[str, float | None] = {}
    _ensure_root_container(
        database,
        root_id=root_id,
        provider=routed_provider,
        ingest_method="openlist_scan",
        locator=remote_root,
        route_id=route_id,
        root_container=_container_name(remote_root),
        source_mode="openlist_full",
        last_scan_mode="full",
    )
    scan_id = "scan_" + uuid.uuid4().hex
    create_durable_scan(
        database,
        scan_id=scan_id,
        root_id=root_id,
        kind="full",
        scan_fn=lambda should_cancel, on_evidence_batch=None, on_progress=None: scan_openlist_directory(
            _client(config),
            remote_root=remote_root,
            mapping_root=_remote_root(config),
            mount_root=config.openlist_mount_root,
            root_id=root_id,
            default_provider=routed_provider,
            routes=routes,
            directory_observations=directory_observations,
            should_cancel=should_cancel,
            on_evidence_batch=on_evidence_batch,
            on_progress=on_progress,
        ),
        state_fn=lambda: build_full_scan_state(root_id, remote_root, directory_observations),
        finalize_fn=_durable_draft_finalizer(
            database,
            request,
            root_id=root_id,
            scan_id=scan_id,
        ),
    )
    return {"scan_id": scan_id, "root_id": root_id, "scan_mode": "full", "status": "running"}


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
            WHERE rb.work_id = ? AND ir.status = 'confirmed' AND sr.retired_at = ''
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
        for item in candidates:
            candidate_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO revision_work_candidates(
                    candidate_id, revision_id, work_id, draft_work_key, provider,
                    provider_id, media_type, title, original_title, aliases_json, year,
                    evidence, confidence, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_search', 'high', 'proposed', ?, ?)
                """,
                (
                    candidate_id,
                    owner_revision,
                    request.work_id,
                    str(work["identity_key"]),
                    "tmdb",
                    str(item.get("provider_id") or ""),
                    item.get("media_type") or media_type,
                    item.get("title") or "",
                    item.get("original_title") or "",
                    json.dumps([str(a) for a in (item.get("aliases") or [])], ensure_ascii=False),
                    item.get("year"),
                    now,
                    now,
                ),
            )
            stored.append({**item, "candidate_id": candidate_id})
    return {"work_id": request.work_id, "candidates": stored}


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
