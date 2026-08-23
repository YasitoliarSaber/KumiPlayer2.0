"""V4 media import API: SourceEvidence → preview → confirmed revision → jobs。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import load_config, resolve_openlist_credentials
from app.core.paths import get_data_dir
from app.integrations.openlist.client import normalize_remote_path
from app.integrations.openlist.models import OpenListError
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.jobs.scrape import V4ScrapeService
from app.media_v4.parsing.parser import V4Parser
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
from app.media_v4.sources.scanner import (
    openlist_root_id,
    parse_directory_tree_file,
    scan_local_directory,
    scan_openlist_directory,
    tree_root_id,
)
from app.media_v4.tracking.store import V4TrackingStore

router = APIRouter(prefix="/api/v4", tags=["media-v4"])

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
    scan_mode: Literal["auto", "full"] = "auto"


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


def _confirmed_source_evidence(root_id: str):
    """读取来源根当前 confirmed revision 的完整证据快照。"""

    database = get_database()
    return V4Repository(database).list_confirmed_source_evidence(root_id)


@router.post("/sources/scan")
def scan_source(request: SourceScanRequest):
    if request.source == "local" and not request.root_path.strip():
        raise HTTPException(status_code=400, detail="本地媒体目录不能为空")
    if request.source in {"tree", "hybrid"} and not request.tree_file:
        detail = "目录树 + OpenList 增量需要先选择 TXT 基线文件" if request.source == "hybrid" else "目录树 TXT 文件不能为空"
        raise HTTPException(status_code=400, detail=detail)
    try:
        effective_scan_mode: str = request.source
        scan_stats: dict[str, int] = {}
        content_provider = request.provider if request.provider in {"local", "pan115", "baidu"} else "unknown"
        if request.source == "hybrid":
            effective_scan_mode = "tree_baseline"
            from app.api.openlist_v4 import _remote_root
            from app.integrations.openlist.providers import derive_local_path

            config = load_config()
            username, _password, credential_state = resolve_openlist_credentials()
            if credential_state == "unavailable":
                raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
            if not config.openlist_server_url or not username:
                raise HTTPException(status_code=400, detail="请先在设置页完成 OpenList 连接配置")
            remote_root = normalize_remote_path(request.root_path.strip() or _remote_root(config))
            root_id = openlist_root_id(config.openlist_server_url, username, remote_root)
            local_root = request.source_root.strip()
            if not local_root and config.openlist_mount_root:
                local_root = derive_local_path(config.openlist_mount_root, _remote_root(config), remote_root)
            scan_id, evidence = parse_directory_tree_file(
                request.tree_file,
                root_id=root_id,
                provider=content_provider,
                source_root=local_root,
            )
            stage_scan_state(
                scan_id,
                build_tree_baseline_state(root_id, remote_root, evidence),
            )
        elif request.source == "tree":
            effective_scan_mode = "tree_snapshot"
            root_id = tree_root_id(request.provider, request.source_root, request.tree_file)
            scan_id, evidence = parse_directory_tree_file(
                request.tree_file,
                root_id=root_id,
                provider=content_provider,
                source_root=request.source_root,
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
            root_id = openlist_root_id(config.openlist_server_url, username, remote_root)
            baseline = _confirmed_source_evidence(root_id)
            state = load_active_state(root_id)
            if request.scan_mode != "full" and baseline:
                if not state or state.get("remote_root") != remote_root:
                    state = build_tree_baseline_state(root_id, remote_root, baseline)
                effective_scan_mode = "incremental"
                scan_id, evidence, next_state, scan_stats = scan_openlist_incremental(
                    _client(config),
                    baseline=baseline,
                    state=state,
                    mapping_root=_remote_root(config),
                    mount_root=config.openlist_mount_root,
                    default_provider=content_provider,
                    routes=_configured_routes(config),
                )
                stage_scan_state(scan_id, next_state)
            else:
                effective_scan_mode = "full"
                directory_observations: dict[str, float | None] = {}
                scan_id, evidence = scan_openlist_directory(
                    _client(config),
                    remote_root=remote_root,
                    mapping_root=_remote_root(config),
                    mount_root=config.openlist_mount_root,
                    root_id=root_id,
                    default_provider=content_provider,
                    routes=_configured_routes(config),
                    directory_observations=directory_observations,
                )
                stage_scan_state(
                    scan_id,
                    build_full_scan_state(root_id, remote_root, directory_observations),
                )
        else:
            root_id, scan_id, evidence = scan_local_directory(request.root_path)
    except (FileNotFoundError, NotADirectoryError, OSError, OpenListError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"来源扫描失败: {exc}") from exc
    return {
        "root_id": root_id,
        "scan_id": scan_id,
        "entries": [asdict(item) for item in evidence],
        "scan_mode": effective_scan_mode,
        "scan_stats": scan_stats,
    }


@router.post("/imports/preview")
def preview(request: PreviewRequest):
    database = get_database()
    if not request.entries and not request.allow_empty:
        raise HTTPException(status_code=409, detail="空来源必须由用户明确确认后才能替代当前 revision")
    evidence = _make_entries(request)
    parser = V4Parser()
    parsed = [(item, parser.parse(item)) for item in evidence]
    service = V4RevisionService(database)
    try:
        graph = service.create_draft(
            request.revision_id,
            parsed,
            root_id=request.root_id,
            scan_id=request.scan_id,
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
    try:
        service.confirm(revision_id)
    except (RevisionBlockedError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}") from exc
    with database.connect() as conn:
        row = conn.execute(
            "SELECT scan_id FROM import_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    if row is not None:
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
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"revision 不存在: {revision_id}") from exc
    return {"revision_id": revision_id, "status": status, "jobs": service.list_jobs(revision_id)}


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


@router.get("/library")
def library():
    projection = V4LibraryProjection(get_database())
    snapshot = projection.current() or projection.rebuild()
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
