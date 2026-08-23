"""V4 Revision 草稿/确认状态机。"""

from __future__ import annotations

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
    assert {job["job_type"] for job in jobs} == {"materialize_mirror", "refresh_projection"}
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

    assert len(service.list_jobs("rev-1")) == 2


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
