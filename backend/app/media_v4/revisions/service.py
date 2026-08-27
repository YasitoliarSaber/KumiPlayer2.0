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

from app.media_v4.domain.models import (
    ParsedFacts,
    ResolutionIssue,
    ResolvedMediaGraph,
    SourceEvidence,
)
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.resolution import candidates as candidate_service
from app.media_v4.resolution.candidates import CandidateSearch
from app.media_v4.resolution.resolver import MediaResolver


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _job_summary(job: dict | None) -> dict:
    """把单个 job 折叠成作品单元可用的最小投影；无 job 时返回等待态。"""

    if job is None:
        return {"job_id": "", "status": "queued", "attempts": 0, "last_error": ""}
    return {
        "job_id": str(job["job_id"] or ""),
        "status": str(job["status"] or "queued"),
        "attempts": int(job["attempts"] or 0),
        "last_error": str(job["last_error"] or ""),
    }


def _derive_work_status(mirror: dict | None, metadata: dict | None, scrape_status: str) -> str:
    """由 mirror + metadata jobs 推导作品单元的用户可见状态。

    优先级与文档一致：运行中 > 需要处理/失败/取消 > 等待 > 完成；
    失败/取消不能被后续 queued job 掩盖。scrape 元数据状态（waiting_review /
    source_unavailable / failed / waiting_metadata）不能显示为已完成。
    """

    def first(*jobs: dict | None) -> dict | None:
        return next((job for job in jobs if job is not None), None)

    mirror_job = first(mirror)
    metadata_job = first(metadata)
    if mirror_job is not None:
        if mirror_job["status"] == "running":
            return "running_mirror"
        if mirror_job["status"] == "failed":
            return "failed"
        if mirror_job["status"] == "cancelled":
            return "cancelled"
        if mirror_job["status"] == "queued":
            return "waiting_mirror"
        # mirror succeeded
        if metadata_job is not None:
            if metadata_job["status"] == "running":
                return "running_metadata"
            if metadata_job["status"] == "failed":
                return "failed"
            if metadata_job["status"] == "cancelled":
                return "cancelled"
            if metadata_job["status"] == "queued":
                return "waiting_metadata"
            # metadata succeeded
            if scrape_status and scrape_status != "confirmed":
                return "needs_attention"
            return "completed"
        return "waiting_metadata"
    # 理论上每个 work 都有 mirror job；异常情况按 metadata 状态展示。
    if metadata_job is not None:
        if metadata_job["status"] == "running":
            return "running_metadata"
        if metadata_job["status"] == "failed":
            return "failed"
        if metadata_job["status"] == "cancelled":
            return "cancelled"
        if metadata_job["status"] == "queued":
            return "waiting_metadata"
        if scrape_status and scrape_status != "confirmed":
            return "needs_attention"
        return "completed"
    return "waiting_mirror"


def _stage_status(jobs: list[dict]) -> str:
    """阶段级状态：running > failed/cancelled > queued > succeeded；空为 idle。"""

    if not jobs:
        return "idle"
    statuses = [str(job["status"]) for job in jobs]
    if "running" in statuses:
        return "running"
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if "queued" in statuses:
        return "queued"
    return "succeeded"


def _stage_summary(jobs: list[dict]) -> dict:
    return {
        "status": _stage_status(jobs),
        "total": len(jobs),
        "queued": sum(1 for job in jobs if job["status"] == "queued"),
        "running": sum(1 for job in jobs if job["status"] == "running"),
        "succeeded": sum(1 for job in jobs if job["status"] == "succeeded"),
        "failed": sum(1 for job in jobs if job["status"] == "failed"),
        "cancelled": sum(1 for job in jobs if job["status"] == "cancelled"),
    }


def _revision_overall_status(work_units: list[dict], stage: dict[str, list[dict]]) -> str:
    """revision 级整体状态：running > needs_attention > queued > completed。"""

    if any(unit["overall_status"] in {"running_mirror", "running_metadata"} for unit in work_units):
        return "running"
    if any(stage["projection"] and str(job["status"]) == "running" for job in stage["projection"]):
        return "running"
    if any(unit["overall_status"] in {"failed", "cancelled", "needs_attention"} for unit in work_units):
        return "needs_attention"
    if any(
        str(job["status"]) in {"failed", "cancelled"}
        for stage_jobs in stage.values()
        for job in stage_jobs
    ):
        return "needs_attention"
    if any(
        str(job["status"]) in {"queued"}
        for stage_jobs in stage.values()
        for job in stage_jobs
    ):
        return "queued"
    if any(unit["overall_status"] == "waiting_mirror" or unit["overall_status"] == "waiting_metadata" for unit in work_units):
        return "queued"
    return "completed"



