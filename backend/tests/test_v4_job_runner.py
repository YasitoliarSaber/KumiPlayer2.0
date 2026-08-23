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


def test_runner_materializes_mirror_and_publishes_projection(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "runner.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-runner", [_entry()])
    service.confirm("rev-runner")

    runner = V4JobRunner(database)
    results = runner.process_available(mirror_root=tmp_path / "mirror")

    assert {result.job_type for result in results} == {"materialize_mirror", "refresh_projection"}
    assert all(result.status == "succeeded" for result in results)
    with database.connect() as conn:
        statuses = {
            row["job_type"]: row["status"]
            for row in conn.execute("SELECT job_type, status FROM jobs WHERE revision_id = 'rev-runner'")
        }
    assert statuses == {"materialize_mirror": "succeeded", "refresh_projection": "succeeded"}
    assert list((tmp_path / "mirror").rglob("*.strm"))


def test_runner_does_not_consume_scrape_without_explicit_provider(tmp_path):
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
            INSERT INTO jobs(job_id, job_type, revision_id, idempotency_key, created_at, updated_at)
            VALUES ('job', 'scrape_work', 'rev', 'scrape:rev', 'now', 'now');
            """
        )

    runner = V4JobRunner(database)
    try:
        runner.process_job("job")
    except ValueError as exc:
        assert "provider" in str(exc)
    else:
        raise AssertionError("scrape job must require an explicit provider")
