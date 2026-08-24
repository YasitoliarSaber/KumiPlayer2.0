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
from app.integrations.openlist.providers import derive_local_path, provider_for_remote
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
    source_display_name: str = ""
    source_locator: str = ""
    playback_locator: str = ""
    source_route_id: str = ""


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
        content_provider = (
            request.provider
            if request.provider in {"local", "pan115", "baidu", "quark", "other"}
            else "unknown"
        )
        if request.source == "hybrid":
            effective_scan_mode = "tree_baseline"
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
            scan_id, evidence = parse_directory_tree_file(
                request.tree_file,
                root_id=root_id,
                provider=content_provider,
                source_root=local_root,
                source_route_id=route_id,
            )
            stage_scan_state(
                scan_id,
                build_tree_baseline_state(root_id, remote_root, evidence),
            )
        elif request.source == "tree":
            effective_scan_mode = "tree_snapshot"
            config = load_config()
            configured_roots = _configured_tree_roots(config, content_provider)
            if not configured_roots:
                raise HTTPException(status_code=409, detail="当前内容来源尚未配置本地挂载路径，请先在设置页完成来源映射")
            requested_root = request.source_root.strip().rstrip("\\/").replace("/", "\\").casefold()
            matched_root = next(
                (root for root in configured_roots if root.replace("/", "\\").casefold() == requested_root),
                "",
            )
            if not matched_root and len(configured_roots) == 1:
                matched_root = configured_roots[0]
            if not matched_root:
                raise HTTPException(status_code=409, detail="前端提交的播放映射与当前设置不一致，请重新选择内容来源")
            root_id = tree_root_id(content_provider, matched_root, request.tree_file)
            scan_id, evidence = parse_directory_tree_file(
                request.tree_file,
                root_id=root_id,
                provider=content_provider,
                source_root=matched_root,
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
            if request.scan_mode == "incremental" and not baseline:
                raise HTTPException(
                    status_code=409,
                    detail="此 OpenList 目录还没有已确认的 TXT 基线，请先建立并确认 TXT 基线",
                )
            if request.scan_mode == "incremental" or (request.scan_mode == "auto" and baseline):
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
                    routes=routes,
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
                    routes=routes,
                    directory_observations=directory_observations,
                )
                stage_scan_state(
                    scan_id,
                    build_full_scan_state(root_id, remote_root, directory_observations),
                )
        else:
            config = load_config()
            root_id, scan_id, evidence = scan_local_directory(
                request.root_path,
                excluded_roots=_configured_cloud_roots(config),
            )
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
            source_metadata={
                "display_name": request.source_display_name,
                "source_locator": request.source_locator,
                "playback_locator": request.playback_locator,
                "route_id": request.source_route_id,
            },
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


@router.get("/sources/libraries")
def list_source_libraries():
    """列出已确认来源根及其最新 revision 的任务进度。

    来源卡只读地投影 V4 authoritative tables；它不是作品卡，也不会通过
    刮削反向推导来源归属。
    """

    with get_database().connect() as conn:
        rows = conn.execute(
            """
            SELECT
                sr.root_id,
                sr.provider,
                sr.ingest_method,
                sr.source_locator,
                sr.playback_locator,
                sr.route_id,
                sr.display_name,
                sr.enabled,
                sr.created_at AS root_created_at,
                sr.updated_at AS root_updated_at,
                ir.revision_id,
                ir.status AS revision_status,
                ir.created_at AS revision_created_at,
                ir.confirmed_at,
                (
                    SELECT COUNT(*) FROM revision_evidence re
                    WHERE re.revision_id = ir.revision_id
                ) AS evidence_count,
                (
                    SELECT COUNT(DISTINCT rb.work_id) FROM revision_bindings rb
                    WHERE rb.revision_id = ir.revision_id AND rb.work_id != ''
                ) AS work_count,
                (
                    SELECT COUNT(DISTINCT rb.asset_id) FROM revision_bindings rb
                    WHERE rb.revision_id = ir.revision_id AND rb.asset_id IS NOT NULL
                ) AS asset_count
            FROM source_roots sr
            JOIN import_revisions ir ON ir.revision_id = (
                SELECT latest.revision_id
                FROM import_revisions latest
                WHERE latest.root_id = sr.root_id AND latest.status = 'confirmed'
                ORDER BY latest.confirmed_at DESC, latest.created_at DESC, latest.revision_id DESC
                LIMIT 1
            )
            ORDER BY ir.confirmed_at DESC, sr.updated_at DESC, sr.root_id
            """
        ).fetchall()
        revision_ids = [row["revision_id"] for row in rows]
        summaries = {
            row["revision_id"]: {
                "total": int(row["total"]),
                "queued": int(row["queued"]),
                "running": int(row["running"]),
                "succeeded": int(row["succeeded"]),
                "failed": int(row["failed"]),
                "cancelled": int(row["cancelled"]),
            }
            for row in conn.execute(
                """
                SELECT
                    revision_id,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
                FROM jobs
                WHERE revision_id IN ({placeholders})
                GROUP BY revision_id
                """.format(placeholders=",".join("?" for _ in revision_ids)),
                revision_ids,
            ).fetchall()
        } if revision_ids else {}

    cards = []
    for row in rows:
        summary = summaries.get(row["revision_id"], {
            "total": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        })
        cards.append({
            **dict(row),
            "display_name": row["display_name"] or row["source_locator"] or f"{row['provider']} 媒体库",
            "evidence_count": int(row["evidence_count"]),
            "work_count": int(row["work_count"]),
            "asset_count": int(row["asset_count"]),
            "job_summary": summary,
            "can_resume": (
                summary["queued"] > 0
                or summary["running"] > 0
                or summary["failed"] > 0
                or summary["cancelled"] > 0
            ),
        })
    return {"cards": cards}


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
