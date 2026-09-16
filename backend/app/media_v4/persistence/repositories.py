"""V4 不可变来源事实的 repository。"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.persistence.database import V4Database

#: 批量读写的单批 id 数（与既有 `save_*_bulk` 的 400 保持一致）：
#: 用 IN(...) 批量替代逐条 SELECT，避免 revision 级读取退化成 N+1。
_BULK_BATCH_SIZE = 400


class V4Repository:
    """只负责事实 round-trip，不在这里执行解析或身份推断。"""

    def __init__(self, database: V4Database):
        self.database = database

    @staticmethod
    def _row_to_source_evidence(row) -> SourceEvidence:
        return SourceEvidence(
            evidence_id=row["evidence_id"],
            scan_id=row["scan_id"],
            root_id=row["root_id"],
            provider=row["provider"],
            source_key=row["source_key"],
            relative_path=row["relative_path"],
            entry_kind=row["entry_kind"],
            size=row["size"],
            mtime=row["mtime"],
            fingerprint=row["fingerprint"],
            raw_file_id=row["raw_file_id"],
            ingest_method=row["ingest_method"],
            source_route_id=row["source_route_id"],
            source_locator=row["source_locator"],
            playback_locator=row["playback_locator"],
            tmdb_hint_id=row["tmdb_hint_id"],
            tmdb_hint_type=row["tmdb_hint_type"],
            import_family=row["import_family"],
            target_filename=row["target_filename"],
            observed_at=row["observed_at"],
            presence_state=row["presence_state"],
        )

    def save_source_evidence(self, evidence: SourceEvidence) -> None:
        self.save_scan_evidence_bulk([evidence])

    @staticmethod
    def _source_evidence_values(evidence: SourceEvidence) -> tuple:
        return (
            evidence.evidence_id,
            evidence.scan_id,
            evidence.root_id,
            evidence.provider,
            evidence.source_key,
            evidence.relative_path,
            evidence.entry_kind,
            evidence.size,
            evidence.mtime,
            evidence.fingerprint,
            evidence.raw_file_id,
            evidence.ingest_method,
            evidence.source_route_id,
            evidence.source_locator,
            evidence.playback_locator,
            evidence.tmdb_hint_id,
            evidence.tmdb_hint_type,
            evidence.import_family,
            evidence.target_filename,
            evidence.observed_at,
            evidence.presence_state,
        )

    def save_scan_evidence_bulk(self, evidence: list[SourceEvidence]) -> None:
        """同一次扫描的证据在单个事务内幂等写入（目录树权威来源）。"""

        if not evidence:
            return
        # SQLite 的两个唯一约束分别保护 evidence 身份和扫描内来源键。
        # 先在内存和同一连接中核对完整 payload，不能让 INSERT OR IGNORE
        # 把冲突静默吞掉；这样流式批次和最终兜底路径遵守同一合同。
        by_id: dict[str, tuple] = {}
        by_scan_key: dict[tuple[str, str], tuple] = {}
        unique_values: list[tuple] = []
        for item in evidence:
            values = self._source_evidence_values(item)
            previous = by_id.get(item.evidence_id)
            if previous is not None and previous != values:
                raise ValueError(f"不可变 SourceEvidence 冲突: {item.evidence_id}")
            scan_key = (item.scan_id, item.source_key)
            previous = by_scan_key.get(scan_key)
            if previous is not None and previous != values:
                raise ValueError(
                    f"不可变 SourceEvidence 冲突: {item.scan_id}/{item.source_key}"
                )
            if item.evidence_id not in by_id:
                unique_values.append(values)
            by_id[item.evidence_id] = values
            by_scan_key[scan_key] = values

        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_by_id: dict[str, tuple] = {}
            ids = list(by_id)
            for offset in range(0, len(ids), 400):
                chunk = ids[offset : offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM source_evidence WHERE evidence_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                existing_by_id.update(
                    {
                        str(row["evidence_id"]): self._source_evidence_values(
                            self._row_to_source_evidence(row)
                        )
                        for row in rows
                    }
                )

            existing_by_scan_key: dict[tuple[str, str], tuple] = {}
            scan_keys_by_id: dict[str, list[str]] = {}
            for scan_id, source_key in by_scan_key:
                scan_keys_by_id.setdefault(scan_id, []).append(source_key)
            for scan_id, source_keys in scan_keys_by_id.items():
                for offset in range(0, len(source_keys), 400):
                    chunk = source_keys[offset : offset + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        "SELECT * FROM source_evidence WHERE scan_id = ? "
                        f"AND source_key IN ({placeholders})",
                        [scan_id, *chunk],
                    ).fetchall()
                    existing_by_scan_key.update(
                        {
                            (str(row["scan_id"]), str(row["source_key"])): self._source_evidence_values(
                                self._row_to_source_evidence(row)
                            )
                            for row in rows
                        }
                    )

            scan_ids = sorted(scan_keys_by_id)
            scan_roots: dict[str, str] = {}
            for offset in range(0, len(scan_ids), 400):
                chunk = scan_ids[offset : offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT scan_id, root_id FROM source_scans WHERE scan_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                scan_roots.update({str(row["scan_id"]): str(row["root_id"]) for row in rows})

            for item in evidence:
                expected_root = scan_roots.get(item.scan_id)
                if expected_root is not None and expected_root != item.root_id:
                    raise ValueError(
                        "SourceEvidence root_id 与 source_scan 不一致: "
                        f"{item.scan_id}/{item.root_id}"
                    )

            for evidence_id, values in by_id.items():
                existing = existing_by_id.get(evidence_id)
                if existing is not None and existing != values:
                    raise ValueError(f"不可变 SourceEvidence 冲突: {evidence_id}")
            for scan_key, values in by_scan_key.items():
                existing = existing_by_scan_key.get(scan_key)
                if existing is not None and existing != values:
                    raise ValueError(
                        f"不可变 SourceEvidence 冲突: {scan_key[0]}/{scan_key[1]}"
                    )

            conn.executemany(
                """
                INSERT OR IGNORE INTO source_evidence(
                    evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind,
                    size, mtime, fingerprint, raw_file_id, ingest_method, source_route_id,
                    source_locator, playback_locator, tmdb_hint_id, tmdb_hint_type,
                    import_family, target_filename, observed_at, presence_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                unique_values,
            )
            conn.commit()

    def list_scan_evidence(self, scan_id: str) -> list[SourceEvidence]:
        """读取后端在某次扫描中持久化的全部证据（目录树权威来源）。"""

        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM source_evidence WHERE scan_id = ? ORDER BY source_key",
                (scan_id,),
            ).fetchall()
        return [self._row_to_source_evidence(row) for row in rows]

    def get_source_evidence(self, evidence_id: str, conn=None) -> SourceEvidence:
        with (nullcontext(conn) if conn is not None else self.database.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM source_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(evidence_id)
        return self._row_to_source_evidence(row)

    def get_source_evidence_bulk(self, evidence_ids, *, conn) -> dict[str, SourceEvidence]:
        """按 id 批量读取证据，返回 ``{evidence_id: SourceEvidence}``。

        逐条 ``get_source_evidence`` 在 revision 级读取（识别预览、来源卡只读评估）里
        会退化成 N+1：3 万条证据就是 6 万次单条 SELECT（还要加上同样数量的 parsed_facts
        读取）。这里按 ``_BULK_BATCH_SIZE`` 一批，返回对象与单条读取完全一致；缺失的 id
        同样抛 ``KeyError``，调用方语义不变。
        """

        unique = list(dict.fromkeys(str(item) for item in evidence_ids if str(item)))
        found: dict[str, SourceEvidence] = {}
        for offset in range(0, len(unique), _BULK_BATCH_SIZE):
            batch = unique[offset : offset + _BULK_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT * FROM source_evidence WHERE evidence_id IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                found[str(row["evidence_id"])] = self._row_to_source_evidence(row)
        for evidence_id in unique:
            if evidence_id not in found:
                raise KeyError(evidence_id)
        return found

    def list_confirmed_source_evidence(self, root_id: str) -> list[SourceEvidence]:
        """一次查询读取来源根当前唯一 confirmed revision 的完整证据。"""

        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT se.*
                FROM import_revisions ir
                JOIN revision_evidence re ON re.revision_id = ir.revision_id
                JOIN source_evidence se ON se.evidence_id = re.evidence_id
                WHERE ir.root_id = ? AND ir.status = 'confirmed'
                ORDER BY se.relative_path COLLATE NOCASE, se.evidence_id
                """,
                (root_id,),
            ).fetchall()
        return [self._row_to_source_evidence(row) for row in rows]

    def save_parsed_facts(self, facts: ParsedFacts) -> None:
        self.save_parsed_facts_bulk([facts])

    @staticmethod
    def _parsed_facts_values(facts: ParsedFacts) -> tuple:
        return (
            facts.parsed_fact_id,
            facts.evidence_id,
            facts.parser_version,
            facts.resource_type,
            facts.media_type,
            facts.group_type,
            facts.work_title,
            facts.original_title,
            facts.series_group,
            facts.card_type,
            facts.relation_type,
            facts.show_type,
            json.dumps(facts.title_candidates, ensure_ascii=False),
            facts.year_candidate,
            facts.season_token_raw,
            facts.episode_token_raw,
            facts.episode_title,
            facts.season_candidate,
            facts.episode_candidate,
            facts.absolute_episode_candidate,
            int(facts.special_candidate),
            json.dumps(facts.episode_range, ensure_ascii=False),
            facts.special_number,
            facts.tmdb_hint_id,
            facts.tmdb_hint_type,
            facts.release_group,
            json.dumps(facts.edition_tags, ensure_ascii=False),
            json.dumps(facts.quality_tags, ensure_ascii=False),
            facts.confidence,
            int(facts.needs_review),
            int(facts.is_importable),
            int(facts.is_auxiliary),
            json.dumps(facts.reasons, ensure_ascii=False),
            json.dumps(facts.warnings, ensure_ascii=False),
        )

    def save_parsed_facts_bulk(self, facts: list[ParsedFacts]) -> None:
        """批量持久化不可变解析事实，避免大库逐条打开 SQLite 连接。"""

        if not facts:
            return
        with self.database.connect() as conn:
            # 只对"已经存在的 id"做不可变校验：
            # INSERT OR IGNORE 插入的新行必然等于入参，回读它们纯属浪费——3 万条
            # facts 的全量回读 + 逐行 6 次 json.loads 是导入期的秒级纯开销。
            # 先只取 id（不解析 JSON），插入后仅回读冲突候选。
            ids = [item.parsed_fact_id for item in facts]
            existing_ids: set[str] = set()
            for offset in range(0, len(ids), _BULK_BATCH_SIZE):
                batch = ids[offset : offset + _BULK_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                existing_ids.update(
                    str(row[0])
                    for row in conn.execute(
                        f"SELECT parsed_fact_id FROM parsed_facts WHERE parsed_fact_id IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            conn.executemany(
                """
                INSERT OR IGNORE INTO parsed_facts(
                    parsed_fact_id, evidence_id, parser_version, resource_type, media_type,
                    group_type, work_title, original_title, series_group, card_type,
                    relation_type, show_type, title_candidates_json,
                    year_candidate, season_token_raw, episode_token_raw, episode_title, season_candidate,
                    episode_candidate, absolute_episode_candidate, special_candidate,
                    episode_range_json, special_number, tmdb_hint_id, tmdb_hint_type,
                    release_group, edition_tags_json, quality_tags_json, confidence,
                    needs_review, is_importable, is_auxiliary, reasons_json, warnings_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [self._parsed_facts_values(item) for item in facts],
            )

            conflicts = [item for item in facts if item.parsed_fact_id in existing_ids]
            persisted: dict[str, ParsedFacts] = {}
            conflict_ids = [item.parsed_fact_id for item in conflicts]
            for offset in range(0, len(conflict_ids), _BULK_BATCH_SIZE):
                batch = conflict_ids[offset : offset + _BULK_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM parsed_facts WHERE parsed_fact_id IN ({placeholders})",
                    batch,
                ).fetchall()
                persisted.update({row["parsed_fact_id"]: self._row_to_parsed_facts(row) for row in rows})
        for item in conflicts:
            if persisted.get(item.parsed_fact_id) != item:
                raise ValueError(f"不可变 ParsedFacts 冲突: {item.parsed_fact_id}")

    @staticmethod
    def _row_to_parsed_facts(row) -> ParsedFacts:
        episode_range = json.loads(row["episode_range_json"])
        return ParsedFacts(
            parsed_fact_id=row["parsed_fact_id"],
            evidence_id=row["evidence_id"],
            parser_version=row["parser_version"],
            resource_type=row["resource_type"],
            media_type=row["media_type"],
            group_type=row["group_type"],
            work_title=row["work_title"],
            original_title=row["original_title"],
            series_group=row["series_group"],
            card_type=row["card_type"],
            relation_type=row["relation_type"],
            show_type=row["show_type"],
            title_candidates=tuple(json.loads(row["title_candidates_json"])),
            year_candidate=row["year_candidate"],
            season_token_raw=row["season_token_raw"],
            episode_token_raw=row["episode_token_raw"],
            episode_title=row["episode_title"],
            season_candidate=row["season_candidate"],
            episode_candidate=row["episode_candidate"],
            absolute_episode_candidate=row["absolute_episode_candidate"],
            special_candidate=bool(row["special_candidate"]),
            episode_range=tuple(episode_range) if episode_range is not None else None,
            special_number=row["special_number"],
            tmdb_hint_id=row["tmdb_hint_id"],
            tmdb_hint_type=row["tmdb_hint_type"],
            release_group=row["release_group"],
            edition_tags=tuple(json.loads(row["edition_tags_json"])),
            quality_tags=tuple(json.loads(row["quality_tags_json"])),
            confidence=row["confidence"],
            needs_review=bool(row["needs_review"]),
            is_importable=bool(row["is_importable"]),
            is_auxiliary=bool(row["is_auxiliary"]),
            reasons=tuple(json.loads(row["reasons_json"])),
            warnings=tuple(json.loads(row["warnings_json"])),
        )

    def get_parsed_facts(self, parsed_fact_id: str, conn=None) -> ParsedFacts:
        with (nullcontext(conn) if conn is not None else self.database.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM parsed_facts WHERE parsed_fact_id = ?",
                (parsed_fact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(parsed_fact_id)
        return self._row_to_parsed_facts(row)

    def get_parsed_facts_bulk(self, parsed_fact_ids, *, conn) -> dict[str, ParsedFacts]:
        """按 id 批量读取解析事实；语义与 ``get_parsed_facts`` 一致（缺失抛 KeyError）。"""

        unique = list(dict.fromkeys(str(item) for item in parsed_fact_ids if str(item)))
        found: dict[str, ParsedFacts] = {}
        for offset in range(0, len(unique), _BULK_BATCH_SIZE):
            batch = unique[offset : offset + _BULK_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT * FROM parsed_facts WHERE parsed_fact_id IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                found[str(row["parsed_fact_id"])] = self._row_to_parsed_facts(row)
        for parsed_fact_id in unique:
            if parsed_fact_id not in found:
                raise KeyError(parsed_fact_id)
        return found

    def list_confirmed_work_facts(self, revision_id: str, work_id: str, *, conn) -> list[ParsedFacts]:
        """读取本次已确认成员及其确认时覆盖，不重解析，也不复用历史别名。"""

        rows = conn.execute(
            """
            SELECT DISTINCT pf.*, rb.override_json AS confirmed_override_json
            FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN revision_evidence re
              ON re.revision_id = rb.revision_id AND re.evidence_id = rb.evidence_id
            JOIN parsed_facts pf ON pf.parsed_fact_id = re.parsed_fact_id
            WHERE rb.revision_id = ? AND rb.work_id = ? AND ir.status = 'confirmed'
            ORDER BY pf.evidence_id
            """,
            (revision_id, work_id),
        ).fetchall()
        facts = []
        for row in rows:
            overrides = json.loads(row["confirmed_override_json"] or "{}")
            for key in ("title_candidates", "edition_tags"):
                if key in overrides:
                    overrides[key] = tuple(overrides[key] or ())
            facts.append(replace(self._row_to_parsed_facts(row), **overrides))
        return facts
