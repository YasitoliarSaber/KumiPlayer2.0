"""P-006 追更管理窄接口。

Tracking 只保存用户选择与扫描策略（tracking_states）；扫描命令复用 V4
durable SourceScan，增量结果进入同一识别/确认链，不复活旧 JSON plan。
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.media_v4 import get_database, resolve_openlist_credentials
from app.core.config import load_config as load_config
from app.media_v4.tracking.refresh import list_refresh_sources, refresh_all_sources

router = APIRouter(prefix="/api/v4/tracking", tags=["tracking-v4"])


class TrackingScanRequest(BaseModel):
    include_scrape: bool = True


def _tracking_work_rows(database) -> list[dict]:
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                t.work_id, t.provider, t.provider_id, t.last_watched_episode,
                t.metadata_json, t.updated_at,
                w.preferred_title AS title, w.show_type, w.work_type,
                (
                    SELECT MAX(e.local_episode_number)
                    FROM episodes e
                    JOIN revision_bindings rb ON rb.episode_id = e.episode_id
                    JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                    JOIN source_roots sr ON sr.root_id = ir.root_id
                    WHERE e.work_id = w.work_id AND ir.status = 'confirmed'
                      AND sr.retired_at = '' AND e.episode_kind = 'regular'
                ) AS latest_episode_number
            FROM tracking_states t
            JOIN works w ON w.work_id = t.work_id
            ORDER BY t.updated_at DESC, t.work_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/works")
def list_tracking_works():
    """列出有追更状态的作品与最新集摘要（用于新番分类与卡片标签）。"""

    items = []
    for row in _tracking_work_rows(get_database()):
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        status = str(metadata.get("status") or "")
        if status not in {"watching", "on_hold"}:
            continue
        items.append({
            "work_id": row["work_id"],
            "title": row["title"] or row["work_id"],
            "show_type": row["show_type"] or "",
            "media_type": "movie" if row["work_type"] == "movie" else "tv",
            "status": status,
            "favorite": bool(metadata.get("favorite", False)),
            "last_watched_episode": row["last_watched_episode"],
            "latest_episode_number": row["latest_episode_number"],
            "updated_at": row["updated_at"],
        })
    return {"works": items}


@router.get("/sources")
def list_tracking_sources():
    return {"sources": list_refresh_sources(get_database())}


def _enqueue_root_incremental(database, config, *, root_id: str, remote_root: str,
                              include_scrape: bool = True, revision_id: str = "") -> dict:
    from app.api.media_v4 import (
        _confirmed_source_evidence,
        _connection_request_guard,
        _register_durable_source_scan,
        _source_openlist_root_id,
        _source_root_mode,
    )
    from app.api.openlist_v4 import _configured_routes, _remote_root
    from app.core.openlist_connections import connection_id, select_connection
    from app.media_v4.sources.connection_identity import root_connection_id
    from app.media_v4.sources.source_scan_runner import get_source_scan_runner

    identity = root_connection_id(database, root_id)
    try:
        config = select_connection(config, identity)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    credentials = resolve_openlist_credentials(identity) if identity != "legacy" else resolve_openlist_credentials()
    username, _password, credential_state = credentials
    if credential_state == "unavailable":
        raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
    if not config.openlist_server_url or not username:
        raise HTTPException(status_code=400, detail="请先在设置页完成此来源的 OpenList 连接配置")
    if identity != "legacy" and root_id != _source_openlist_root_id(
        config, username, remote_root, connection_id=identity,
    ):
        raise HTTPException(status_code=409, detail="此来源的 OpenList 服务器或账号已改变，请先重新建立对应基线")

    routes = _configured_routes(config)
    from app.integrations.openlist.providers import provider_for_remote

    _route_id, routed_provider = provider_for_remote(routes, remote_root)
    if not _route_id:
        raise HTTPException(status_code=409, detail="此来源未匹配所选连接的内容路由")
    baseline = _confirmed_source_evidence(root_id)
    if not baseline:
        raise HTTPException(status_code=409, detail=f"来源 {remote_root} 尚无已确认基线，请先在媒体管理完成首次扫描")
    scan_id = "scan_" + uuid.uuid4().hex
    source_mode = _source_root_mode(root_id) or "openlist_full"
    from app.integrations.openlist.providers import derive_local_path

    with database.connect() as conn:
        root_row = conn.execute("SELECT content_scope FROM source_roots WHERE root_id = ?", (root_id,)).fetchone()
    content_scope = root_row["content_scope"] if root_row else "completed"
    actual_id = _register_durable_source_scan(database, root={
        "root_id": root_id, "provider": routed_provider, "ingest_method": "openlist_scan",
        "source_locator": remote_root, "route_id": _route_id,
        "playback_locator": derive_local_path(config.openlist_mount_root, _remote_root(config), remote_root),
        "root_container": remote_root, "source_mode": source_mode, "last_scan_mode": "incremental",
    }, scan={
        "scan_id": scan_id, "root_id": root_id, "scan_kind": "openlist_incremental", "source_mode": source_mode,
        "request": {
            "revision_id": revision_id or "rev_" + uuid.uuid4().hex,
            "content_scope": content_scope, "auto_update": content_scope == "ongoing" and include_scrape,
            "connection_id": connection_id(config), "remote_root": remote_root,
            **_connection_request_guard(config, username=username),
            "mapping_root": _remote_root(config), "mount_root": config.openlist_mount_root,
            "provider": routed_provider,
            "routes": [{"route_id": r.route_id, "remote_prefix": r.remote_prefix,
                        "provider_id": r.provider_id, "enabled": r.enabled} for r in routes],
        },
    })
    get_source_scan_runner(database).wake()
    return {"task_id": actual_id, "root_id": root_id, "remote_root": remote_root, "status": "queued"}


@router.post("/scan-all")
def tracking_scan_all(request: TrackingScanRequest):
    """更新明确标记为新番且已有确认基线的活动来源。"""
    return {"tasks": refresh_all_sources(get_database(), include_scrape=request.include_scrape)}


@router.post("/{work_id}/scan")
def tracking_scan_work(work_id: str, request: TrackingScanRequest):
    """更新指定作品所属的新番来源，并保留多来源结果。"""
    tasks = refresh_all_sources(get_database(), include_scrape=request.include_scrape, work_id=work_id)
    if not tasks:
        raise HTTPException(status_code=409, detail="该作品没有已确认的新番来源，请在媒体管理检查来源用途")
    return {**tasks[0], "tasks": tasks}


@router.post("/scans/{scan_id}/cancel")
def tracking_cancel_scan(scan_id: str):
    from app.media_v4.sources.durable_scan import cancel_durable_scan

    if not cancel_durable_scan(scan_id, database=get_database()):
        raise HTTPException(status_code=404, detail=f"扫描任务不存在或已结束: {scan_id}")
    return {"scan_id": scan_id, "status": "cancelling"}
