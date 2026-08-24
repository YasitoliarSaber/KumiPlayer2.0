"""V4 镜像物化器合同。"""

from __future__ import annotations

import pytest


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

    def entry(evidence_id: str, name: str) -> tuple:
        media = tmp_path / name
        media.write_bytes(b"video")
        evidence, facts = _entry(evidence_id, str(media))
        return evidence, facts

    database = V4Database(tmp_path / "mirror.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft(
        "rev-mirror",
        [entry("1080p", "show.1080p.mkv"), entry("2160p", "show.2160p.mkv")],
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
        str(tmp_path / "show.1080p.mkv"),
        str(tmp_path / "show.2160p.mkv"),
    }


def test_mirror_job_is_idempotent_after_success(tmp_path):
    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    media = tmp_path / "show.mkv"
    media.write_bytes(b"video")
    database = V4Database(tmp_path / "mirror-idempotent.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-mirror", [_entry("1080p", str(media))])
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


def test_remote_assets_without_fingerprints_get_distinct_mirror_files(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "mirror-no-fingerprint.db")
    database.initialize()
    pairs = []
    for evidence_id, locator in (("remote-a", "https://example.invalid/a.mkv"), ("remote-b", "https://example.invalid/b.mkv")):
        evidence = SourceEvidence(
            evidence_id=evidence_id,
            scan_id="scan-remote",
            root_id="root-remote",
            source_key=evidence_id,
            relative_path=f"Show/{evidence_id}.mkv",
            entry_kind="video",
            provider="pan115",
            source_locator=locator,
            playback_locator=locator,
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
        )
        pairs.append((evidence, facts))

    revisions = V4RevisionService(database)
    revisions.create_draft("rev-remote", pairs)
    revisions.confirm("rev-remote")
    job = next(job for job in revisions.list_jobs("rev-remote") if job["job_type"] == "materialize_mirror")

    V4MirrorMaterializer(database).process(job["job_id"], tmp_path / "mirror")
    files = sorted((tmp_path / "mirror").rglob("*.strm"))

    assert len(files) == 2
    assert {path.read_text(encoding="utf-8") for path in files} == {
        "https://example.invalid/a.mkv",
        "https://example.invalid/b.mkv",
    }


def test_remote_tree_asset_with_relative_locator_fails_before_publishing(tmp_path):
    """无效/相对播放定位不能发布 STRM：mirror 必须失败且零 Artifact。"""

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    evidence = SourceEvidence(
        evidence_id="inventory-only",
        scan_id="scan-inventory",
        root_id="root-inventory",
        source_key="Show/Show.S01E01.mkv",
        relative_path="Show/Show.S01E01.mkv",
        entry_kind="video",
        provider="pan115",
        source_locator="Show/Show.S01E01.mkv",
        playback_locator="",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-inventory",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    database = V4Database(tmp_path / "inventory.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-inventory", [(evidence, facts)])
    revisions.confirm("rev-inventory")
    job = next(
        item for item in revisions.list_jobs("rev-inventory") if item["job_type"] == "materialize_mirror"
    )

    with pytest.raises(RuntimeError, match="不是本地绝对路径"):
        V4MirrorMaterializer(database).process(job["job_id"], tmp_path / "mirror")

    assert list((tmp_path / "mirror").rglob("*.strm")) == []
    with database.connect() as conn:
        status = conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()["status"]
        published = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE revision_id = 'rev-inventory' AND artifact_type = 'mirror'"
        ).fetchone()[0]
    assert status == "failed"
    assert published == 0
