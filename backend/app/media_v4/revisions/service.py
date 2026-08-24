"""V4 Revision 草稿、确认和 transactional outbox。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import asdict, replace
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


def _persist_work_candidates(
    conn,
    revision_id: str,
    work_ids: dict[str, str],
    graph: ResolvedMediaGraph,
    entries: list[tuple[SourceEvidence, ParsedFacts]],
    created_at: str,
) -> None:
    """P-001 7.4 阶段4：确认时按 revision 冻结 provider 候选身份。

    候选输入包括显式 TMDB hint 与已存在的 provider binding；搜索结果只提出
    provider identity，不修改本地季号/集号。同一 provider/media_type/provider_id
    在 provider_bindings 唯一约束下天然对应唯一 Work。
    """

    conn.execute(
        "DELETE FROM revision_work_candidates WHERE revision_id = ?", (revision_id,)
    )
    now = _now()
    work_by_key = {work.work_key: work for work in graph.works}
    for work_key, work_id in work_ids.items():
        work = work_by_key.get(work_key)
        related_entries = [
            (evidence, facts)
            for evidence, facts in entries
            if work is not None and evidence.evidence_id in work.source_evidence_ids
        ]
        hints: list[tuple[str, str]] = []
        for _evidence, facts in related_entries:
            if facts.tmdb_hint_id and facts.tmdb_hint_type:
                hint = (facts.tmdb_hint_type.casefold(), str(facts.tmdb_hint_id))
                if hint not in hints:
                    hints.append(hint)
        existing = conn.execute(
            "SELECT provider, media_type, provider_id FROM provider_bindings WHERE work_id = ?",
            (work_id,),
        ).fetchall()
        for provider, media_type, provider_id in existing:
            status = "confirmed"
            conn.execute(
                """
                INSERT OR IGNORE INTO revision_work_candidates(
                    candidate_id, revision_id, work_id, draft_work_key, provider,
                    provider_id, media_type, title, year, evidence, confidence, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'high', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    work_id,
                    work_key,
                    provider,
                    provider_id,
                    media_type,
                    work.preferred_title if work else "",
                    work.year if work else None,
                    "existing_provider_binding",
                    status,
                    now,
                    now,
                ),
            )
        for provider, provider_id in hints:
            status = "confirmed" if any(
                p == provider and str(pid) == provider_id
                for p, _m, pid in existing
            ) else "proposed"
            conn.execute(
                """
                INSERT OR IGNORE INTO revision_work_candidates(
                    candidate_id, revision_id, work_id, draft_work_key, provider,
                    provider_id, media_type, title, year, evidence, confidence, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'high', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    work_id,
                    work_key,
                    "tmdb",
                    provider_id,
                    provider,
                    work.preferred_title if work else "",
                    work.year if work else None,
                    "parsed_tmdb_hint",
                    status,
                    now,
                    now,
                ),
            )


def _structural_key(relative_path: str) -> str:
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    directories = parts[:-1]
    generic = {
        "动画",
        "新番",
        "剧集",
        "电影",
        "动漫",
        "番剧",
        "影视",
        "动画电影",
        "已完结",
        "完结",
        "全部",
        "网盘",
        "115网盘",
        "百度网盘",
        "刮削好的动画",
        "media",
        "video",
        "tv",
        "anime",
        "movies",
        "series",
        "shows",
    }
    season_dir = re.compile(r"(?i)^(?:season\s*\d+|s\d+|第\s*\d+\s*季|specials?|sps?|s00)$")
    for part in reversed(directories):
        normalized = _normalize_title(part)
        if not normalized or normalized in generic or season_dir.fullmatch(normalized):
            continue
        return normalized
    return ""


class RevisionBlockedError(RuntimeError):
    """Revision 仍有未解决 review issue。"""


class V4RevisionService:
    """把一次解析结果固化为 revision，并以单事务发布执行任务。"""

    def __init__(self, database: V4Database):
        self.database = database
        self.repository = V4Repository(database)
        self.resolver = MediaResolver()

    _OVERRIDE_FIELDS = frozenset({
        "work_title",
        "original_title",
        "series_group",
        "title_candidates",
        "year_candidate",
        "media_type",
        "group_type",
        "season_candidate",
        "episode_candidate",
        "absolute_episode_candidate",
        "special_candidate",
        "special_number",
        "tmdb_hint_id",
        "tmdb_hint_type",
        "edition_tags",
        "needs_review",
        "is_importable",
        "is_auxiliary",
    })

    def create_draft(
        self,
        revision_id: str,
        entries: list[tuple[SourceEvidence, ParsedFacts]],
        *,
        resolver_version: str = "v4-resolver-1",
        root_id: str = "",
        scan_id: str = "",
        source_provider: str = "local",
        ingest_method: str = "local_scan",
        source_metadata: dict[str, str] | None = None,
        _publish: bool = False,
        _override_payloads: dict[str, dict] | None = None,
    ) -> ResolvedMediaGraph:
        if entries and any(evidence.root_id != entries[0][0].root_id for evidence, _ in entries):
            raise ValueError("同一 revision 不能混合多个来源根")
        if entries:
            root_id = entries[0][0].root_id
            scan_id = entries[0][0].scan_id
            source_provider = entries[0][0].provider
            ingest_method = entries[0][0].ingest_method
        if not root_id or not scan_id:
            raise ValueError("空 revision 必须明确提供 root_id 和 scan_id")

        graph = self.resolver.resolve(entries)
        override_payloads = _override_payloads or {}
        source_metadata = source_metadata or {}
        created_at = _now()

        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_roots(
                    root_id, provider, ingest_method, source_locator, playback_locator,
                    route_id, display_name, root_container, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_id) DO UPDATE SET
                    provider = excluded.provider,
                    ingest_method = excluded.ingest_method,
                    source_locator = CASE
                        WHEN excluded.source_locator != '' THEN excluded.source_locator
                        ELSE source_roots.source_locator
                    END,
                    playback_locator = CASE
                        WHEN excluded.playback_locator != '' THEN excluded.playback_locator
                        ELSE source_roots.playback_locator
                    END,
                    route_id = CASE
                        WHEN excluded.route_id != '' THEN excluded.route_id
                        ELSE source_roots.route_id
                    END,
                    display_name = CASE
                        WHEN excluded.display_name != '' THEN excluded.display_name
                        ELSE source_roots.display_name
                    END,
                    root_container = CASE
                        WHEN excluded.root_container != '' THEN excluded.root_container
                        ELSE source_roots.root_container
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    root_id,
                    source_provider,
                    ingest_method,
                    str(source_metadata.get("source_locator") or ""),
                    str(source_metadata.get("playback_locator") or ""),
                    str(source_metadata.get("route_id") or ""),
                    str(source_metadata.get("display_name") or ""),
                    str(source_metadata.get("root_container") or ""),
                    created_at,
                    created_at,
                ),
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
        if not _publish:
            for evidence, facts in entries:
                self.repository.save_source_evidence(evidence)
                self.repository.save_parsed_facts(facts)

        graph_digest = hashlib.sha256(
            json.dumps(
                asdict(graph),
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        facts_by_evidence = {evidence.evidence_id: facts for evidence, facts in entries}
        episodes_by_evidence: dict[str, list] = {}
        for episode in graph.episodes:
            for evidence_id in episode.asset_evidence_ids:
                episodes_by_evidence.setdefault(evidence_id, []).append(episode)
        work_assets_by_evidence: dict[str, list] = {}
        for work_asset in graph.work_assets:
            for evidence_id in work_asset.asset_evidence_ids:
                work_assets_by_evidence.setdefault(evidence_id, []).append(work_asset)

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
                for evidence, facts in entries:
                    conn.execute(
                        """
                        INSERT INTO revision_evidence(revision_id, evidence_id, parsed_fact_id)
                        VALUES (?, ?, ?)
                        """,
                        (revision_id, evidence.evidence_id, facts.parsed_fact_id),
                    )
                for evidence_id, payload in override_payloads.items():
                    conn.execute(
                        """
                        INSERT INTO revision_overrides(
                            revision_id, evidence_id, overrides_json, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            evidence_id,
                            json.dumps(payload, ensure_ascii=False),
                            created_at,
                        ),
                    )

                if not _publish:
                    for index, issue in enumerate(graph.issues):
                        conn.execute(
                            """
                            INSERT INTO revision_issues(
                                revision_id, issue_id, code, evidence_id, message
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                revision_id,
                                f"issue-{index}",
                                issue.code,
                                issue.evidence_id,
                                issue.message,
                            ),
                        )
                    conn.commit()
                    return graph

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
                                year, show_type, card_type, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                work_id,
                                work.work_key,
                                work_type,
                                work.preferred_title,
                                work.year,
                                work.show_type,
                                work.card_type,
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
                            "UPDATE works SET preferred_title = ?, year = ?, show_type = ?, "
                            "card_type = ?, updated_at = ? WHERE work_id = ?",
                            (work.preferred_title, work.year, work.show_type, work.card_type, created_at, work_id),
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

                # P-001 7.4 阶段3.2：持久化作品关系（外传/独立关联作品 → 父系列）。
                for relation in graph.relations:
                    parent_work_id = work_ids.get(relation.parent_work_key)
                    child_work_id = work_ids.get(relation.child_work_key)
                    if not parent_work_id or not child_work_id:
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO work_relations(
                            relation_id, parent_work_id, child_work_id, relation_type
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            parent_work_id,
                            child_work_id,
                            relation.relation_type,
                        ),
                    )

                # P-001 7.4 阶段4.1：确认时冻结候选身份，供人工恢复与跨语言合卡。
                _persist_work_candidates(conn, revision_id, work_ids, graph, entries, created_at)

                # Provider identity is a mapping, never a replacement for the
                # local Work identity.  Hints observed during parsing are
                # attached here without changing local season/episode numbers.
                for evidence, facts in entries:
                    if not facts.tmdb_hint_id or not facts.tmdb_hint_type:
                        continue
                    episodes = episodes_by_evidence.get(evidence.evidence_id, [])
                    episode_candidate = episodes[0] if episodes else None
                    work_key = episode_candidate.work_key if episode_candidate is not None else next(
                        (work.work_key for work in graph.works if evidence.evidence_id in work.source_evidence_ids),
                        "",
                    )
                    if not work_key:
                        continue
                    work_id = work_ids[work_key]
                    provider_id = str(facts.tmdb_hint_id)
                    identity_owner = conn.execute(
                        """
                        SELECT work_id FROM provider_bindings
                        WHERE provider = 'tmdb' AND media_type = ? AND provider_id = ?
                        """,
                        (facts.tmdb_hint_type, provider_id),
                    ).fetchone()
                    work_binding = conn.execute(
                        """
                        SELECT provider_id FROM provider_bindings
                        WHERE work_id = ? AND provider = 'tmdb' AND media_type = ?
                        """,
                        (work_id, facts.tmdb_hint_type),
                    ).fetchone()
                    if identity_owner is not None and identity_owner["work_id"] != work_id:
                        raise RevisionBlockedError("TMDB 身份已经属于另一个作品，拒绝静默合并")
                    if work_binding is not None and work_binding["provider_id"] != provider_id:
                        raise RevisionBlockedError("作品已经绑定另一个 TMDB 身份，拒绝静默覆盖")
                    conn.execute(
                        """
                        INSERT INTO provider_bindings(
                            work_id, provider, media_type, provider_id
                        ) VALUES (?, 'tmdb', ?, ?)
                        ON CONFLICT(work_id, provider, media_type) DO NOTHING
                        """,
                        (
                            work_id,
                            facts.tmdb_hint_type,
                            provider_id,
                        ),
                    )

                episode_ids: dict[str, str] = {}
                season_ids: dict[tuple[str, int | None, str], str] = {}
                edition_ids: dict[tuple[str, str], str | None] = {}
                asset_ids: dict[tuple[str, str], str] = {}

                def ensure_asset(evidence_id: str) -> str:
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
                    if asset_row is not None:
                        return str(asset_row["asset_id"])
                    facts = facts_by_evidence[evidence_id]
                    asset_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO assets(
                            asset_id, evidence_id, root_id, source_locator,
                            playback_locator, fingerprint, size, mtime,
                            resolution, release_group, version_tags_json,
                            availability_state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            facts.quality_tags[0] if facts.quality_tags else "",
                            facts.release_group,
                            json.dumps(facts.quality_tags, ensure_ascii=False),
                            "available" if evidence_row["presence_state"] == "present" else "missing",
                        ),
                    )
                    return asset_id

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
                          AND episode_kind = ?
                        """,
                        (
                            work_id,
                            season_id,
                            episode.local_episode_number,
                            episode.special_number,
                            episode.episode_kind,
                        ),
                    ).fetchone()
                    if episode_row is None:
                        episode_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO episodes(
                                episode_id, work_id, season_id, local_episode_number,
                                absolute_episode_number, special_number, episode_kind,
                                display_title
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
                                "",
                            ),
                        )
                    else:
                        episode_id = str(episode_row["episode_id"])
                    episode_ids[episode.episode_key] = episode_id

                    edition_id = None
                    if episode.edition_key != "default":
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
                    edition_ids[(episode.episode_key, episode.edition_key)] = edition_id

                    for evidence_id in episode.asset_evidence_ids:
                        asset_id = ensure_asset(evidence_id)
                        asset_ids[(episode.episode_key, evidence_id)] = asset_id
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO episode_assets(episode_id, edition_id, asset_id)
                            VALUES (?, ?, ?)
                            """,
                            (episode_id, edition_id, asset_id),
                        )

                work_edition_ids: dict[tuple[str, str], str | None] = {}
                work_asset_ids: dict[tuple[str, str, str], str] = {}
                for work_asset in graph.work_assets:
                    work_id = work_ids[work_asset.work_key]
                    edition_id = None
                    if work_asset.edition_key != "default":
                        edition_row = conn.execute(
                            "SELECT edition_id FROM editions WHERE work_id = ? AND edition_key = ?",
                            (work_id, work_asset.edition_key),
                        ).fetchone()
                        if edition_row is None:
                            edition_id = str(uuid.uuid4())
                            conn.execute(
                                """
                                INSERT INTO editions(edition_id, work_id, edition_key, display_name)
                                VALUES (?, ?, ?, ?)
                                """,
                                (edition_id, work_id, work_asset.edition_key, work_asset.edition_key),
                            )
                        else:
                            edition_id = str(edition_row["edition_id"])
                    work_edition_ids[(work_asset.work_key, work_asset.edition_key)] = edition_id
                    for evidence_id in work_asset.asset_evidence_ids:
                        asset_id = ensure_asset(evidence_id)
                        work_asset_ids[(work_asset.work_key, work_asset.edition_key, evidence_id)] = asset_id
                        conn.execute(
                            "INSERT OR IGNORE INTO work_assets(work_id, edition_id, asset_id) VALUES (?, ?, ?)",
                            (work_id, edition_id, asset_id),
                        )

                for evidence, _facts in entries:
                    bound_episodes = episodes_by_evidence.get(evidence.evidence_id, [])
                    facts = facts_by_evidence[evidence.evidence_id]
                    for episode in bound_episodes:
                        conn.execute(
                            """
                            INSERT INTO revision_bindings(
                                binding_id, revision_id, evidence_id, work_id, season_id,
                                episode_id, edition_id, asset_id, confidence, decision_source,
                                reasons_json, override_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                revision_id,
                                evidence.evidence_id,
                                work_ids[episode.work_key],
                                season_ids[(episode.work_key, episode.local_season_number, episode.season_kind)],
                                episode_ids[episode.episode_key],
                                edition_ids[(episode.episode_key, episode.edition_key)],
                                asset_ids[(episode.episode_key, evidence.evidence_id)],
                                facts.confidence,
                                "manual_override" if evidence.evidence_id in override_payloads else "resolver",
                                json.dumps(facts.reasons, ensure_ascii=False),
                                json.dumps(override_payloads.get(evidence.evidence_id, {}), ensure_ascii=False),
                            ),
                        )
                    for work_asset in work_assets_by_evidence.get(evidence.evidence_id, []):
                        conn.execute(
                            """
                            INSERT INTO revision_bindings(
                                binding_id, revision_id, evidence_id, work_id,
                                edition_id, asset_id, confidence, decision_source,
                                reasons_json, override_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                revision_id,
                                evidence.evidence_id,
                                work_ids[work_asset.work_key],
                                work_edition_ids[(work_asset.work_key, work_asset.edition_key)],
                                work_asset_ids[(work_asset.work_key, work_asset.edition_key, evidence.evidence_id)],
                                facts.confidence,
                                "manual_override" if evidence.evidence_id in override_payloads else "resolver",
                                json.dumps(facts.reasons, ensure_ascii=False),
                                json.dumps(override_payloads.get(evidence.evidence_id, {}), ensure_ascii=False),
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
                conn.execute(
                    """
                    UPDATE import_revisions
                    SET status = 'superseded'
                    WHERE root_id = ? AND revision_id != ? AND status = 'confirmed'
                    """,
                    (root_id, revision_id),
                )
                conn.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', updated_at = ?
                    WHERE status = 'queued' AND revision_id IN (
                        SELECT revision_id FROM import_revisions
                        WHERE root_id = ? AND status = 'superseded'
                    )
                    """,
                    (created_at, root_id),
                )
                conn.execute(
                    "UPDATE import_revisions SET status = 'confirmed', confirmed_at = ? WHERE revision_id = ?",
                    (created_at, revision_id),
                )
                self._enqueue_execution_jobs(conn, revision_id, set(work_ids.values()), created_at)
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

    def apply_override(
        self,
        revision_id: str,
        evidence_id: str,
        changes: dict,
    ) -> ResolvedMediaGraph:
        unknown = set(changes) - self._OVERRIDE_FIELDS
        if unknown:
            raise ValueError("不允许修正字段: " + ", ".join(sorted(unknown)))
        if not changes:
            raise ValueError("人工修正不能为空")
        normalized = self._normalize_override(changes)
        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT status, root_id, scan_id FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            if revision["status"] != "draft":
                raise RuntimeError("只有 draft revision 可以人工修正")
            member = conn.execute(
                "SELECT 1 FROM revision_evidence WHERE revision_id = ? AND evidence_id = ?",
                (revision_id, evidence_id),
            ).fetchone()
            if member is None:
                raise KeyError(evidence_id)
            existing = conn.execute(
                "SELECT overrides_json FROM revision_overrides WHERE revision_id = ? AND evidence_id = ?",
                (revision_id, evidence_id),
            ).fetchone()
            merged = json.loads(existing["overrides_json"] or "{}") if existing is not None else {}
            merged.update(normalized)
            conn.execute(
                """
                INSERT INTO revision_overrides(
                    revision_id, evidence_id, overrides_json, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(revision_id, evidence_id) DO UPDATE SET
                    overrides_json = excluded.overrides_json
                """,
                (revision_id, evidence_id, json.dumps(merged, ensure_ascii=False), _now()),
            )
        entries = self._load_revision_entries(revision_id)
        graph = self.resolver.resolve(entries)
        with self.database.connect() as conn:
            conn.execute("DELETE FROM revision_issues WHERE revision_id = ?", (revision_id,))
            for index, issue in enumerate(graph.issues):
                conn.execute(
                    """
                    INSERT INTO revision_issues(revision_id, issue_id, code, evidence_id, message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (revision_id, f"issue-{index}", issue.code, issue.evidence_id, issue.message),
                )
        return graph

    @staticmethod
    def _normalize_override(changes: dict) -> dict:
        normalized = dict(changes)
        text_fields = {
            "work_title",
            "original_title",
            "series_group",
            "media_type",
            "group_type",
            "tmdb_hint_type",
        }
        for key in text_fields & normalized.keys():
            if not isinstance(normalized[key], str):
                raise ValueError(f"人工修正字段 {key} 必须是字符串")
            normalized[key] = normalized[key].strip()
        if normalized.get("media_type") not in {None, "", "tv", "movie"}:
            raise ValueError("media_type 只能是 tv 或 movie")
        if normalized.get("tmdb_hint_type") not in {None, "", "tv", "movie"}:
            raise ValueError("tmdb_hint_type 只能是 tv 或 movie")
        if normalized.get("group_type") not in {
            None,
            "",
            "season",
            "special",
            "movie",
            "unknown",
        }:
            raise ValueError("group_type 不是允许的媒体分组")
        for key in ("title_candidates", "edition_tags"):
            if key not in normalized:
                continue
            value = normalized[key]
            if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"人工修正字段 {key} 必须是字符串数组")
            normalized[key] = [item.strip() for item in value if item.strip()]
        numeric_fields = {
            "year_candidate",
            "season_candidate",
            "episode_candidate",
            "absolute_episode_candidate",
            "special_number",
            "tmdb_hint_id",
        }
        for key in numeric_fields & normalized.keys():
            value = normalized[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"人工修正字段 {key} 必须是整数或 null")
            if value is not None and value < 0:
                raise ValueError(f"人工修正字段 {key} 不能为负数")
        year = normalized.get("year_candidate")
        if year is not None and not 1800 <= year <= 2200:
            raise ValueError("year_candidate 超出允许范围")
        tmdb_id = normalized.get("tmdb_hint_id")
        if tmdb_id is not None and tmdb_id <= 0:
            raise ValueError("tmdb_hint_id 必须大于 0")
        for key in ("special_candidate", "needs_review", "is_importable", "is_auxiliary"):
            if key in normalized and not isinstance(normalized[key], bool):
                raise ValueError(f"人工修正字段 {key} 必须是布尔值")
        return normalized

    def _load_revision_entries(self, revision_id: str) -> list[tuple[SourceEvidence, ParsedFacts]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT re.evidence_id, re.parsed_fact_id,
                       COALESCE(ro.overrides_json, '{}') AS overrides_json
                FROM revision_evidence re
                LEFT JOIN revision_overrides ro
                  ON ro.revision_id = re.revision_id AND ro.evidence_id = re.evidence_id
                WHERE re.revision_id = ?
                ORDER BY re.evidence_id
                """,
                (revision_id,),
            ).fetchall()
        entries = []
        tuple_fields = {"title_candidates", "edition_tags"}
        for row in rows:
            evidence = self.repository.get_source_evidence(row["evidence_id"])
            facts = self.repository.get_parsed_facts(row["parsed_fact_id"])
            overrides = json.loads(row["overrides_json"] or "{}")
            for key in tuple_fields:
                if key in overrides:
                    overrides[key] = tuple(overrides[key] or ())
            entries.append((evidence, replace(facts, **overrides)))
        return entries

    def _load_override_payloads(self, revision_id: str) -> dict[str, dict]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT evidence_id, overrides_json FROM revision_overrides WHERE revision_id = ?",
                (revision_id,),
            ).fetchall()
        return {
            str(row["evidence_id"]): json.loads(row["overrides_json"] or "{}")
            for row in rows
        }

    def confirm(self, revision_id: str) -> None:
        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT status, root_id, scan_id FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            if revision["status"] == "confirmed":
                return
            if revision["status"] != "draft":
                raise RuntimeError(f"revision 状态不可确认: {revision['status']}")
            unresolved = conn.execute(
                "SELECT 1 FROM revision_issues WHERE revision_id = ? AND resolved = 0 LIMIT 1",
                (revision_id,),
            ).fetchone()
            if unresolved is not None:
                raise RevisionBlockedError("revision 仍有 review issue，不能确认")
        entries = self._load_revision_entries(revision_id)
        override_payloads = self._load_override_payloads(revision_id)
        self.create_draft(
            revision_id,
            entries,
            root_id=str(revision["root_id"]),
            scan_id=str(revision["scan_id"]),
            _publish=True,
            _override_payloads=override_payloads,
        )

    @staticmethod
    def _enqueue_execution_jobs(conn, revision_id: str, work_ids: set[str], now: str) -> None:
        for work_id in sorted(work_ids):
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
                    job_id, job_type, revision_id, work_id, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, 'scrape_work', ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    work_id,
                    f"scrape_work:{revision_id}:{work_id}",
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
        root_id = conn.execute(
            "SELECT root_id FROM import_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()[0]
        has_superseded = conn.execute(
            "SELECT 1 FROM import_revisions WHERE root_id = ? AND status = 'superseded' LIMIT 1",
            (root_id,),
        ).fetchone()
        if has_superseded is not None:
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_id, job_type, revision_id, idempotency_key, created_at, updated_at
                ) VALUES (?, 'cleanup_superseded_artifacts', ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    f"cleanup_superseded_artifacts:{revision_id}",
                    now,
                    now,
                ),
            )

    def list_jobs(self, revision_id: str) -> list[dict]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE revision_id = ? ORDER BY job_type, job_id",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]
