"""V4 镜像物化器合同。"""

from __future__ import annotations


def _entry(evidence_id: str, locator: str):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-mirror",
        root_id="root-mirror",
        source_key=evidence_id,
        relative_path=f"Show/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=locator,
        playback_locator=locator,
        fingerprint=f"sha256:{evidence_id}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        quality_tags=(evidence_id,),
    )
    return evidence, facts


def test_mirror_consumes_confirmed_revision_and_keeps_multiple_assets(tmp_path, monkeypatch):
    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "mirror.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft(
        "rev-mirror",
        [_entry("1080p", "local://show/1080p"), _entry("2160p", "local://show/2160p")],
    )
    revision_service.confirm("rev-mirror")
    job = next(
        job for job in revision_service.list_jobs("rev-mirror") if job["job_type"] == "materialize_mirror"
    )

    def fail_if_recognized(*_args, **_kwargs):
        raise AssertionError("mirror 不得重新识别")

    monkeypatch.setattr("app.recognition.media.recognize_media", fail_if_recognized)
    materializer = V4MirrorMaterializer(database)
    result = materializer.process(job["job_id"], tmp_path / "mirror")

    assert result.status == "succeeded"
    files = sorted((tmp_path / "mirror").rglob("*.strm"))
    assert len(files) == 2
    assert {file.read_text(encoding="utf-8") for file in files} == {
        "local://show/1080p",
        "local://show/2160p",
    }


def test_mirror_job_is_idempotent_after_success(tmp_path):
    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "mirror-idempotent.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-mirror", [_entry("1080p", "local://show/1080p")])
    revision_service.confirm("rev-mirror")
    job = next(
        job for job in revision_service.list_jobs("rev-mirror") if job["job_type"] == "materialize_mirror"
    )
    materializer = V4MirrorMaterializer(database)
    first = materializer.process(job["job_id"], tmp_path / "mirror")
    second = materializer.process(job["job_id"], tmp_path / "mirror")

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert len(list((tmp_path / "mirror").rglob("*.strm"))) == 1
