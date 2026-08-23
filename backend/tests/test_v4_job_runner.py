"""V4 outbox 运行器只消费 confirmed revision 的本地任务。"""

from __future__ import annotations


def _entry():
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id="ev-runner",
        scan_id="scan-runner",
        root_id="root-runner",
        source_key="runner",
        relative_path="Runner/Runner.S01E01.mkv",
        entry_kind="video",
        source_locator="local://runner/Runner.S01E01.mkv",
        playback_locator="local://runner/Runner.S01E01.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-runner",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Runner",
        title_candidates=("Runner",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    return evidence, facts


def test_runner_materializes_scrapes_and_publishes_projection(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "runner.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-runner", [_entry()])
    service.confirm("rev-runner")

    runner = V4JobRunner(
        database,
        metadata_provider=lambda _target: {
            "provider": "tmdb",
            "provider_id": "9",
            "title": "Runner Online",
            "plot": "metadata reached the projection",
        },
    )
    results = runner.process_available(mirror_root=tmp_path / "mirror")

    assert [result.job_type for result in results] == [
        "materialize_mirror",
        "scrape_work",
        "refresh_projection",
    ]
    assert all(result.status == "succeeded" for result in results)
    with database.connect() as conn:
        statuses = {
            row["job_type"]: row["status"]
            for row in conn.execute("SELECT job_type, status FROM jobs WHERE revision_id = 'rev-runner'")
        }
    assert statuses == {
        "materialize_mirror": "succeeded",
        "scrape_work": "succeeded",
        "refresh_projection": "succeeded",
    }
    assert list((tmp_path / "mirror").rglob("*.strm"))
    assert list((tmp_path / "mirror").rglob("tvshow.nfo"))
    assert list((tmp_path / "mirror").rglob("S01E01.nfo"))
    card = runner.projection.current().cards[0]
    assert card["title"] == "Runner Online"
    assert card["metadata"]["plot"] == "metadata reached the projection"


def test_special_episode_nfo_keeps_zero_season_number():
    from app.media_v4.jobs.metadata_artifacts import _episode_nfo

    payload = _episode_nfo(
        {
            "local_season_number": 0,
            "special_number": 2,
            "season_kind": "special",
        }
    ).decode("utf-8")

    assert "<season>0</season>" in payload
    assert "<episode>2</episode>" in payload


def test_runner_uses_configured_metadata_provider_for_scrape(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "runner.db")
    database.initialize()
    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan', 'root', 1, 'completed');
            INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at)
            VALUES ('rev', 'root', 'scan', 'fixture', 'confirmed', 'now');
            INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at)
            VALUES ('work', 'runner', 'series', 'Runner', 'now', 'now');
            INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, created_at, updated_at)
            VALUES ('job', 'scrape_work', 'rev', 'work', 'scrape:rev', 'now', 'now');
            """
        )

    called = []
    runner = V4JobRunner(
        database,
        metadata_provider=lambda target: called.append(target) or {
            "provider": "tmdb",
            "provider_id": "42",
            "title": "Runner",
        },
    )
    result = runner.process_job("job")

    assert result.status == "succeeded"
    assert called and called[0]["revision_id"] == "rev"


def test_superseded_artifacts_are_cleaned_only_after_empty_revision_is_published(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "cleanup.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-old", [_entry()])
    revisions.confirm("rev-old")
    runner = V4JobRunner(
        database,
        metadata_provider=lambda target: {
            "provider": "local",
            "provider_id": target["work_id"],
            "title": target["preferred_title"],
        },
    )
    mirror_root = tmp_path / "mirror"
    runner.process_available(mirror_root=mirror_root)
    assert list(mirror_root.rglob("*.*"))

    revisions.create_draft(
        "rev-empty",
        [],
        root_id="root-runner",
        scan_id="scan-empty",
    )
    revisions.confirm("rev-empty")
    results = runner.process_available(mirror_root=mirror_root)

    assert [result.job_type for result in results] == [
        "refresh_projection",
        "cleanup_superseded_artifacts",
    ]
    assert not list(mirror_root.rglob("*.*"))
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
