"""V4 不可变来源事实的 repository。"""

from __future__ import annotations

import json

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.persistence.database import V4Database


class V4Repository:
    """只负责事实 round-trip，不在这里执行解析或身份推断。"""

    def __init__(self, database: V4Database):
        self.database = database

    def save_source_evidence(self, evidence: SourceEvidence) -> None:
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_evidence(
                    evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind,
                    size, mtime, fingerprint, raw_file_id, ingest_method, source_route_id,
                    source_locator, playback_locator, tmdb_hint_id, tmdb_hint_type,
                    import_family, target_filename, observed_at, presence_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                ),
            )

    def get_source_evidence(self, evidence_id: str) -> SourceEvidence:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(evidence_id)
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

    def save_parsed_facts(self, facts: ParsedFacts) -> None:
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO parsed_facts(
                    parsed_fact_id, evidence_id, parser_version, resource_type, media_type,
                    group_type, work_title, original_title, series_group, card_type,
                    relation_type, show_type, title_candidates_json,
                    year_candidate, season_token_raw, episode_token_raw, season_candidate,
                    episode_candidate, absolute_episode_candidate, special_candidate,
                    episode_range_json, special_number, tmdb_hint_id, tmdb_hint_type,
                    release_group, edition_tags_json, quality_tags_json, confidence,
                    needs_review, is_importable, is_auxiliary, reasons_json, warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                ),
            )

    def get_parsed_facts(self, parsed_fact_id: str) -> ParsedFacts:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM parsed_facts WHERE parsed_fact_id = ?",
                (parsed_fact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(parsed_fact_id)
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
