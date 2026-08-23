"""V4 outbox 原子性与幂等键合同。"""

from __future__ import annotations


def test_confirmed_revision_has_one_job_per_work_and_one_projection_job(tmp_path):
    from backend.tests.test_v4_revision_confirmation import _entry

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "jobs.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft(
        "rev-1",
        [_entry(evidence_id="ev-1"), _entry(evidence_id="ev-2")],
    )
    service.confirm("rev-1")

    jobs = service.list_jobs("rev-1")
    assert len([job for job in jobs if job["job_type"] == "materialize_mirror"]) == 1
    assert len([job for job in jobs if job["job_type"] == "refresh_projection"]) == 1
