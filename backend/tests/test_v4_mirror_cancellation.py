"""Cancellation and publication safety for V4 mirror materialization."""

from __future__ import annotations

import pytest


def _entry(index: int):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=f"ev-mirror-{index}",
        scan_id="scan-mirror-safety",
        root_id="root-mirror-safety",
        source_key=f"mirror-{index}",
        relative_path=f"Mirror Safety/Mirror Safety.S01E{index:02d}.mkv",
        entry_kind="video",
        source_locator=f"local://mirror/Mirror Safety.S01E{index:02d}.mkv",
        playback_locator=f"local://mirror/Mirror Safety.S01E{index:02d}.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-mirror-{index}",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Mirror Safety",
        title_candidates=("Mirror Safety",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=index,
    )
    return evidence, facts


def _mirror_job(database, revision_id: str) -> str:
    with database.connect() as conn:
        return str(
            conn.execute(
                "SELECT job_id FROM jobs WHERE revision_id = ? AND job_type = 'materialize_mirror'",
                (revision_id,),
            ).fetchone()["job_id"]
        )


def test_cancelling_after_an_existing_target_is_seen_never_deletes_that_target(tmp_path, monkeypatch):
    """Only files created by this attempt may be removed during cancellation."""

    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "mirror-cancel.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-mirror-cancel", [_entry(1), _entry(2)])
    revisions.confirm("rev-mirror-cancel")
    job_id = _mirror_job(database, "rev-mirror-cancel")
    mirror_root = tmp_path / "mirror"
    materializer = V4MirrorMaterializer(database)

    initial = materializer.process(job_id, mirror_root)
    existing_target = initial.artifact_paths[0]
    with database.connect() as conn:
        conn.execute("DELETE FROM artifacts WHERE revision_id = ?", ("rev-mirror-cancel",))
        conn.execute(
            "UPDATE jobs SET status = 'queued', cancel_requested = 0, finished_at = '' WHERE job_id = ?",
            (job_id,),
        )

    import app.media_v4.jobs.mirror as mirror_module

    calls = {"count": 0}

    def cancel_after_first_target(_database, _job_id):
        calls["count"] += 1
        return calls["count"] >= 4

    monkeypatch.setattr(mirror_module, "cancel_requested", cancel_after_first_target)

    result = materializer.process(job_id, mirror_root)

    assert result.status == "cancelled"
    assert (tmp_path / "mirror").exists()
    assert existing_target in {str(path) for path in mirror_root.rglob("*.strm")}


def test_failure_after_file_publish_rolls_back_unregistered_new_target(tmp_path, monkeypatch):
    """A database failure between publish and artifact registration leaves no orphan .strm."""

    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "mirror-orphan.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-mirror-orphan", [_entry(1)])
    revisions.confirm("rev-mirror-orphan")
    job_id = _mirror_job(database, "rev-mirror-orphan")

    import app.media_v4.jobs.mirror as mirror_module

    original_uuid4 = mirror_module.uuid.uuid4
    calls = {"count": 0}

    def fail_while_registering_artifact():
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("artifact registration interrupted")
        return original_uuid4()

    monkeypatch.setattr(mirror_module.uuid, "uuid4", fail_while_registering_artifact)

    with pytest.raises(RuntimeError, match="artifact registration interrupted"):
        V4MirrorMaterializer(database).process(job_id, tmp_path / "mirror")

    assert not list((tmp_path / "mirror").rglob("*.strm"))
    with database.connect() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        artifacts = conn.execute("SELECT COUNT(*) FROM artifacts WHERE revision_id = ?", ("rev-mirror-orphan",)).fetchone()[0]
    assert row["status"] == "failed"
    assert artifacts == 0
