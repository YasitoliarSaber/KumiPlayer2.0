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


def test_bulk_parsed_facts_write_preserves_immutability(tmp_path):
    from dataclasses import replace

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository

    database = V4Database(tmp_path / "bulk-facts.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-bulk', 'local', 'local_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) "
            "VALUES ('scan-bulk', 'root-bulk', 1, 'completed')"
        )
    evidence = [
        SourceEvidence(
            evidence_id=f"ev-bulk-{index}",
            scan_id="scan-bulk",
            root_id="root-bulk",
            source_key=f"Show/E{index:02d}.mkv",
            relative_path=f"Show/E{index:02d}.mkv",
            entry_kind="video",
        )
        for index in range(1, 4)
    ]
    facts = [
        ParsedFacts(
            parsed_fact_id=f"facts-bulk-{index}",
            evidence_id=item.evidence_id,
            parser_version="v4",
            work_title="Show",
            title_candidates=("Show",),
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=index,
        )
        for index, item in enumerate(evidence, start=1)
    ]
    repository = V4Repository(database)
    repository.save_scan_evidence_bulk(evidence)

    repository.save_parsed_facts_bulk(facts)

    assert [repository.get_parsed_facts(item.parsed_fact_id) for item in facts] == facts
    with pytest.raises(ValueError, match="ParsedFacts 冲突"):
        repository.save_parsed_facts_bulk([replace(facts[0], work_title="Other")])


def test_persisted_facts_and_confirmed_bindings_are_sql_immutable(tmp_path):
    import sqlite3

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "immutable.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) VALUES ('r', 'local', 'scan', 'n', 'n')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) VALUES ('s', 'r', 1, 'completed')"
        )
    evidence = SourceEvidence(
        evidence_id="ev-immutable",
        scan_id="s",
        root_id="r",
        source_key="Show/S01E01.mkv",
        relative_path="Show/S01E01.mkv",
        entry_kind="video",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-immutable",
        evidence_id=evidence.evidence_id,
        parser_version="v4",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    repository = V4Repository(database)
    repository.save_source_evidence(evidence)
    repository.save_parsed_facts(facts)
    with database.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE source_evidence SET relative_path = 'changed' WHERE evidence_id = 'ev-immutable'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM parsed_facts WHERE parsed_fact_id = 'facts-immutable'")

    service = V4RevisionService(database)
    service.create_draft("rev-immutable", [(evidence, facts)])
    service.confirm("rev-immutable")
    with database.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE revision_bindings SET confidence = 'low' WHERE revision_id = 'rev-immutable'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM revision_bindings WHERE revision_id = 'rev-immutable'")


def test_reusing_immutable_identity_with_different_payload_is_rejected(tmp_path):
    from dataclasses import replace

    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository

    database = V4Database(tmp_path / "immutable-conflict.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root', 'local', 'scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) "
            "VALUES ('scan', 'root', 1, 'completed')"
        )
    evidence = SourceEvidence(
        evidence_id="same-id",
        scan_id="scan",
        root_id="root",
        source_key="Show/file.mkv",
        relative_path="Show/file.mkv",
        entry_kind="video",
    )
    repository = V4Repository(database)
    repository.save_source_evidence(evidence)

    with pytest.raises(ValueError, match="SourceEvidence 冲突"):
        repository.save_source_evidence(replace(evidence, relative_path="Other/file.mkv"))
