"""从 V4 SQLite authority 原子重建 Library Projection。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    generation_id: str
    digest: str
    cards: tuple[dict, ...] = field(default_factory=tuple)


class V4LibraryProjection:
    """投影只读 V4 图，发布过程在一个 SQLite 事务内完成。"""

    def __init__(self, database: V4Database):
        self.database = database

    def current(self) -> LibrarySnapshot | None:
        """读取已发布 generation；没有 generation 时返回空。"""

        with self.database.connect() as conn:
            generation_id_row = conn.execute(
                "SELECT value FROM v4_meta WHERE key = 'current_library_generation'"
            ).fetchone()
            if generation_id_row is None:
                return None
            generation_id = str(generation_id_row["value"])
            generation = conn.execute(
                "SELECT digest FROM library_generations WHERE generation_id = ? AND status = 'published'",
                (generation_id,),
            ).fetchone()
            if generation is None:
                return None
            cards = tuple(
                dict(row)
                for row in conn.execute(
                    """
                    SELECT work_id, title, year, media_type, episode_count, asset_count
                    FROM library_cards WHERE generation_id = ? ORDER BY title COLLATE NOCASE, work_id
                    """,
                    (generation_id,),
                ).fetchall()
            )
        return LibrarySnapshot(generation_id, str(generation["digest"]), cards)

    def rebuild(self) -> LibrarySnapshot:
        generation_id = str(uuid.uuid4())
        now = _now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT
                        w.work_id,
                        w.preferred_title AS title,
                        w.year,
                        CASE WHEN w.work_type = 'series' THEN 'tv' ELSE 'movie' END AS media_type,
                        COUNT(DISTINCT e.episode_id) AS episode_count,
                        COUNT(DISTINCT ea.asset_id) AS asset_count
                    FROM works w
                    LEFT JOIN episodes e ON e.work_id = w.work_id
                    LEFT JOIN episode_assets ea ON ea.episode_id = e.episode_id
                    WHERE EXISTS (
                        SELECT 1 FROM revision_bindings rb
                        JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                        WHERE rb.work_id = w.work_id AND ir.status = 'confirmed'
                    )
                    GROUP BY w.work_id, w.preferred_title, w.year, w.work_type
                    ORDER BY title COLLATE NOCASE, w.work_id
                    """
                ).fetchall()
                cards = tuple(
                    {
                        "work_id": row["work_id"],
                        "title": row["title"],
                        "year": row["year"],
                        "media_type": row["media_type"],
                        "episode_count": row["episode_count"],
                        "asset_count": row["asset_count"],
                    }
                    for row in rows
                )
                digest = hashlib.sha256(
                    json.dumps(cards, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    "INSERT INTO library_generations(generation_id, digest, created_at) VALUES (?, ?, ?)",
                    (generation_id, digest, now),
                )
                for card in cards:
                    conn.execute(
                        """
                        INSERT INTO library_cards(
                            generation_id, work_id, title, year, media_type,
                            episode_count, asset_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            generation_id,
                            card["work_id"],
                            card["title"],
                            card["year"],
                            card["media_type"],
                            card["episode_count"],
                            card["asset_count"],
                        ),
                    )
                conn.execute(
                    "UPDATE library_generations SET status = 'published', published_at = ? WHERE generation_id = ?",
                    (now, generation_id),
                )
                conn.execute(
                    "INSERT INTO v4_meta(key, value) VALUES ('current_library_generation', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (generation_id,),
                )
                conn.commit()
                return LibrarySnapshot(generation_id, digest, cards)
            except Exception:
                conn.rollback()
                raise
