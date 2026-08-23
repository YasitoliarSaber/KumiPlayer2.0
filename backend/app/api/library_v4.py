"""V4 SQLite 投影读模型与本地观看状态接口。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.media_v4 import get_database
from app.media_v4.projection.library import V4LibraryProjection

router = APIRouter(prefix="/api/library", tags=["library"])


class WatchStatusRequest(BaseModel):
    status: str | None = None
    note: str | None = None
    favorite: bool | None = None


def _card_payload(card: dict) -> dict:
    metadata = card.get("metadata") or {}
    media_type = card["media_type"] if card["media_type"] in {"tv", "movie"} else "tv"
    show_type = "anime_series" if media_type == "tv" else "anime_movie"
    sources = metadata.get("sources") or ["local"]
    return {
        "work_id": card["work_id"],
        "title": card["title"],
        "original_title": metadata.get("original_title") or card["title"],
        "year": card["year"],
        "title_provenance": "online" if metadata.get("provider") not in {None, "", "local"} else "local",
        "rating": metadata.get("rating") or 0,
        "plot": metadata.get("plot") or "",
        "genres": metadata.get("genres") or [],
        "studios": metadata.get("studios") or [],
        "media_type": media_type,
        "show_type": show_type,
        "source": sources[0],
        "sources": sources,
        "card_type": "main_series" if media_type == "tv" else "standalone",
        "episodes": [],
        "seasons": [],
        "episode_count": card["episode_count"],
        "asset_count": card["asset_count"],
        "source_locations": {},
        "poster_path": metadata.get("poster_url") or "",
        "fanart_path": metadata.get("fanart_url") or "",
        "local_poster_path": metadata.get("local_poster_path") or "",
        "local_fanart_path": metadata.get("local_fanart_path") or "",
        "clearlogo_path": "",
        "dir_path": "",
        "related_works": [],
        "tags": [],
        "last_played": None,
        "metadata_state": metadata.get("metadata_state") or ("ready" if metadata else "pending"),
    }


def _library_snapshot():
    database = get_database()
    projection = V4LibraryProjection(database)
    return projection.current() or projection.rebuild()


@router.get("")
def get_library(compact: bool = False, source: str | None = None):
    del compact
    snapshot = _library_snapshot()
    works = [_card_payload(card) for card in snapshot.cards]
    if source and source != "all":
        works = [work for work in works if source in work["sources"]]
    return {
        "works": works,
        "summary": {
            "work_count": len(works),
            "episode_count": sum(int(work["episode_count"]) for work in works),
        },
        "generated_at": snapshot.generation_id,
        "needs_rescan": False,
        "digest": snapshot.digest,
    }


@router.get("/works/{work_id}")
def get_work_detail(work_id: str):
    with get_database().connect() as conn:
        work = conn.execute(
            """
            SELECT w.* FROM works w
            WHERE w.work_id = ? AND EXISTS (
                SELECT 1 FROM revision_bindings rb
                JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                WHERE rb.work_id = w.work_id AND ir.status = 'confirmed'
            )
            """,
            (work_id,),
        ).fetchone()
        if work is None:
            raise HTTPException(status_code=404, detail=f"作品不存在: {work_id}")
        seasons = conn.execute(
            """
            SELECT s.* FROM seasons s
            WHERE s.work_id = ? AND EXISTS (
                SELECT 1 FROM revision_bindings rb
                JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                JOIN episodes e ON e.episode_id = rb.episode_id
                WHERE e.season_id = s.season_id AND ir.status = 'confirmed'
            )
            ORDER BY s.local_season_number, s.season_id
            """,
            (work_id,),
        ).fetchall()
        episodes = conn.execute(
            """
            SELECT DISTINCT e.*, s.local_season_number, s.season_kind, s.title AS season_title,
                   a.asset_id, a.root_id, a.playback_locator, a.source_locator,
                   a.availability_state, se.provider
            FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN episodes e ON e.episode_id = rb.episode_id
            JOIN seasons s ON s.season_id = e.season_id
            JOIN assets a ON a.asset_id = rb.asset_id
            JOIN source_evidence se ON se.evidence_id = a.evidence_id
            WHERE e.work_id = ? AND ir.status = 'confirmed'
            ORDER BY s.local_season_number, e.local_episode_number, e.episode_id,
                     CASE WHEN a.availability_state = 'available' THEN 0 ELSE 1 END,
                     CASE WHEN se.provider = 'local' THEN 0 ELSE 1 END,
                     CASE lower(a.resolution)
                         WHEN '4k' THEN 4000 WHEN 'uhd' THEN 4000
                         ELSE CAST(replace(replace(lower(a.resolution), 'p', ''), 'i', '') AS INTEGER)
                     END DESC,
                     a.fingerprint, a.asset_id
            """,
            (work_id,),
        ).fetchall()
        movie_assets = conn.execute(
            """
            SELECT a.asset_id, a.playback_locator, a.source_locator,
                   a.availability_state, se.provider
            FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN assets a ON a.asset_id = rb.asset_id
            JOIN source_evidence se ON se.evidence_id = a.evidence_id
            WHERE rb.work_id = ? AND rb.episode_id IS NULL
              AND rb.asset_id IS NOT NULL AND ir.status = 'confirmed'
            ORDER BY CASE WHEN a.availability_state = 'available' THEN 0 ELSE 1 END,
                     CASE WHEN se.provider = 'local' THEN 0 ELSE 1 END,
                     CASE lower(a.resolution)
                         WHEN '4k' THEN 4000 WHEN 'uhd' THEN 4000
                         ELSE CAST(replace(replace(lower(a.resolution), 'p', ''), 'i', '') AS INTEGER)
                     END DESC,
                     a.fingerprint, a.asset_id
            """,
            (work_id,),
        ).fetchall()
        watch_row = conn.execute(
            "SELECT * FROM tracking_states WHERE work_id = ? AND provider = 'local'",
            (work_id,),
        ).fetchone()
        scrape_row = conn.execute(
            """
            SELECT sb.metadata_json
            FROM scrape_bindings sb
            JOIN import_revisions ir ON ir.revision_id = sb.revision_id
            WHERE sb.work_id = ? AND ir.status = 'confirmed'
            ORDER BY sb.updated_at DESC, sb.binding_id DESC LIMIT 1
            """,
            (work_id,),
        ).fetchone()

    episode_map: dict[str, dict] = {}
    source_locations: dict[str, list[str]] = {}
    for row in episodes:
        episode = episode_map.setdefault(
            row["episode_id"],
            {
                "episode_id": row["episode_id"],
                "work_id": work_id,
                "season_number": row["local_season_number"],
                "episode_number": row["local_episode_number"],
                "absolute_episode_number": row["absolute_episode_number"],
                "special_number": row["special_number"],
                "title": row["display_title"] or "",
                "group_type": "season" if row["season_kind"] == "regular" else row["season_kind"],
                "kind": row["episode_kind"],
                "playback_locator": "",
                "asset_id": "",
                "source": row["provider"] or "local",
                "availability": "missing",
                "assets": [],
            },
        )
        if not row["asset_id"]:
            continue
        locator = row["playback_locator"] or row["source_locator"] or ""
        asset = {
            "asset_id": row["asset_id"],
            "playback_locator": locator,
            "availability": row["availability_state"] or "available",
            "source": row["provider"] or "local",
        }
        episode["assets"].append(asset)
        if not episode["asset_id"]:
            episode["asset_id"] = row["asset_id"]
            episode["playback_locator"] = locator
            episode["availability"] = asset["availability"]
        provider = row["provider"] or "local"
        source_locations.setdefault(provider, []).append(row["source_locator"] or locator)

    episode_payload = list(episode_map.values())
    if work["work_type"] == "movie" and movie_assets:
        assets = []
        for row in movie_assets:
            locator = row["playback_locator"] or row["source_locator"] or ""
            provider = row["provider"] or "local"
            assets.append({
                "asset_id": row["asset_id"],
                "playback_locator": locator,
                "availability": row["availability_state"] or "available",
                "source": provider,
            })
            source_locations.setdefault(provider, []).append(row["source_locator"] or locator)
        first = assets[0]
        episode_payload = [{
            "episode_id": f"movie:{work_id}",
            "work_id": work_id,
            "season_number": 0,
            "episode_number": 1,
            "absolute_episode_number": None,
            "special_number": None,
            "title": work["preferred_title"],
            "group_type": "movie",
            "kind": "movie",
            "playback_locator": first["playback_locator"],
            "asset_id": first["asset_id"],
            "source": first["source"],
            "availability": first["availability"],
            "assets": assets,
        }]
    season_payload = [
        {
            "season_id": season["season_id"],
            "season_number": season["local_season_number"],
            "group_type": "season" if season["season_kind"] == "regular" else season["season_kind"],
            "label": season["title"] or (
                "特别篇" if season["season_kind"] == "special" else f"第 {season['local_season_number']} 季"
            ),
            "episode_count": sum(
                1 for item in episode_payload if item["season_number"] == season["local_season_number"]
            ),
            "episodes": [item for item in episode_payload if item["season_number"] == season["local_season_number"]],
        }
        for season in seasons
    ]
    media_type = "tv" if work["work_type"] == "series" else "movie"
    show_type = "anime_series" if media_type == "tv" else "anime_movie"
    watch_status = _watch_payload(dict(watch_row)) if watch_row else None
    try:
        metadata = json.loads(scrape_row["metadata_json"] or "{}") if scrape_row else {}
    except (TypeError, ValueError):
        metadata = {}
    sources = sorted({item["source"] for item in episode_payload}) or ["local"]
    payload = {
        "work_id": work["work_id"],
        "title": metadata.get("title") or work["preferred_title"],
        "original_title": metadata.get("original_title") or work["original_title"] or work["preferred_title"],
        "year": metadata.get("year") or work["year"],
        "title_provenance": "online" if metadata.get("provider") not in {None, "", "local"} else "local",
        "rating": metadata.get("rating") or 0,
        "plot": metadata.get("plot") or "",
        "genres": metadata.get("genres") or [],
        "studios": metadata.get("studios") or [],
        "media_type": media_type,
        "show_type": show_type,
        "source": sources[0],
        "sources": sources,
        "card_type": "main_series" if media_type == "tv" else "standalone",
        "episodes": episode_payload,
        "seasons": season_payload,
        "episode_count": len({item["episode_id"] for item in episode_payload}),
        "asset_count": sum(len(item["assets"]) for item in episode_payload),
        "source_locations": {key: sorted(set(values)) for key, values in source_locations.items()},
        "poster_path": metadata.get("poster_url") or "",
        "fanart_path": metadata.get("fanart_url") or "",
        "local_poster_path": metadata.get("local_poster_path") or "",
        "local_fanart_path": metadata.get("local_fanart_path") or "",
        "clearlogo_path": "",
        "dir_path": "",
        "related_works": [],
        "tags": [],
        "last_played": None,
        "metadata_state": metadata.get("metadata_state") or ("ready" if metadata else "pending"),
        "watch_status": watch_status,
    }
    return payload


def _watch_payload(row: dict) -> dict:
    try:
        metadata = json.loads(row.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return {
        "work_id": row["work_id"],
        "status": metadata.get("status", ""),
        "note": metadata.get("note", ""),
        "favorite": bool(metadata.get("favorite", False)),
        "updated_at": row.get("updated_at", ""),
    }


@router.get("/diagnostics")
def diagnostics():
    snapshot = _library_snapshot()
    return {"ok": True, "digest": snapshot.digest, "summary": {"work_count": len(snapshot.cards)}}


@router.get("/watch-status")
def list_watch_status():
    with get_database().connect() as conn:
        rows = conn.execute("SELECT * FROM tracking_states ORDER BY work_id, provider").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/watch-status/{work_id}")
def get_watch_status(work_id: str):
    with get_database().connect() as conn:
        row = conn.execute(
            "SELECT * FROM tracking_states WHERE work_id = ? AND provider = 'local'",
            (work_id,),
        ).fetchone()
    return _watch_payload(dict(row)) if row else {"work_id": work_id, "status": "", "note": "", "favorite": False}


@router.patch("/watch-status/{work_id}")
def patch_watch_status(work_id: str, request: WatchStatusRequest):
    if request.status is not None and request.status not in {"", "watching", "watched", "on_hold", "dropped"}:
        raise HTTPException(status_code=400, detail="未知观看状态")
    with get_database().connect() as conn:
        exists = conn.execute(
            """
            SELECT 1 FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            WHERE rb.work_id = ? AND ir.status = 'confirmed' LIMIT 1
            """,
            (work_id,),
        ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="作品不存在")
    with get_database().connect() as conn:
        current = conn.execute(
            "SELECT * FROM tracking_states WHERE work_id = ? AND provider = 'local'",
            (work_id,),
        ).fetchone()
    current_metadata = _watch_payload(dict(current)) if current else {"status": "", "note": "", "favorite": False}
    metadata = {
        "status": request.status if request.status is not None else current_metadata["status"],
        "note": request.note if request.note is not None else current_metadata["note"],
        "favorite": request.favorite if request.favorite is not None else current_metadata["favorite"],
    }
    from app.media_v4.tracking.store import V4TrackingStore

    store = V4TrackingStore(get_database())
    try:
        store.save_state(
            work_id,
            "local",
            provider_id="",
            last_watched_episode=None,
            metadata=metadata,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品不存在或已经失效") from exc
    return _watch_payload(store.get_state(work_id, "local"))


@router.post("/rescan")
def rescan_library():
    snapshot = V4LibraryProjection(get_database()).rebuild()
    return {"task_id": f"projection:{snapshot.generation_id}", "status": "succeeded"}
