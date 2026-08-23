"""SourceEvidence、ParsedFacts 与 confirmed revision 的不可变合同。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest


def test_source_evidence_and_parsed_facts_are_frozen_values():
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id="ev-1",
        scan_id="scan-1",
        root_id="root-1",
        source_key="/anime/show/S01E01.mkv",
        relative_path="show/S01E01.mkv",
        entry_kind="video",
        size=123,
        mtime=456.0,
        fingerprint="sha256:test",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-1",
        evidence_id=evidence.evidence_id,
        parser_version="v4-test",
        title_candidates=("show",),
        season_token_raw="S01",
        episode_token_raw="E01",
        season_candidate=1,
        episode_candidate=1,
    )

    with pytest.raises(FrozenInstanceError):
        evidence.size = 999  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        facts.episode_candidate = 9  # type: ignore[misc]


def test_repository_round_trip_preserves_source_and_parse_payload_without_loss(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository

    database = V4Database(tmp_path / "facts.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_roots(
                root_id, provider, ingest_method, created_at, updated_at
            ) VALUES ('root-full', 'openlist', 'txt_tree', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan-full', 'root-full', 1, 'completed')
            """
        )
    evidence = SourceEvidence(
        evidence_id="ev-full",
        scan_id="scan-full",
        root_id="root-full",
        source_key="remote:show/S01E01.mkv",
        relative_path="show/S01E01.mkv",
        entry_kind="video",
        size=987,
        mtime=1234.5,
        fingerprint="sha256:full",
        raw_file_id="raw-1",
        ingest_method="txt_tree",
        source_route_id="route-1",
        tmdb_hint_id="tv-42",
        tmdb_hint_type="tv",
        import_family="anime",
        target_filename="S01E01.strm",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-full",
        evidence_id=evidence.evidence_id,
        parser_version="parser-4.0",
        title_candidates=("Show", "Show JP"),
        year_candidate=2024,
        season_token_raw="S01",
        episode_token_raw="E01-E02",
        season_candidate=1,
        episode_candidate=1,
        absolute_episode_candidate=13,
        special_candidate=False,
        episode_range=(1, 2),
        release_group="GROUP",
        edition_tags=("extended",),
        quality_tags=("2160p", "HDR"),
        confidence="high",
        warnings=("ambiguous-title",),
    )

    repository = V4Repository(database)
    repository.save_source_evidence(evidence)
    repository.save_parsed_facts(facts)

    assert repository.get_source_evidence(evidence.evidence_id) == evidence
    assert repository.get_parsed_facts(facts.parsed_fact_id) == facts
