"""V4 外部追踪状态存储。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class V4TrackingStore:
    def __init__(self, database: V4Database):
        self.database = database

    def save_state(
        self,
        work_id: str,
        provider: str,
        *,
        provider_id: str,
        last_watched_episode: int | None,
        metadata: dict | None = None,
    ) -> None:
        with self.database.connect() as conn:
            active_work = conn.execute(
                """
                SELECT 1 FROM revision_bindings rb
                JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                WHERE rb.work_id = ? AND ir.status = 'confirmed' LIMIT 1
                """,
                (work_id,),
            ).fetchone()
            if active_work is None:
                raise KeyError(work_id)
            conn.execute(
                """
                INSERT INTO tracking_states(
                    work_id, provider, provider_id, last_watched_episode,
                    metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id, provider) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    last_watched_episode = excluded.last_watched_episode,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    work_id,
                    provider,
                    provider_id,
                    last_watched_episode,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    _now(),
                ),
            )

    def get_state(self, work_id: str, provider: str) -> dict:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tracking_states WHERE work_id = ? AND provider = ?",
                (work_id, provider),
            ).fetchone()
        if row is None:
            raise KeyError((work_id, provider))
        return dict(row)
