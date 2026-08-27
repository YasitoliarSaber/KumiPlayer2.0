"""来源重新导入后的活动状态与来源卡时序回归。"""

from __future__ import annotations


def _entry(root_id: str = "root-reactivated", episode: int = 1):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=f"ev-reactivated-{episode}",
        scan_id="scan-reactivated",
        root_id=root_id,
        source_key=f"Mini/Mini.S01E{episode:02d}.mkv",
        relative_path=f"Mini/Mini.S01E{episode:02d}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://mini/Mini.S01E{episode:02d}.mkv",
        playback_locator=f"local://mini/Mini.S01E{episode:02d}.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-reactivated-{episode}",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Mini",
        title_candidates=("Mini",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=episode,
    )
    return evidence, facts


def test_confirm_reactivates_retired_root_and_creates_source_card_immediately(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "reactivate-on-confirm.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-reactivated", [_entry(episode=1), _entry(episode=2)])
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_roots SET retired_at = ?, retired_reason = ? WHERE root_id = ?",
            ("2026-08-26T00:00:00+00:00", "fixture", "root-reactivated"),
        )

    assert list_source_cards(database) == []

    revisions.confirm("rev-reactivated")

    cards = list_source_cards(database)
    assert len(cards) == 1
    assert cards[0]["revision_id"] == "rev-reactivated"
    assert cards[0]["work_count"] == 1
    assert cards[0]["asset_count"] == 2
    assert cards[0]["job_summary"]["queued"] > 0
    with database.connect() as conn:
        root = conn.execute(
            "SELECT retired_at, retired_reason FROM source_roots WHERE root_id = ?",
            ("root-reactivated",),
        ).fetchone()
    assert root["retired_at"] == ""
    assert root["retired_reason"] == ""


def test_initialize_repairs_only_roots_reimported_after_retirement(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "reactivation-repair.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-reimported", [_entry("root-reimported")])
    revisions.confirm("rev-reimported")
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_roots SET retired_at = ?, retired_reason = ? WHERE root_id = ?",
            ("2000-01-01T00:00:00+00:00", "stale retirement", "root-reimported"),
        )
        conn.execute(
            """
            INSERT INTO source_roots(
                root_id, provider, ingest_method, retired_at, retired_reason, created_at, updated_at
            ) VALUES (?, 'local', 'local_scan', ?, 'current retirement', ?, ?)
            """,
            (
                "root-still-retired",
                "2999-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    database.initialize()

    with database.connect() as conn:
        repaired = conn.execute(
            "SELECT retired_at FROM source_roots WHERE root_id = 'root-reimported'"
        ).fetchone()
        preserved = conn.execute(
            "SELECT retired_at FROM source_roots WHERE root_id = 'root-still-retired'"
        ).fetchone()
        dirty = conn.execute(
            "SELECT value FROM v4_meta WHERE key = 'library_projection_dirty'"
        ).fetchone()
    assert repaired["retired_at"] == ""
    assert preserved["retired_at"] == "2999-01-01T00:00:00+00:00"
    assert dirty is not None
