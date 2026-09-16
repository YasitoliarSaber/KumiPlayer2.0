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
        """读取已发布 generation；没有 generation 时返回空。

        三条 SELECT 必须在**同一个读事务**里完成：并发 rebuild 的收尾会
        `DELETE FROM library_generations WHERE generation_id != ?`，而 library_cards
        是 `ON DELETE CASCADE`。没有读事务时（sqlite3 默认 autocommit，SELECT 各自
        看一次最新提交），若 rebuild 在"读到 generation"与"读 cards"之间提交，
        cards 查询会返回空集，于是媒体墙在一切正常的情况下瞬间清空——而脏标记已被
        对方清掉，`ensure_current()` 也不会补重建。WAL 下读事务会稳定持有快照。
        """

        with self.database.connect() as conn:
            conn.execute("BEGIN")
            try:
                snapshot = self._read_current(conn)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return snapshot

    @staticmethod
    def _read_current(conn) -> LibrarySnapshot | None:
        """在同一读事务内读取 generation 与它的 cards。"""

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
            V4LibraryProjection._decode_card(dict(row))
            for row in conn.execute(
                """
                SELECT work_id, title, year, media_type, episode_count, asset_count, metadata_json
                FROM library_cards WHERE generation_id = ? ORDER BY title COLLATE NOCASE, work_id
                """,
                (generation_id,),
            ).fetchall()
        )
        return LibrarySnapshot(generation_id, str(generation["digest"]), cards)

    def ensure_current(self) -> LibrarySnapshot:
        """读取最新投影；权威事实有变化时按需原子重建。"""

        snapshot = self.current()
        with self.database.connect() as conn:
            dirty = conn.execute(
                "SELECT 1 FROM v4_meta WHERE key = 'library_projection_dirty'"
            ).fetchone()
        if snapshot is None or dirty is not None:
            return self.rebuild()
        return snapshot

    def rebuild(self) -> LibrarySnapshot:
        generation_id = str(uuid.uuid4())
        now = _now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    WITH scrape_rows AS (
                        SELECT
                            sb.work_id,
                            sb.revision_id,
                            sb.status,
                            sb.metadata_json,
                            ROW_NUMBER() OVER (
                                PARTITION BY sb.work_id
                                ORDER BY sb.updated_at DESC, sb.binding_id DESC
                            ) AS latest_rank,
                            -- 同一 work 中"最近一次 confirmed 结果"。最新一次刷新失败时用它
                            -- 回退，而不是把已经发布过的作品整条移出媒体墙。
                            ROW_NUMBER() OVER (
                                PARTITION BY sb.work_id, CASE WHEN sb.status = 'confirmed' THEN 1 ELSE 0 END
                                ORDER BY sb.updated_at DESC, sb.binding_id DESC
                            ) AS confirmed_rank
                        FROM scrape_bindings sb
                        JOIN import_revisions sir ON sir.revision_id = sb.revision_id
                        JOIN source_roots srs ON srs.root_id = sir.root_id
                        WHERE sir.status = 'confirmed' AND srs.retired_at = ''
                    ),
                    latest_scrapes AS (
                        SELECT * FROM scrape_rows WHERE latest_rank = 1
                    ),
                    latest_confirmed AS (
                        SELECT * FROM scrape_rows WHERE confirmed_rank = 1 AND status = 'confirmed'
                    )
                    SELECT
                        w.work_id,
                        w.preferred_title AS title,
                        w.year,
                        w.show_type,
                        w.card_type,
                        CASE WHEN w.work_type = 'series' THEN 'tv' ELSE 'movie' END AS media_type,
                        latest_scrapes.revision_id AS revision_id,
                        CASE WHEN w.work_type = 'series' THEN (
                            SELECT COUNT(DISTINCT rb.episode_id)
                            FROM revision_bindings rb
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                            WHERE rb.work_id = w.work_id AND rb.episode_id IS NOT NULL
                              AND ir.status = 'confirmed'
                        ) ELSE CASE WHEN EXISTS (
                            SELECT 1 FROM revision_bindings rb
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                            WHERE rb.work_id = w.work_id AND rb.episode_id IS NULL
                              AND rb.asset_id IS NOT NULL AND ir.status = 'confirmed'
                        ) THEN 1 ELSE 0 END END AS episode_count,
                        (
                            SELECT COUNT(DISTINCT rb.asset_id)
                            FROM revision_bindings rb
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                            WHERE rb.work_id = w.work_id AND rb.asset_id IS NOT NULL
                              AND ir.status = 'confirmed'
                        ) AS asset_count,
                        COALESCE(latest_scrapes.metadata_json, '{}') AS scraped_metadata_json,
                        COALESCE(latest_scrapes.status, '') AS scrape_binding_status,
                        latest_confirmed.revision_id AS previous_ready_revision_id,
                        COALESCE(latest_confirmed.metadata_json, '{}') AS previous_ready_metadata_json,
                        COALESCE((
                            SELECT GROUP_CONCAT(DISTINCT provider) FROM (
                                SELECT se.provider
                                FROM revision_bindings rb
                                JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
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
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                            WHERE s.work_id = w.work_id AND ir.status = 'confirmed'
                              AND s.season_kind = 'regular'
                        ) AS regular_season_count,
                        (
                            SELECT COUNT(DISTINCT s.local_season_number)
                            FROM seasons s
                            JOIN episodes e ON e.season_id = s.season_id
                            JOIN revision_bindings rb ON rb.episode_id = e.episode_id
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                            WHERE s.work_id = w.work_id AND ir.status = 'confirmed'
                              AND s.season_kind != 'regular'
                        ) AS special_season_count,
                        (
                            SELECT MAX(e.local_episode_number)
                            FROM episodes e
                            JOIN revision_bindings rb ON rb.episode_id = e.episode_id
                            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                            WHERE e.work_id = w.work_id AND ir.status = 'confirmed'
                              AND e.episode_kind = 'regular'
                        ) AS latest_episode_number
                    FROM works w
                    LEFT JOIN latest_scrapes
                      ON latest_scrapes.work_id = w.work_id
                    LEFT JOIN latest_confirmed
                      ON latest_confirmed.work_id = w.work_id
                    WHERE EXISTS (
                        SELECT 1 FROM revision_bindings rb
                        JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                            JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
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
                    from app.media_v4.jobs.completeness import artifact_only_failure

                    if artifact_only_failure(metadata):
                        # 历史记录里“只缺图片”的失败只读归一：作品照常进入媒体库，
                        # 产物标成 degraded 供 UI 提示，不写回真实数据。
                        state = "ready"
                        metadata["artifact_state"] = "degraded"
                        metadata["artifact_reasons"] = [
                            str(item) for item in (metadata.get("completeness") or [])
                        ]
                        metadata["reason"] = "部分图片产物缺失，可重新下载"
                    elif binding_status == "confirmed" and meta_state == "ready":
                        # P-001 7.8 R8：投影重建复查 artifact 文件，不信任 JSON。
                        from app.media_v4.jobs.completeness import (
                            artwork_only_reasons,
                            assess_persisted_completeness,
                        )

                        complete, artifact_reasons = assess_persisted_completeness(
                            self.database,
                            revision_id=str(row["revision_id"]),
                            work_id=str(row["work_id"]),
                            metadata=metadata,
                        )
                        if complete:
                            state = "ready"
                            metadata["artifact_state"] = "ready"
                        elif artwork_only_reasons(artifact_reasons):
                            # 只缺图片：作品仍是 ready，只把产物标成 degraded，
                            # 不再从媒体墙消失。
                            state = "ready"
                            metadata["artifact_state"] = "degraded"
                            metadata["artifact_reasons"] = [str(item) for item in artifact_reasons]
                            metadata["reason"] = "部分图片产物缺失，可重新下载"
                        else:
                            # 缺 NFO 等物化失败仍按原判处理。
                            state = "failed"
                            metadata["artifact_state"] = "incomplete"
                            metadata["reason"] = "投影复查发现元数据产物缺失或损坏"
                    elif binding_status in {"waiting_metadata", "waiting_review", "source_unavailable", "failed"}:
                        recovered = self._recover_previous_ready(row, metadata)
                        if recovered is None:
                            state = binding_status
                        else:
                            state, metadata = recovered
                    elif binding_status == "confirmed":
                        state = meta_state or "waiting_metadata"
                    else:
                        state = "waiting_metadata"
                    metadata["metadata_state"] = state
                    show_type = str(row["show_type"] or "")
                    card_type = str(row["card_type"] or "")
                    metadata["show_type"] = show_type
                    metadata["card_type"] = card_type
                    cards_list.append({
                        "work_id": row["work_id"],
                        "title": metadata.get("title") or row["title"],
                        "year": metadata.get("year") or row["year"],
                        "media_type": row["media_type"],
                        "show_type": show_type,
                        "card_type": card_type,
                        "episode_count": row["episode_count"],
                        "asset_count": row["asset_count"],
                        "regular_season_count": regular_season_count,
                        "special_season_count": special_season_count,
                        "latest_episode_number": row["latest_episode_number"],
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
                conn.execute("DELETE FROM v4_meta WHERE key = 'library_projection_dirty'")
                conn.execute(
                    "DELETE FROM library_generations WHERE generation_id != ?",
                    (generation_id,),
                )
                conn.commit()
                return LibrarySnapshot(generation_id, digest, cards)
            except Exception:
                conn.rollback()
                raise

    def _recover_previous_ready(self, row, current_metadata: dict) -> tuple[str, dict] | None:
        """最新一次资料刷新失败时，回退到同一作品上一次成功且产物完整的快照。

        已发布作品不能因为一次瞬时失败（限流、超时、服务端 5xx）就从媒体墙消失、
        连标题与海报一起丢——这与 `_retain_successful_details` / `_artifact_view`
        的既有意图一致。上次结果不是 ready、或复查发现产物已损坏时不回退，
        避免把真正坏掉的作品伪装成正常（只读判断，不写回任何事实）。
        """

        previous_revision = str(row["previous_ready_revision_id"] or "")
        if not previous_revision:
            return None
        try:
            previous_metadata = json.loads(row["previous_ready_metadata_json"] or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(previous_metadata, dict):
            return None
        if str(previous_metadata.get("metadata_state") or "") != "ready":
            return None
        from app.media_v4.jobs.completeness import assess_persisted_completeness

        complete, _reasons = assess_persisted_completeness(
            self.database,
            revision_id=previous_revision,
            work_id=str(row["work_id"]),
            metadata=previous_metadata,
        )
        if not complete:
            return None
        refresh_error = str(current_metadata.get("reason") or "").strip() or (
            str(current_metadata.get("reason_code") or "").strip() or "最近一次资料刷新失败"
        )
        merged = dict(previous_metadata)
        merged["sources"] = current_metadata.get("sources") or []
        merged["regular_season_count"] = current_metadata.get("regular_season_count") or 0
        merged["special_season_count"] = current_metadata.get("special_season_count") or 0
        merged["metadata_state"] = "ready"
        # 明确标注"当前显示的是上一次成功结果"，避免用户误以为这是最新资料。
        merged["metadata_refresh_error"] = refresh_error
        return "ready", merged

    @staticmethod
    def _decode_card(card: dict) -> dict:
        try:
            card["metadata"] = json.loads(card.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            card.pop("metadata_json", None)
            card["metadata"] = {}
        return card
