"""V4 Revision 草稿、确认和 transactional outbox。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.resolution.resolver import MediaResolver


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RevisionBlockedError(RuntimeError):
    """Revision 仍有未解决 review issue。"""


class V4RevisionService:
    """把一次解析结果固化为 revision，并以单事务发布执行任务。"""

    def __init__(self, database: V4Database):
        self.database = database
        self.repository = V4Repository(database)
        self.resolver = MediaResolver()

    def create_draft(
        self,
        revision_id: str,
        entries: list[tuple[SourceEvidence, ParsedFacts]],
        *,
        resolver_version: str = "v4-resolver-1",
    ) -> None:
        if not entries:
            raise ValueError("revision 至少需要一个来源事实")
        graph = self.resolver.resolve(entries)
        root_id = entries[0][0].root_id
        scan_id = entries[0][0].scan_id
        created_at = _now()

        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_roots(
                    root_id, provider, ingest_method, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (root_id, entries[0][0].provider, entries[0][0].ingest_method, created_at, created_at),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO source_scans(scan_id, root_id, generation, status)
                VALUES (?, ?, 1, 'completed')
                """,
                (scan_id, root_id),
            )

        for evidence, facts in entries:
            self.repository.save_source_evidence(evidence)
            self.repository.save_parsed_facts(facts)

        graph_digest = hashlib.sha256(
            json.dumps(
                {
                    "works": [work.work_key for work in graph.works],
                    "episodes": [episode.episode_key for episode in graph.episodes],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO import_revisions(
                        revision_id, root_id, scan_id, resolver_version, status,
                        graph_digest, created_at
                    ) VALUES (?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (revision_id, root_id, scan_id, resolver_version, graph_digest, created_at),
                )
                work_ids: dict[str, str] = {}
                for work in graph.works:
                    work_id = str(uuid.uuid4())
                    work_ids[work.work_key] = work_id
                    conn.execute(
                        """
                        INSERT INTO works(work_id, work_type, preferred_title, year, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (work_id, "series" if work.media_type == "tv" else "movie", work.preferred_title, work.year, created_at, created_at),
                    )
                episode_ids: dict[str, str] = {}
                season_ids: dict[tuple[str, int | None, str], str] = {}
                for episode in graph.episodes:
                    work_id = work_ids[episode.work_key]
                    season_key = (episode.work_key, episode.local_season_number, episode.season_kind)
                    season_id = season_ids.get(season_key)
                    if season_id is None:
                        season_id = str(uuid.uuid4())
                        season_ids[season_key] = season_id
                        conn.execute(
                            """
                            INSERT INTO seasons(season_id, work_id, local_season_number, season_kind)
                            VALUES (?, ?, ?, ?)
                            """,
                            (season_id, work_id, episode.local_season_number or 0, episode.season_kind),
                        )
                    episode_id = str(uuid.uuid4())
                    episode_ids[episode.episode_key] = episode_id
                    conn.execute(
                        """
                        INSERT INTO episodes(
                            episode_id, work_id, season_id, local_episode_number,
                            absolute_episode_number, special_number, episode_kind
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            episode_id,
                            work_id,
                            season_id,
                            episode.local_episode_number,
                            episode.absolute_episode_number,
                            episode.special_number,
                            episode.episode_kind,
                        ),
                    )
                    for evidence_id in episode.asset_evidence_ids:
                        evidence = conn.execute(
                            "SELECT * FROM source_evidence WHERE evidence_id = ?",
                            (evidence_id,),
                        ).fetchone()
                        if evidence is None:
                            raise ValueError(f"缺少 Asset 来源事实: {evidence_id}")
                        asset_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO assets(
                                asset_id, evidence_id, root_id, source_locator,
                                playback_locator, fingerprint, size, mtime,
                                availability_state
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                asset_id,
                                evidence_id,
                                evidence["root_id"],
                                evidence["source_locator"],
                                evidence["playback_locator"],
                                evidence["fingerprint"],
                                evidence["size"],
                                evidence["mtime"],
                                evidence["presence_state"],
                            ),
                        )
                        conn.execute(
                            "INSERT INTO episode_assets(episode_id, asset_id) VALUES (?, ?)",
                            (episode_id, asset_id),
                        )
                for evidence, _facts in entries:
                    key = next(
                        (episode.episode_key for episode in graph.episodes if evidence.evidence_id in episode.asset_evidence_ids),
                        None,
                    )
                    if key is None:
                        continue
                    work_key = next(episode.work_key for episode in graph.episodes if episode.episode_key == key)
                    conn.execute(
                        """
                        INSERT INTO revision_bindings(
                            revision_id, evidence_id, work_id, episode_id, confidence
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (revision_id, evidence.evidence_id, work_ids[work_key], episode_ids[key], "medium"),
                    )
                for index, issue in enumerate(graph.issues):
                    conn.execute(
                        """
                        INSERT INTO revision_issues(revision_id, issue_id, code, evidence_id, message)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (revision_id, f"issue-{index}", issue.code, issue.evidence_id, issue.message),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_status(self, revision_id: str) -> str:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise KeyError(revision_id)
        return row["status"]

    def confirm(self, revision_id: str) -> None:
        now = _now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                revision = conn.execute(
                    "SELECT status FROM import_revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if revision is None:
                    raise KeyError(revision_id)
                unresolved = conn.execute(
                    "SELECT 1 FROM revision_issues WHERE revision_id = ? AND resolved = 0 LIMIT 1",
                    (revision_id,),
                ).fetchone()
                if unresolved is not None:
                    raise RevisionBlockedError("revision 仍有 review issue，不能确认")
                if revision["status"] not in {"draft", "confirmed"}:
                    raise RuntimeError(f"revision 状态不可确认: {revision['status']}")
                conn.execute(
                    "UPDATE import_revisions SET status = 'confirmed', confirmed_at = ? WHERE revision_id = ?",
                    (now, revision_id),
                )
                work_rows = conn.execute(
                    "SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id = ?",
                    (revision_id,),
                ).fetchall()
                for row in work_rows:
                    work_id = row["work_id"]
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO jobs(
                            job_id, job_type, revision_id, work_id, idempotency_key,
                            created_at, updated_at
                        ) VALUES (?, 'materialize_mirror', ?, ?, ?, ?, ?)
                        """,
                        (str(uuid.uuid4()), revision_id, work_id, f"materialize_mirror:{revision_id}:{work_id}", now, now),
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs(
                        job_id, job_type, revision_id, idempotency_key, created_at, updated_at
                    ) VALUES (?, 'refresh_projection', ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), revision_id, f"refresh_projection:{revision_id}", now, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def list_jobs(self, revision_id: str) -> list[dict]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE revision_id = ? ORDER BY job_type, job_id",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]
