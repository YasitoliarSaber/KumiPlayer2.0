"""V4 Library Projection 合同。"""

from __future__ import annotations


def _entry(evidence_id: str):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-projection",
        root_id="root-projection",
        source_key=evidence_id,
        relative_path=f"Show/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://{evidence_id}",
        fingerprint=f"sha256:{evidence_id}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        year_candidate=2024,
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    return evidence, facts


def test_projection_has_one_card_and_counts_all_assets(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "projection.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("1080p"), _entry("2160p")])
    revisions.confirm("rev-1")

    snapshot = V4LibraryProjection(database).rebuild()

    assert len(snapshot.cards) == 1
    assert snapshot.cards[0]["title"] == "Show"
    assert snapshot.cards[0]["media_type"] == "tv"
    assert snapshot.cards[0]["episode_count"] == 1
    assert snapshot.cards[0]["asset_count"] == 2


def test_projection_can_be_deleted_and_rebuilt_from_sqlite_authority(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "projection-rebuild.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("a")])
    revisions.confirm("rev-1")
    projection = V4LibraryProjection(database)
    first = projection.rebuild()
    with database.connect() as conn:
        conn.execute("DELETE FROM library_cards")
        conn.execute("DELETE FROM library_generations")
        conn.execute("DELETE FROM v4_meta WHERE key = 'current_library_generation'")
    second = projection.rebuild()

    assert second.digest == first.digest
    assert second.cards == first.cards


def test_same_work_across_revisions_reuses_work_episode_and_asset_identity(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "stable-identities.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("first")])
    revisions.confirm("rev-1")
    revisions.create_draft("rev-2", [_entry("second")])
    revisions.confirm("rev-2")

    with database.connect() as conn:
        works = conn.execute("SELECT work_id FROM works").fetchall()
        episodes = conn.execute("SELECT episode_id FROM episodes").fetchall()
        assets = conn.execute("SELECT asset_id FROM assets").fetchall()
        card = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM revision_bindings rb
            JOIN works w ON w.work_id = rb.work_id
            WHERE rb.revision_id IN ('rev-1', 'rev-2')
            """
        ).fetchone()

    assert len(works) == 1
    assert len(episodes) == 1
    assert len(assets) == 2
    assert card["count"] == 2


def test_title_change_on_same_source_lineage_keeps_work_identity(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "title-change.db")
    database.initialize()
    revisions = V4RevisionService(database)

    def entry(revision: str, title: str):
        evidence = SourceEvidence(
            evidence_id=f"evidence-{revision}",
            scan_id=f"scan-{revision}",
            root_id="root-title-change",
            source_key=f"Show/{revision}.mkv",
            relative_path=f"Show/{revision}.mkv",
            entry_kind="video",
            provider="local",
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{revision}",
            evidence_id=evidence.evidence_id,
            parser_version="fixture",
            work_title=title,
            title_candidates=(title,),
            year_candidate=2024,
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=1,
        )
        return evidence, facts

    revisions.create_draft("rev-title-1", [entry("one", "Old Title")])
    revisions.confirm("rev-title-1")
    revisions.create_draft("rev-title-2", [entry("two", "New Title")])
    revisions.confirm("rev-title-2")

    with database.connect() as conn:
        works = conn.execute("SELECT work_id, preferred_title FROM works").fetchall()

    assert len(works) == 1
    assert works[0]["preferred_title"] == "New Title"
