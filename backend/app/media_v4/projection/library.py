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
                self._decode_card(dict(row))
                for row in conn.execute(
                    """
                    SELECT work_id, title, year, media_type, episode_count, asset_count, metadata_json
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
                        CASE WHEN w.work_type = 'series' THEN (
                            SELECT COUNT(DISTINCT rb.episode_id)
                            FROM revision_bindings rb
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            WHERE rb.work_id = w.work_id AND rb.episode_id IS NOT NULL
                              AND ir.status = 'confirmed'
                        ) ELSE CASE WHEN EXISTS (
                            SELECT 1 FROM revision_bindings rb
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            WHERE rb.work_id = w.work_id AND rb.episode_id IS NULL
                              AND rb.asset_id IS NOT NULL AND ir.status = 'confirmed'
                        ) THEN 1 ELSE 0 END END AS episode_count,
                        (
                            SELECT COUNT(DISTINCT rb.asset_id)
                            FROM revision_bindings rb
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            WHERE rb.work_id = w.work_id AND rb.asset_id IS NOT NULL
                              AND ir.status = 'confirmed'
                        ) AS asset_count,
                        COALESCE((
                            SELECT sb.metadata_json FROM scrape_bindings sb
                            JOIN import_revisions sir ON sir.revision_id = sb.revision_id
                            WHERE sb.work_id = w.work_id AND sir.status = 'confirmed'
                            ORDER BY sb.updated_at DESC, sb.binding_id DESC LIMIT 1
                        ), '{}') AS scraped_metadata_json,
                        COALESCE((
                            SELECT sb.status FROM scrape_bindings sb
                            JOIN import_revisions sir ON sir.revision_id = sb.revision_id
                            WHERE sb.work_id = w.work_id AND sir.status = 'confirmed'
                            ORDER BY sb.updated_at DESC, sb.binding_id DESC LIMIT 1
                        ), '') AS scrape_binding_status,
                        COALESCE((
                            SELECT GROUP_CONCAT(DISTINCT provider) FROM (
                                SELECT se.provider
                                FROM revision_bindings rb
                                JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                                JOIN assets a ON a.asset_id = rb.asset_id
                                JOIN source_evidence se ON se.evidence_id = a.evidence_id
                                WHERE rb.work_id = w.work_id AND ir.status = 'confirmed'
                            )
                        ), '') AS source_providers,
                        (
                            SELECT COUNT(DISTINCT s.local_season_number)
                            FROM seasons s
                            JOIN episodes e ON e.season_id = s.season_id
                            JOIN revision_bindings rb ON rb.episode_id = e.episode_id
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            WHERE s.work_id = w.work_id AND ir.status = 'confirmed'
                              AND s.season_kind = 'regular'
                        ) AS regular_season_count,
                        (
                            SELECT COUNT(DISTINCT s.local_season_number)
                            FROM seasons s
                            JOIN episodes e ON e.season_id = s.season_id
                            JOIN revision_bindings rb ON rb.episode_id = e.episode_id
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            WHERE s.work_id = w.work_id AND ir.status = 'confirmed'
                              AND s.season_kind != 'regular'
                        ) AS special_season_count
                    FROM works w
                    WHERE EXISTS (
                        SELECT 1 FROM revision_bindings rb
                        JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                        WHERE rb.work_id = w.work_id AND ir.status = 'confirmed'
                    )
                    ORDER BY title COLLATE NOCASE, w.work_id
                    """
                ).fetchall()
                cards_list = []
                for row in rows:
                    try:
                        metadata = json.loads(row["scraped_metadata_json"] or "{}")
                    except (TypeError, ValueError):
                        metadata = {}
                    metadata["sources"] = sorted(
                        filter(None, str(row["source_providers"] or "").split(","))
                    )
                    regular_season_count = int(row["regular_season_count"] or 0)
                    special_season_count = int(row["special_season_count"] or 0)
                    metadata["regular_season_count"] = regular_season_count
                    metadata["special_season_count"] = special_season_count
                    # 元数据状态：只有 confirmed + ready 才是正式可发布；
                    # 等待类状态保持非 ready，不得冒充成功（P-001 7.3.E）。
                    binding_status = str(row["scrape_binding_status"] or "")
                    meta_state = str(metadata.get("metadata_state") or "")
                    if binding_status == "confirmed" and meta_state == "ready":
                        state = "ready"
                    elif binding_status in {"waiting_metadata", "waiting_review", "source_unavailable", "failed"}:
                        state = binding_status
                    elif binding_status == "confirmed":
                        state = meta_state or "waiting_metadata"
                    else:
                        state = "waiting_metadata"
                    metadata["metadata_state"] = state
                    cards_list.append({
                        "work_id": row["work_id"],
                        "title": metadata.get("title") or row["title"],
                        "year": metadata.get("year") or row["year"],
                        "media_type": row["media_type"],
                        "episode_count": row["episode_count"],
                        "asset_count": row["asset_count"],
                        "regular_season_count": regular_season_count,
                        "special_season_count": special_season_count,
                        "metadata": metadata,
                    })
                cards = tuple(cards_list)
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
                            episode_count, asset_count, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            generation_id,
                            card["work_id"],
                            card["title"],
                            card["year"],
                            card["media_type"],
                            card["episode_count"],
                            card["asset_count"],
                            json.dumps(card["metadata"], ensure_ascii=False, sort_keys=True),
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
                conn.execute(
                    "DELETE FROM library_generations WHERE generation_id != ?",
                    (generation_id,),
                )
                conn.commit()
                return LibrarySnapshot(generation_id, digest, cards)
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _decode_card(card: dict) -> dict:
        try:
            card["metadata"] = json.loads(card.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            card.pop("metadata_json", None)
            card["metadata"] = {}
        return card
