"""V4 Revision 草稿、确认和 transactional outbox。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.media_v4.domain.models import ParsedFacts, ResolvedMediaGraph, SourceEvidence
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.resolution.resolver import MediaResolver


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip(" ._-·:：/\\()（）【】[]{}<>《》「」『』\"'")


def _structural_key(relative_path: str) -> str:
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    return _normalize_title(parts[0] if len(parts) > 1 else PurePosixPath(relative_path).stem)


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
    ) -> ResolvedMediaGraph:
        if not entries:
            raise ValueError("revision 至少需要一个来源事实")
        if any(evidence.root_id != entries[0][0].root_id for evidence, _ in entries):
            raise ValueError("同一 revision 不能混合多个来源根")

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
            existing_scan = conn.execute(
                "SELECT scan_id FROM source_scans WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()
            if existing_scan is None:
                generation = conn.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM source_scans WHERE root_id = ?",
                    (root_id,),
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO source_scans(scan_id, root_id, generation, status)
                    VALUES (?, ?, ?, 'completed')
                    """,
                    (scan_id, root_id, generation),
                )

        # Facts are immutable. Re-observing the same evidence is idempotent and
        # never replaces the previously parsed payload.
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
        facts_by_evidence = {evidence.evidence_id: facts for evidence, facts in entries}
        episode_by_evidence = {
            evidence_id: episode
            for episode in graph.episodes
            for evidence_id in episode.asset_evidence_ids
        }

        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_revision = conn.execute(
                    "SELECT status FROM import_revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if existing_revision is not None:
                    if existing_revision["status"] != "draft":
                        raise ValueError(f"revision 已经不是可重建草稿: {revision_id}")
                    conn.execute(
                        "DELETE FROM import_revisions WHERE revision_id = ?",
                        (revision_id,),
                    )
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
                    related_entries = [
                        (evidence, facts)
                        for evidence, facts in entries
                        if evidence.evidence_id in work.source_evidence_ids
                    ]
                    work_type = "series" if work.media_type == "tv" else "movie"
                    existing_work = conn.execute(
                        "SELECT * FROM works WHERE identity_key = ?",
                        (work.work_key,),
                    ).fetchone()

                    if existing_work is None:
                        provider_candidates = {
                            (facts.tmdb_hint_type.casefold(), str(facts.tmdb_hint_id))
                            for _evidence, facts in related_entries
                            if facts.tmdb_hint_id and facts.tmdb_hint_type
                        }
                        provider_work_ids = {
                            str(row["work_id"])
                            for media_type, provider_id in provider_candidates
                            for row in [
                                conn.execute(
                                    """
                                    SELECT w.work_id
                                    FROM provider_bindings pb
                                    JOIN works w ON w.work_id = pb.work_id
                                    WHERE pb.provider = 'tmdb'
                                      AND pb.media_type = ? AND pb.provider_id = ?
                                    """,
                                    (media_type, provider_id),
                                ).fetchone()
                            ]
                            if row is not None
                        }
                        if len(provider_work_ids) == 1:
                            existing_work = conn.execute(
                                "SELECT * FROM works WHERE work_id = ?",
                                (next(iter(provider_work_ids)),),
                            ).fetchone()

                    if existing_work is None:
                        source_work_ids: set[str] = set()
                        for evidence, _facts in related_entries:
                            structural_key = _structural_key(evidence.relative_path)
                            if not structural_key:
                                continue
                            row = conn.execute(
                                """
                                SELECT w.work_id
                                FROM work_source_bindings b
                                JOIN works w ON w.work_id = b.work_id
                                WHERE b.root_id = ? AND b.structural_key = ?
                                  AND w.work_type = ?
                                  AND (w.year IS NULL OR w.year = ? OR ? IS NULL)
                                """,
                                (root_id, structural_key, work_type, work.year, work.year),
                            ).fetchone()
                            if row is not None:
                                source_work_ids.add(str(row["work_id"]))
                        if len(source_work_ids) == 1:
                            existing_work = conn.execute(
                                "SELECT * FROM works WHERE work_id = ?",
                                (next(iter(source_work_ids)),),
                            ).fetchone()

                    if existing_work is None:
                        candidate_titles = {
                            _normalize_title(value)
                            for _evidence, facts in related_entries
                            for value in (
                                facts.work_title,
                                facts.series_group,
                                *facts.title_candidates,
                            )
                            if _normalize_title(value)
                        }
                        candidate_rows = conn.execute(
                            """
                            SELECT DISTINCT w.*
                            FROM works w
                            LEFT JOIN work_aliases a ON a.work_id = w.work_id
                            WHERE w.work_type = ?
                              AND (w.year IS NULL OR w.year = ? OR ? IS NULL)
                            """,
                            (work_type, work.year, work.year),
                        ).fetchall()
                        alias_matches = [
                            row
                            for row in candidate_rows
                            if _normalize_title(row["preferred_title"]) in candidate_titles
                            or any(
                                _normalize_title(alias["normalized_title"]) in candidate_titles
                                for alias in conn.execute(
                                    "SELECT normalized_title FROM work_aliases WHERE work_id = ?",
                                    (row["work_id"],),
                                ).fetchall()
                            )
                        ]
                        if len(alias_matches) == 1:
                            existing_work = alias_matches[0]

                    if existing_work is None:
                        work_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO works(
                                work_id, identity_key, work_type, preferred_title,
                                year, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                work_id,
                                work.work_key,
                                work_type,
                                work.preferred_title,
                                work.year,
                                created_at,
                                created_at,
                            ),
                        )
                    else:
                        work_id = str(existing_work["work_id"])
                        if existing_work["preferred_title"]:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO work_aliases(
                                    work_id, normalized_title, alias_type
                                ) VALUES (?, ?, 'previous_preferred')
                                """,
                                (work_id, _normalize_title(existing_work["preferred_title"])),
                            )
                        conn.execute(
                            "UPDATE works SET preferred_title = ?, year = ?, updated_at = ? WHERE work_id = ?",
                            (work.preferred_title, work.year, created_at, work_id),
                        )

                    work_ids[work.work_key] = work_id
                    for evidence, facts in related_entries:
                        structural_key = _structural_key(evidence.relative_path)
                        if structural_key:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO work_source_bindings(
                                    work_id, root_id, structural_key, confidence, binding_source
                                ) VALUES (?, ?, ?, ?, 'resolver')
                                """,
                                (work_id, root_id, structural_key, facts.confidence),
                            )
                        for title in (facts.work_title, facts.series_group, *facts.title_candidates):
                            normalized_title = _normalize_title(title)
                            if normalized_title:
                                conn.execute(
                                    """
                                    INSERT OR IGNORE INTO work_aliases(
                                        work_id, normalized_title, alias_type
                                    ) VALUES (?, ?, 'observed')
                                    """,
                                    (work_id, normalized_title),
                                )

                # Provider identity is a mapping, never a replacement for the
                # local Work identity.  Hints observed during parsing are
                # attached here without changing local season/episode numbers.
                for evidence, facts in entries:
                    if not facts.tmdb_hint_id or not facts.tmdb_hint_type:
                        continue
                    episode = episode_by_evidence.get(evidence.evidence_id)
                    work_key = episode.work_key if episode is not None else next(
                        (work.work_key for work in graph.works if evidence.evidence_id in work.source_evidence_ids),
                        "",
                    )
                    if not work_key:
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO provider_bindings(
                            work_id, provider, media_type, provider_id
                        ) VALUES (?, 'tmdb', ?, ?)
                        """,
                        (
                            work_ids[work_key],
                            facts.tmdb_hint_type,
                            str(facts.tmdb_hint_id),
                        ),
                    )

                episode_ids: dict[str, str] = {}
                season_ids: dict[tuple[str, int | None, str], str] = {}
                edition_ids: dict[str, str] = {}
                asset_ids: dict[tuple[str, str], str] = {}

                for episode in graph.episodes:
                    work_id = work_ids[episode.work_key]
                    season_key = (episode.work_key, episode.local_season_number, episode.season_kind)
                    season_id = season_ids.get(season_key)
                    if season_id is None:
                        season_row = conn.execute(
                            """
                            SELECT season_id FROM seasons
                            WHERE work_id = ? AND local_season_number = ? AND season_kind = ?
                            """,
                            (work_id, episode.local_season_number or 0, episode.season_kind),
                        ).fetchone()
                        if season_row is None:
                            season_id = str(uuid.uuid4())
                            conn.execute(
                                """
                                INSERT INTO seasons(season_id, work_id, local_season_number, season_kind)
                                VALUES (?, ?, ?, ?)
                                """,
                                (season_id, work_id, episode.local_season_number or 0, episode.season_kind),
                            )
                        else:
                            season_id = str(season_row["season_id"])
                        season_ids[season_key] = season_id

                    episode_row = conn.execute(
                        """
                        SELECT episode_id FROM episodes
                        WHERE work_id = ? AND season_id = ?
                          AND local_episode_number IS ?
                          AND special_number IS ?
                          AND episode_kind = ? AND edition_key = ?
                        """,
                        (
                            work_id,
                            season_id,
                            episode.local_episode_number,
                            episode.special_number,
                            episode.episode_kind,
                            episode.edition_key,
                        ),
                    ).fetchone()
                    if episode_row is None:
                        episode_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO episodes(
                                episode_id, work_id, season_id, local_episode_number,
                                absolute_episode_number, special_number, episode_kind,
                                edition_key
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                episode_id,
                                work_id,
                                season_id,
                                episode.local_episode_number,
                                episode.absolute_episode_number,
                                episode.special_number,
                                episode.episode_kind,
                                episode.edition_key,
                            ),
                        )
                    else:
                        episode_id = str(episode_row["episode_id"])
                    episode_ids[episode.episode_key] = episode_id

                    edition_row = conn.execute(
                        "SELECT edition_id FROM editions WHERE episode_id = ? AND edition_key = ?",
                        (episode_id, episode.edition_key),
                    ).fetchone()
                    if edition_row is None:
                        edition_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO editions(edition_id, episode_id, edition_key, display_name)
                            VALUES (?, ?, ?, ?)
                            """,
                            (edition_id, episode_id, episode.edition_key, episode.edition_key),
                        )
                    else:
                        edition_id = str(edition_row["edition_id"])
                    edition_ids[episode.episode_key] = edition_id

                    for evidence_id in episode.asset_evidence_ids:
                        evidence_row = conn.execute(
                            "SELECT * FROM source_evidence WHERE evidence_id = ?",
                            (evidence_id,),
                        ).fetchone()
                        if evidence_row is None:
                            raise ValueError(f"缺少 Asset 来源事实: {evidence_id}")
                        asset_row = conn.execute(
                            "SELECT asset_id FROM assets WHERE evidence_id = ?",
                            (evidence_id,),
                        ).fetchone()
                        if asset_row is None:
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
                                    evidence_row["root_id"],
                                    evidence_row["source_locator"],
                                    evidence_row["playback_locator"],
                                    evidence_row["fingerprint"],
                                    evidence_row["size"],
                                    evidence_row["mtime"],
                                    evidence_row["presence_state"],
                                ),
                            )
                        else:
                            asset_id = str(asset_row["asset_id"])
                        asset_ids[(episode.episode_key, evidence_id)] = asset_id
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO episode_assets(episode_id, edition_id, asset_id)
                            VALUES (?, ?, ?)
                            """,
                            (episode_id, edition_id, asset_id),
                        )

                for evidence, _facts in entries:
                    episode = episode_by_evidence.get(evidence.evidence_id)
                    if episode is None:
                        continue
                    facts = facts_by_evidence[evidence.evidence_id]
                    conn.execute(
                        """
                        INSERT INTO revision_bindings(
                            revision_id, evidence_id, work_id, season_id, episode_id,
                            edition_id, asset_id, confidence, reasons_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            evidence.evidence_id,
                            work_ids[episode.work_key],
                            season_ids[(episode.work_key, episode.local_season_number, episode.season_kind)],
                            episode_ids[episode.episode_key],
                            edition_ids[episode.episode_key],
                            asset_ids[(episode.episode_key, evidence.evidence_id)],
                            facts.confidence,
                            json.dumps(facts.reasons, ensure_ascii=False),
                        ),
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
        return graph

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
                        (
                            str(uuid.uuid4()),
                            revision_id,
                            work_id,
                            f"materialize_mirror:{revision_id}:{work_id}",
                            now,
                            now,
                        ),
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
