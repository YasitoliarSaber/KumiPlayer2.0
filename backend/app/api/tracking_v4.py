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
from app.core.config import load_config

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


def _active_openlist_roots_for_tracking(database) -> list[dict]:
    """返回拥有 watching/on_hold 作品的 openlist 活动来源根（供增量扫描）。"""

    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT sr.root_id, sr.source_locator
            FROM tracking_states t
            JOIN works w ON w.work_id = t.work_id
            JOIN revision_bindings rb ON rb.work_id = w.work_id
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE ir.status = 'confirmed' AND sr.retired_at = ''
              AND sr.source_locator LIKE '/%'
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _enqueue_root_incremental(database, config, *, root_id: str, remote_root: str) -> dict:
    from app.api.media_v4 import (
        _confirmed_source_evidence,
        _ensure_root_container,
        _source_root_mode,
    )
    from app.api.openlist_v4 import _client, _configured_routes, _remote_root
    from app.media_v4.sources.durable_scan import create_durable_scan
    from app.media_v4.sources.incremental import (
        build_tree_baseline_state,
        load_active_state,
        scan_openlist_incremental,
    )

    routes = _configured_routes(config)
    from app.integrations.openlist.providers import provider_for_remote

    _route_id, routed_provider = provider_for_remote(routes, remote_root)
    baseline = _confirmed_source_evidence(root_id)
    if not baseline:
        raise HTTPException(status_code=409, detail=f"来源 {remote_root} 尚无已确认基线，请先在媒体管理完成首次扫描")
    state = load_active_state(root_id)
    if not state or state.get("remote_root") != remote_root:
        state = build_tree_baseline_state(root_id, remote_root, baseline)
    scan_id = "scan_" + uuid.uuid4().hex
    create_durable_scan(
        database,
        scan_id=scan_id,
        root_id=root_id,
        kind="incremental",
        scan_fn=lambda: scan_openlist_incremental(
            _client(config),
            baseline=baseline,
            state=state,
            mapping_root=_remote_root(config),
            mount_root=config.openlist_mount_root,
            default_provider=routed_provider,
            routes=routes,
        ),
        state_fn=lambda: state,
    )
    _ensure_root_container(
        database,
        root_id=root_id,
        provider=routed_provider,
        ingest_method="openlist_scan",
        locator=remote_root,
        route_id=_route_id,
        root_container=remote_root,
        source_mode=_source_root_mode(root_id) or "openlist_full",
        last_scan_mode="incremental",
    )
    return {"task_id": scan_id, "root_id": root_id, "remote_root": remote_root, "status": "running"}


@router.post("/scan-all")
def tracking_scan_all(request: TrackingScanRequest):
    """对所有包含追更作品的 OpenList 活动来源发起增量扫描（进入同一 V4 链）。"""

    del request.include_scrape
    config = load_config()
    username, _password, credential_state = resolve_openlist_credentials()
    if credential_state == "unavailable":
        raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
    if not config.openlist_server_url or not username:
        raise HTTPException(status_code=400, detail="请先在设置页完成 OpenList 连接配置")
    database = get_database()
    tasks = []
    for root in _active_openlist_roots_for_tracking(database):
        try:
            tasks.append(_enqueue_root_incremental(database, config, root_id=root["root_id"], remote_root=root["source_locator"]))
        except HTTPException as exc:
            tasks.append({"root_id": root["root_id"], "remote_root": root["source_locator"], "status": "blocked", "reason": exc.detail})
    return {"tasks": tasks}


@router.post("/{work_id}/scan")
def tracking_scan_work(work_id: str, request: TrackingScanRequest):
    """对指定追更作品所属的活动 OpenList 来源发起增量扫描。"""

    del request.include_scrape
    config = load_config()
    username, _password, credential_state = resolve_openlist_credentials()
    if credential_state == "unavailable":
        raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
    if not config.openlist_server_url or not username:
        raise HTTPException(status_code=400, detail="请先在设置页完成 OpenList 连接配置")
    database = get_database()
    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT DISTINCT sr.root_id, sr.source_locator
            FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed' AND sr.retired_at = ''
              AND sr.source_locator LIKE '/%'
            LIMIT 1
            """,
            (work_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="该作品不在任何 OpenList 活动来源中，本地来源请到媒体管理执行扫描")
    return _enqueue_root_incremental(database, config, root_id=str(row["root_id"]), remote_root=str(row["source_locator"]))


@router.post("/scans/{scan_id}/cancel")
def tracking_cancel_scan(scan_id: str):
    from app.media_v4.sources.durable_scan import cancel_durable_scan

    if not cancel_durable_scan(scan_id):
        raise HTTPException(status_code=404, detail=f"扫描任务不存在或已结束: {scan_id}")
    return {"scan_id": scan_id, "status": "cancelling"}
