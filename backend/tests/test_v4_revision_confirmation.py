"""V4 Revision 草稿/确认状态机。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest


def _entry(title: str = "Show", evidence_id: str = "ev-revision"):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-revision",
        root_id="root-revision",
        source_key=evidence_id,
        relative_path=f"{title}/{evidence_id}.mkv" if title else f"unknown/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=title,
        title_candidates=(title,) if title else (),
        media_type="tv" if title else "",
        group_type="season" if title else "unknown",
        season_candidate=1 if title else None,
        episode_candidate=1 if title else None,
    )
    return evidence, facts


def test_confirmation_atomically_publishes_revision_and_outbox_jobs(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "revision.db")
    database.initialize()
    service = V4RevisionService(database)

    service.create_draft("rev-1", [_entry()])
    service.confirm("rev-1")

    assert service.get_status("rev-1") == "confirmed"
    jobs = service.list_jobs("rev-1")
    assert {job["job_type"] for job in jobs} == {
        "materialize_mirror",
        "scrape_work",
        "refresh_projection",
    }
    assert len({job["idempotency_key"] for job in jobs}) == len(jobs)


def test_confirmation_is_idempotent_and_does_not_duplicate_jobs(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "idempotent.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-1", [_entry()])

    service.confirm("rev-1")
    service.confirm("rev-1")

    assert len(service.list_jobs("rev-1")) == 3


def test_review_issues_block_confirmation_without_legacy_fallback(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import RevisionBlockedError, V4RevisionService

    database = V4Database(tmp_path / "blocked.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-1", [_entry(title="", evidence_id="ev-blocked")])

    with pytest.raises(RevisionBlockedError, match="review"):
        service.confirm("rev-1")

    assert service.get_status("rev-1") == "draft"
    assert service.list_jobs("rev-1") == []


def test_draft_does_not_publish_authoritative_media_graph(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "draft-isolation.db")
    database.initialize()
    service = V4RevisionService(database)

    service.create_draft("rev-draft", [_entry()])

    with database.connect() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("works", "seasons", "episodes", "editions", "assets", "revision_bindings")
        }

    assert counts == {table: 0 for table in counts}


def test_unconfirmed_draft_cannot_change_confirmed_library_projection(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "draft-projection-isolation.db")
    database.initialize()
    service = V4RevisionService(database)

    service.create_draft("rev-confirmed", [_entry(evidence_id="episode-1")])
    service.confirm("rev-confirmed")
    before = V4LibraryProjection(database).rebuild()

    evidence = SourceEvidence(
        evidence_id="episode-2",
        scan_id="scan-draft-2",
        root_id="root-revision",
        source_key="episode-2",
        relative_path="Show/Show.S01E02.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-episode-2",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=2,
    )
    service.create_draft("rev-draft", [(evidence, facts)])
    after = V4LibraryProjection(database).rebuild()

    assert after.cards == before.cards


def test_confirmed_range_keeps_one_asset_bound_to_each_episode(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "range-confirm.db")
    database.initialize()
    service = V4RevisionService(database)
    evidence = SourceEvidence(
        evidence_id="range",
        scan_id="scan-range",
        root_id="root-range",
        source_key="range",
        relative_path="Show/Show.S01E01-E03.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-range",
        evidence_id="range",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        episode_range=(1, 3),
    )

    service.create_draft("rev-range", [(evidence, facts)])
    service.confirm("rev-range")

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM episode_assets").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM revision_bindings WHERE revision_id = 'rev-range'"
        ).fetchone()[0] == 3


def test_draft_override_resolves_issue_without_mutating_parsed_facts(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "override.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-override", [_entry(title="", evidence_id="needs-fix")])

    with pytest.raises(ValueError, match="必须是字符串"):
        service.apply_override("rev-override", "needs-fix", {"work_title": 123})

    service.apply_override(
        "rev-override",
        "needs-fix",
        {
            "work_title": "Fixed Show",
            "title_candidates": ["Fixed Show"],
            "media_type": "tv",
        },
    )
    graph = service.apply_override(
        "rev-override",
        "needs-fix",
        {
            "group_type": "season",
            "season_candidate": 1,
            "episode_candidate": 1,
            "needs_review": False,
        },
    )
    assert graph.issues == ()
    service.confirm("rev-override")

    with database.connect() as conn:
        parsed = conn.execute(
            "SELECT work_title FROM parsed_facts WHERE evidence_id = 'needs-fix'"
        ).fetchone()
        work = conn.execute("SELECT preferred_title FROM works").fetchone()
        override = conn.execute(
            "SELECT overrides_json FROM revision_overrides WHERE revision_id = 'rev-override'"
        ).fetchone()
        binding = conn.execute(
            "SELECT decision_source, override_json FROM revision_bindings WHERE revision_id = 'rev-override'"
        ).fetchone()
    assert parsed["work_title"] == ""
    assert work["preferred_title"] == "Fixed Show"
    assert json.loads(override["overrides_json"])["season_candidate"] == 1
    assert binding["decision_source"] == "manual_override"
    assert json.loads(binding["override_json"])["work_title"] == "Fixed Show"

    with database.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE revision_overrides SET overrides_json = '{}' WHERE revision_id = 'rev-override'"
        )


def test_graph_digest_covers_asset_membership(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "digest.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-one", [_entry(evidence_id="asset-one")])
    service.create_draft(
        "rev-two",
        [_entry(evidence_id="asset-one"), _entry(evidence_id="asset-two")],
    )

    with database.connect() as conn:
        digests = {
            row["revision_id"]: row["graph_digest"]
            for row in conn.execute(
                "SELECT revision_id, graph_digest FROM import_revisions ORDER BY revision_id"
            )
        }
    assert digests["rev-one"] != digests["rev-two"]


def test_new_confirmed_revision_supersedes_same_root_without_unlocking_snapshot(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "supersede.db")
    database.initialize()
    service = V4RevisionService(database)
    first = _entry(evidence_id="old-asset")
    service.create_draft("rev-old", [first])
    service.confirm("rev-old")

    evidence, facts = _entry(evidence_id="new-asset")
    second = (evidence, replace(facts, episode_candidate=2))
    service.create_draft("rev-new", [second])
    service.confirm("rev-new")

    with database.connect() as conn:
        statuses = {
            row["revision_id"]: row["status"]
            for row in conn.execute("SELECT revision_id, status FROM import_revisions")
        }
        old_binding = conn.execute(
            "SELECT binding_id FROM revision_bindings WHERE revision_id = 'rev-old'"
        ).fetchone()[0]
    assert statuses == {"rev-old": "superseded", "rev-new": "confirmed"}
    card = V4LibraryProjection(database).rebuild().cards[0]
    assert card["episode_count"] == 1
    assert card["asset_count"] == 1

    captured = []
    scrape_job = next(
        job for job in service.list_jobs("rev-new") if job["job_type"] == "scrape_work"
    )
    from app.media_v4.jobs.scrape import V4ScrapeService

    V4ScrapeService(database).process(
        scrape_job["job_id"],
        lambda target: captured.append(target)
        or {"provider": "local", "provider_id": target["work_id"], "title": "Show"},
    )
    assert [episode["local_episode_number"] for episode in captured[0]["episodes"]] == [2]
    assert all(job["status"] == "cancelled" for job in service.list_jobs("rev-old"))

    with database.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE revision_bindings SET confidence = 'low' WHERE binding_id = ?",
            (old_binding,),
        )
    with database.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE import_revisions SET status = 'draft' WHERE revision_id = 'rev-old'")