def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip(" ._-·:：/\\()（）【】[]{}<>《》「」『』\"'")


def _merge_map_from_candidates(candidates_by_key: dict[str, list]) -> dict[str, str]:
    """从已确认候选重建合并映射（同一 provider identity 的 draft Work 合并）。"""

    identity_owner: dict[tuple[str, str, str], str] = {}
    merge_map: dict[str, str] = {}
    for work_key, items in candidates_by_key.items():
        for item in items:
            if item.status != "confirmed":
                continue
            identity = (item.provider, item.media_type, item.provider_id)
            owner = identity_owner.setdefault(identity, work_key)
            if owner != work_key:
                merge_map[work_key] = owner
    return merge_map


def _persist_candidates(
    conn,
    revision_id: str,
    work_ids: dict[str, str],
    candidates_by_key: dict[str, list],
    created_at: str,
) -> None:
    """按 revision_id + draft_work_key 持久化候选（P-001 7.7 R1）。"""

    conn.execute(
        "DELETE FROM revision_work_candidates WHERE revision_id = ?", (revision_id,)
    )
    now = _now()
    for work_key, items in candidates_by_key.items():
        work_id = work_ids.get(work_key, "")
        for item in items:
            conn.execute(
                """
                INSERT OR IGNORE INTO revision_work_candidates(
                    candidate_id, revision_id, work_id, draft_work_key, provider,
                    provider_id, media_type, title, original_title, aliases_json, year,
                    evidence, confidence, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    work_id,
                    work_key,
                    item.provider,
                    item.provider_id,
                    item.media_type,
                    item.title,
                    item.original_title,
                    json.dumps(list(item.aliases), ensure_ascii=False),
                    item.year,
                    item.evidence,
                    item.confidence,
                    item.status,
                    now,
                    now,
                ),
            )


def _freeze_candidate_bindings(
    conn,
    work_ids: dict[str, str],
    candidates_by_key: dict[str, list],
    created_at: str,
) -> None:
    """确认时把高置信唯一身份写入 provider_bindings，冻结身份供 scrape 消费。"""

    for work_key, work_id in work_ids.items():
        confirmed = [
            item for item in candidates_by_key.get(work_key, [])
            if item.status == "confirmed" and candidate_service.supported_provider(item.provider)
        ]
        unique_identities = {(item.provider, item.media_type, item.provider_id) for item in confirmed}
        if len(unique_identities) != 1:
            continue
        chosen = confirmed[0]
        conn.execute(
            """
            INSERT INTO provider_bindings(work_id, provider, media_type, provider_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_id, provider, media_type) DO UPDATE SET provider_id = excluded.provider_id
            """,
            (work_id, chosen.provider, chosen.media_type, chosen.provider_id),
        )


def _structural_key(relative_path: str) -> str:
    from app.media_v4.generic_container import is_generic_container_name

    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    directories = parts[:-1]
    for part in reversed(directories):
        normalized = _normalize_title(part)
        if not normalized or is_generic_container_name(normalized):
            continue
        return normalized
    return ""


def _lookup_work_by_key(conn, work_key: str, *, exclude_work_id: str = "") -> str:
    row = conn.execute(
        "SELECT work_id FROM works WHERE identity_key = ?", (work_key,)
    ).fetchone()
    if row:
        return str(row["work_id"])
    # 显式合集产生 series:<title>:<type> 身份；同一父作品若在较早 revision
    # 以普通 title 身份确认，关系仍应通过已持久化别名确定性复用。只接受唯一
    # 命中，避免同名作品被静默串联。
    if not work_key.startswith("series:"):
        return ""
    try:
        normalized_title, media_type = work_key[len("series:"):].rsplit(":", 1)
    except ValueError:
        return ""
    work_type = "movie" if media_type == "movie" else "series" if media_type == "tv" else ""
    if not normalized_title or not work_type:
        return ""
    rows = conn.execute(
        """
        SELECT DISTINCT works.work_id
        FROM works
        JOIN work_aliases ON work_aliases.work_id = works.work_id
        WHERE work_aliases.normalized_title = ? AND works.work_type = ?
          AND (? = '' OR works.work_id != ?)
        """,
        (normalized_title, work_type, exclude_work_id, exclude_work_id),
    ).fetchall()
    return str(rows[0]["work_id"]) if len(rows) == 1 else ""


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

    def _load_draft_candidates(self, revision_id: str) -> dict[str, list]:
        """确认时复用 draft 已冻结候选，避免再次联网搜索。"""

        from app.media_v4.resolution.candidates import WorkCandidate

        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM revision_work_candidates WHERE revision_id = ?",
                (revision_id,),
            ).fetchall()
        result: dict[str, list] = {}
        for row in rows:
            key = str(row["draft_work_key"])
            result.setdefault(key, []).append(WorkCandidate(
                work_key=key,
                provider=str(row["provider"]),
                provider_id=str(row["provider_id"]),
                media_type=str(row["media_type"]),
                title=str(row["title"]),
                original_title=str(row["original_title"] or ""),
                aliases=tuple(
                    str(alias) for alias in json.loads(row["aliases_json"] or "[]")
                ),
                year=row["year"],
                evidence=str(row["evidence"]),
                confidence=str(row["confidence"]),
                status=str(row["status"]),
            ))
        return result

    def _existing_bindings_by_key(self, graph: ResolvedMediaGraph) -> dict[str, list[tuple[str, str, str]]]:
        """按 identity_key 读取既有 provider binding，供跨 revision 候选复用。"""

        bindings: dict[str, list[tuple[str, str, str]]] = {}
        with self.database.connect() as conn:
            for work in graph.works:
                rows = conn.execute(
                    """
                    SELECT pb.provider, pb.media_type, pb.provider_id
                    FROM works w JOIN provider_bindings pb ON pb.work_id = w.work_id
                    WHERE w.identity_key = ?
                    """,
                    (work.work_key,),
                ).fetchall()
                bindings[work.work_key] = [(str(r[0]), str(r[1]), str(r[2])) for r in rows]
        return bindings

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
        source_mode: str = "",
        _publish: bool = False,
        _evidence_already_persisted: bool = False,
        _override_payloads: dict[str, dict] | None = None,
        candidate_search: CandidateSearch | None = None,
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
        # P-002 8.4：来源根级模式是独立权威字段，不再从排序后的第一条文件证据
        # 反推。显式 source_mode 存在时，root 级 ingest_method 只保留为兼容遗留
        # 列并按其稳定映射；文件级 SourceEvidence.ingest_method 仍表达观测方式。
        if source_mode:
            ingest_method = {
                "local": "local_scan",
                "tree_snapshot": "directory_tree",
                "tree_openlist": "directory_tree",
                "openlist_full": "openlist_scan",
            }.get(source_mode, ingest_method)

        graph = self.resolver.resolve(entries)
        # P-001 7.7 R1：确认前候选解析。draft 时在线/测试搜索并合并同一
        # provider identity；确认（publish）时复用已冻结候选，不再重新搜索。
        candidates_by_key: dict[str, list] = {}
        if _publish:
            candidates_by_key = self._load_draft_candidates(revision_id)
            graph = candidate_service.merge_graph(
                graph,
                _merge_map_from_candidates(candidates_by_key),
            )
        else:
            if candidate_search is not None:
                search = candidate_search
            else:
                # 同一 draft 内共享详情缓存与预算，避免逐查询放大 API。
                detail_cache: dict = {}
                detail_budget: list[int] = [0]

                def search(work_key, queries, year, media_type):
                    return candidate_service.default_candidate_search(
                        work_key,
                        queries,
                        year,
                        media_type,
                        detail_cache=detail_cache,
                        detail_budget=detail_budget,
                    )

            existing_bindings = self._existing_bindings_by_key(graph)
            candidates_by_key, merge_map, candidate_issues = candidate_service.plan_work_candidates(
                graph,
                entries,
                search,
                existing_bindings=existing_bindings,
            )
            graph = candidate_service.merge_graph(graph, merge_map)
            if candidate_issues:
                graph = replace(graph, issues=(*graph.issues, *candidate_issues))
        override_payloads = _override_payloads or {}
        source_metadata = source_metadata or {}
        created_at = _now()

        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_roots(
                    root_id, provider, ingest_method, source_locator, playback_locator,
                    route_id, display_name, root_container, source_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    source_mode = CASE
                        WHEN excluded.source_mode != '' THEN excluded.source_mode
                        ELSE source_roots.source_mode
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
                    source_mode,
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
                if not _evidence_already_persisted:
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
                    _persist_candidates(conn, revision_id, {}, candidates_by_key, created_at)
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
                        # P-001 7.8 R6：确认事务按冻结候选 identity 复用已有 Work
                        # （provider+media_type+provider_id 在数据库层唯一归属一个 Work）。
                        frozen = [
                            item for item in candidates_by_key.get(work.work_key, [])
                            if item.status == "confirmed"
                            and candidate_service.supported_provider(item.provider)
                        ]
                        unique_frozen = {
                            (item.provider, item.media_type, item.provider_id) for item in frozen
                        }
                        if len(unique_frozen) == 1:
                            chosen = frozen[0]
                            owner_row = conn.execute(
                                """
                                SELECT w.work_id FROM provider_bindings pb
                                JOIN works w ON w.work_id = pb.work_id
                                WHERE pb.provider = ? AND pb.media_type = ? AND pb.provider_id = ?
                                LIMIT 1
                                """,
                                (chosen.provider, chosen.media_type, chosen.provider_id),
                            ).fetchone()
                            if owner_row is not None:
                                existing_work = conn.execute(
                                    "SELECT * FROM works WHERE work_id = ?",
                                    (owner_row["work_id"],),
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
                            for value in (facts.work_title, *facts.title_candidates)
                            if _normalize_title(value)
                            and _normalize_title(value) != _normalize_title(facts.series_group)
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

                # P-001 7.7 R4：持久化作品关系；父 Work 可能只存在于已确认数据库。
                for relation in graph.relations:
                    parent_work_id = work_ids.get(relation.parent_work_key)
                    child_work_id = work_ids.get(relation.child_work_key)
                    if not parent_work_id:
                        parent_work_id = _lookup_work_by_key(
                            conn,
                            relation.parent_work_key,
                            exclude_work_id=child_work_id or "",
                        )
                    if not parent_work_id or not child_work_id:
                        # 父 Work 不存在时保存明确待解析记录，不静默丢弃。
                        conn.execute(
                            """
                            INSERT INTO revision_issues(
                                revision_id, issue_id, code, evidence_id, message
                            ) VALUES (?, ?, 'unresolved_parent_relation', ?, ?)
                            """,
                            (
                                revision_id,
                                f"relation-{uuid.uuid4().hex}",
                                child_work_id or "",
                                f"父系列 {relation.parent_work_key} 尚未导入，无法建立作品关系",
                            ),
                        )
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

                # P-001 7.7 R1：确认时冻结候选身份（work_id 落库），并把高置信
                # 唯一身份写入 provider_bindings，使确认后的 scrape 不再搜索。
                _persist_candidates(conn, revision_id, work_ids, candidates_by_key, created_at)
                _freeze_candidate_bindings(conn, work_ids, candidates_by_key, created_at)

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
                # 确认即代表该来源重新进入媒体库。来源卡必须在任务开始时就可见，
                # 不能等镜像、刮削和最终投影全部完成后才恢复。
                conn.execute(
                    """
                    UPDATE source_roots
                    SET retired_at = '', retired_reason = '', updated_at = ?
                    WHERE root_id = ?
                    """,
                    (created_at, root_id),
                )
                conn.execute(
                    """
                    INSERT INTO v4_meta(key, value) VALUES ('library_projection_dirty', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (created_at,),
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

    def load_draft_graph(self, revision_id: str) -> ResolvedMediaGraph:
        """从已持久化事实与冻结候选重建草稿，不再执行在线搜索。"""

        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            if revision["status"] != "draft":
                raise RuntimeError("只有 draft revision 可以读取识别预览")
            issue_rows = conn.execute(
                """
                SELECT code, evidence_id, message
                FROM revision_issues
                WHERE revision_id = ? AND resolved = 0
                ORDER BY issue_id
                """,
                (revision_id,),
            ).fetchall()

        entries = self._load_revision_entries(revision_id)
        graph = self.resolver.resolve(entries)
        candidates_by_key = self._load_draft_candidates(revision_id)
        graph = candidate_service.merge_graph(
            graph,
            _merge_map_from_candidates(candidates_by_key),
        )
        issues = tuple(
            ResolutionIssue(
                code=str(row["code"]),
                evidence_id=str(row["evidence_id"] or ""),
                message=str(row["message"]),
            )
            for row in issue_rows
        )
        return replace(graph, issues=issues)

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

    def get_execution_progress(self, revision_id: str) -> dict:
        """P-003：V4 只读执行进度投影。

        从 authoritative tables（import_revisions / jobs / works /
        revision_bindings / scrape_bindings）读取，禁止从前端 preview、日志
        文本或 Library Projection 反推运行状态。返回作品单元 + 用户阶段摘要，
        原始 job_id 只作为重试命令参数。
        """

        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT revision_id, status, confirmed_at FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            job_rows = conn.execute(
                "SELECT job_id, job_type, work_id, status, attempts, last_error "
                "FROM jobs WHERE revision_id = ? ORDER BY job_type, job_id",
                (revision_id,),
            ).fetchall()
            binding_rows = conn.execute(
                "SELECT work_id, COUNT(DISTINCT episode_id) AS episode_count, "
                "COUNT(DISTINCT asset_id) AS asset_count "
                "FROM revision_bindings WHERE revision_id = ? AND work_id != '' "
                "GROUP BY work_id",
                (revision_id,),
            ).fetchall()
            work_ids = sorted(
                {str(row["work_id"]) for row in job_rows if str(row["work_id"])}
                | {str(row["work_id"]) for row in binding_rows}
            )
            works: dict[str, dict] = {}
            if work_ids:
                placeholders = ",".join("?" for _ in work_ids)
                for row in conn.execute(
                    "SELECT work_id, preferred_title, work_type FROM works WHERE work_id IN (" + placeholders + ")",
                    work_ids,
                ).fetchall():
                    works[str(row["work_id"])] = dict(row)
            scrape_rows: dict[str, str] = {}
            for row in conn.execute(
                "SELECT sb.work_id, sb.status FROM scrape_bindings sb "
                "WHERE sb.revision_id = ? AND sb.updated_at = ( "
                "  SELECT MAX(updated_at) FROM scrape_bindings latest "
                "  WHERE latest.revision_id = sb.revision_id AND latest.work_id = sb.work_id "
                ")",
                (revision_id,),
            ).fetchall():
                scrape_rows[str(row["work_id"])] = str(row["status"] or "")

        jobs_by_work: dict[str, dict] = {}
        stage: dict[str, list[dict]] = {"mirror": [], "metadata": [], "projection": []}
        for row in job_rows:
            job = dict(row)
            job_type = str(job["job_type"])
            work_id = str(job["work_id"] or "")
            if job_type == "materialize_mirror":
                stage["mirror"].append(job)
                jobs_by_work.setdefault(work_id, {})["mirror"] = job
            elif job_type == "scrape_work":
                stage["metadata"].append(job)
                jobs_by_work.setdefault(work_id, {})["metadata"] = job
            elif job_type == "refresh_projection":
                stage["projection"].append(job)

        work_units: list[dict] = []
        for work_id in work_ids:
            work = works.get(work_id, {})
            job_pair = jobs_by_work.get(work_id, {})
            mirror = job_pair.get("mirror")
            metadata = job_pair.get("metadata")
            work_units.append({
                "work_id": work_id,
                "title": str(work.get("preferred_title") or work_id),
                "media_type": "movie" if str(work.get("work_type") or "") == "movie" else "tv",
                "episode_count": int(next(
                    (row["episode_count"] for row in binding_rows if row["work_id"] == work_id),
                    0,
                ) or 0),
                "asset_count": int(next(
                    (row["asset_count"] for row in binding_rows if row["work_id"] == work_id),
                    0,
                ) or 0),
                "overall_status": _derive_work_status(
                    mirror,
                    metadata,
                    scrape_rows.get(work_id, ""),
                ),
                "mirror": _job_summary(mirror),
                "metadata": _job_summary(metadata),
            })

        return {
            "revision_id": revision_id,
            "revision_status": str(revision["status"]),
            "overall_status": _revision_overall_status(work_units, stage),
            "stage_summary": {
                "mirror": _stage_summary(stage["mirror"]),
                "metadata": _stage_summary(stage["metadata"]),
                "projection": _stage_summary(stage["projection"]),
            },
            "work_units": work_units,
        }
