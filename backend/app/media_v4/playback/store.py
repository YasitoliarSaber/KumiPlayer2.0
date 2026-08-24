"""V4 播放进度存储；不写入作品图身份。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class V4PlaybackStore:
    def __init__(self, database: V4Database):
        self.database = database

    def save_progress(
        self,
        work_id: str,
        episode_id: str,
        asset_id: str,
        position: float,
        duration: float,
        completed: bool,
    ) -> None:
        with self.database.connect() as conn:
            if episode_id == f"movie:{work_id}":
                binding = conn.execute(
                    """
                    SELECT 1 FROM revision_bindings rb
                    JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                    WHERE rb.work_id = ? AND rb.episode_id IS NULL AND rb.asset_id = ?
                      AND ir.status = 'confirmed'
                    LIMIT 1
                    """,
                    (work_id, asset_id),
                ).fetchone()
            else:
                binding = conn.execute(
                    """
                    SELECT 1 FROM revision_bindings rb
                    JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                    WHERE rb.work_id = ? AND rb.episode_id = ? AND rb.asset_id = ?
                      AND ir.status = 'confirmed'
                    LIMIT 1
                    """,
                    (work_id, episode_id, asset_id),
                ).fetchone()
            if binding is None:
                raise KeyError((work_id, episode_id, asset_id))
            conn.execute(
                """
                INSERT INTO playback_progress(
                    episode_id, asset_id, work_id, position, duration, completed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id, asset_id) DO UPDATE SET
                    work_id = excluded.work_id,
                    position = excluded.position,
                    duration = excluded.duration,
                    completed = excluded.completed,
                    updated_at = excluded.updated_at
                """,
                (episode_id, asset_id, work_id, position, duration, int(completed), _now()),
            )
            # P-006：播放历史是独立事件（进度可 upsert，历史必须记录事件）。
            _append_history_event(
                conn,
                work_id=work_id,
                episode_id=episode_id,
                asset_id=asset_id,
            )

    def get_progress(self, episode_id: str, asset_id: str) -> dict:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM playback_progress WHERE episode_id = ? AND asset_id = ?",
                (episode_id, asset_id),
            ).fetchone()
        if row is None:
            raise KeyError((episode_id, asset_id))
        return dict(row)


def _append_history_event(conn, *, work_id: str, episode_id: str, asset_id: str) -> None:
    """写入一条播放历史事件，附当时标题/季集快照与来源。"""

    import uuid

    title_snapshot = ""
    season_snapshot = ""
    episode_snapshot = ""
    source_provider = ""
    row = conn.execute(
        """
        SELECT w.preferred_title AS title, w.year,
               s.local_season_number, e.local_episode_number,
               se.provider
        FROM works w
        LEFT JOIN episodes e ON e.episode_id = ? AND e.work_id = w.work_id
        LEFT JOIN seasons s ON s.season_id = e.season_id
        LEFT JOIN assets a ON a.asset_id = ?
        LEFT JOIN source_evidence se ON se.evidence_id = a.evidence_id
        WHERE w.work_id = ?
        """,
        (episode_id, asset_id, work_id),
    ).fetchone()
    if row is not None:
        title_snapshot = str(row["title"] or "")
        if row["local_season_number"] is not None:
            season_snapshot = f"第 {row['local_season_number']} 季"
        if row["local_episode_number"] is not None:
            episode_snapshot = f"第 {row['local_episode_number']} 集"
        source_provider = str(row["provider"] or "")
    conn.execute(
        """
        INSERT INTO playback_history(
            event_id, work_id, episode_id, asset_id, played_at,
            title_snapshot, season_snapshot, episode_snapshot, source_provider
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "hist_" + uuid.uuid4().hex,
            work_id,
            episode_id,
            asset_id,
            _now(),
            title_snapshot,
            season_snapshot,
            episode_snapshot,
            source_provider,
        ),
    )
