"""Tracking API backed by V4 work/provider state."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.media_v4 import get_database
from app.media_v4.tracking.store import V4TrackingStore

router = APIRouter(prefix="/api/tracking", tags=["tracking"])


class TrackingRequest(BaseModel):
    provider: str = "local"
    provider_id: str = ""
    last_watched_episode: int | None = None
    metadata: dict = Field(default_factory=dict)


@router.get("/works")
def list_tracking():
    with get_database().connect() as conn:
        rows = conn.execute("SELECT * FROM tracking_states ORDER BY work_id, provider").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/works/{work_id}")
def get_tracking(work_id: str):
    with get_database().connect() as conn:
        row = conn.execute("SELECT * FROM tracking_states WHERE work_id = ?", (work_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="追踪状态不存在")
    return dict(row)


@router.put("/works/{work_id}")
@router.patch("/works/{work_id}")
def save_tracking(work_id: str, request: TrackingRequest):
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
    try:
        V4TrackingStore(get_database()).save_state(
            work_id,
            request.provider,
            provider_id=request.provider_id,
            last_watched_episode=request.last_watched_episode,
            metadata=request.metadata,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="作品不存在或已经失效") from exc
    return V4TrackingStore(get_database()).get_state(work_id, request.provider)
