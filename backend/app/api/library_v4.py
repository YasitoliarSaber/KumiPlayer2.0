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
    media_type = card["media_type"] if card["media_type"] in {"tv", "movie"} else "tv"
    show_type = "anime_series" if media_type == "tv" else "anime_movie"
    return {
        "work_id": card["work_id"],
        "title": card["title"],
        "original_title": card["title"],
        "year": card["year"],
        "title_provenance": "online",
        "rating": 0,
        "plot": "",
        "genres": [],
        "studios": [],
        "media_type": media_type,
        "show_type": show_type,
        "source": "local",
        "sources": ["local"],
        "card_type": "main_series" if media_type == "tv" else "standalone",
        "episodes": [],
        "seasons": [],
        "episode_count": card["episode_count"],
        "asset_count": card["asset_count"],
        "source_locations": {},
        "poster_path": "",
        "fanart_path": "",
        "local_poster_path": "",
        "local_fanart_path": "",
        "clearlogo_path": "",
        "dir_path": "",
        "related_works": [],
        "tags": [],
        "last_played": None,
        "metadata_state": "ready",
    }


def _library_snapshot():
    database = get_database()
    projection = V4LibraryProjection(database)
    return projection.current() or projection.rebuild()


@router.get("")
def get_library(compact: bool = False, source: str | None = None):
    del compact, source
    snapshot = _library_snapshot()
    works = [_card_payload(card) for card in snapshot.cards]
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
        work = conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
        if work is None:
            raise HTTPException(status_code=404, detail=f"作品不存在: {work_id}")
        seasons = conn.execute(
            "SELECT * FROM seasons WHERE work_id = ? ORDER BY local_season_number, season_id",
            (work_id,),
        ).fetchall()
        episodes = conn.execute(
            """
            SELECT e.*, s.local_season_number, s.season_kind, s.title AS season_title,
                   a.asset_id, a.root_id, a.playback_locator, a.source_locator,
                   a.availability_state, sr.provider
            FROM episodes e
            JOIN seasons s ON s.season_id = e.season_id
            LEFT JOIN episode_assets ea ON ea.episode_id = e.episode_id
            LEFT JOIN assets a ON a.asset_id = ea.asset_id
            LEFT JOIN source_roots sr ON sr.root_id = a.root_id
            WHERE e.work_id = ?
            ORDER BY s.local_season_number, e.local_episode_number, e.episode_id, a.asset_id
            """,
            (work_id,),
        ).fetchall()
        watch_row = conn.execute(
            "SELECT * FROM tracking_states WHERE work_id = ? AND provider = 'local'",
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
    payload = {
        "work_id": work["work_id"],
        "title": work["preferred_title"],
        "original_title": work["original_title"] or work["preferred_title"],
        "year": work["year"],
        "title_provenance": "online",
        "rating": 0,
        "plot": "",
        "genres": [],
        "studios": [],
        "media_type": media_type,
        "show_type": show_type,
        "source": episode_payload[0]["source"] if episode_payload else "local",
        "sources": sorted({item["source"] for item in episode_payload}) or ["local"],
        "card_type": "main_series" if media_type == "tv" else "standalone",
        "episodes": episode_payload,
        "seasons": season_payload,
        "episode_count": len({item["episode_id"] for item in episode_payload}),
        "asset_count": sum(len(item["assets"]) for item in episode_payload),
        "source_locations": {key: sorted(set(values)) for key, values in source_locations.items()},
        "poster_path": "",
        "fanart_path": "",
        "local_poster_path": "",
        "local_fanart_path": "",
        "clearlogo_path": "",
        "dir_path": "",
        "related_works": [],
        "tags": [],
        "last_played": None,
        "metadata_state": "ready",
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
        exists = conn.execute("SELECT 1 FROM works WHERE work_id = ?", (work_id,)).fetchone()
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
    store.save_state(
        work_id,
        "local",
        provider_id="",
        last_watched_episode=None,
        metadata=metadata,
    )
    return _watch_payload(store.get_state(work_id, "local"))


@router.post("/rescan")
def rescan_library():
    snapshot = V4LibraryProjection(get_database()).rebuild()
    return {"task_id": f"projection:{snapshot.generation_id}", "status": "succeeded"}
