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


def test_independent_scrape_jobs_use_bounded_parallel_workers(tmp_path):
    """不同 Work 的在线刮削不应被一个串行 worker 放大成线性长等待。"""

    import threading
    import time
    from dataclasses import replace

    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "parallel-scrapes.db")
    database.initialize()
    entries = []
    for number in range(1, 4):
        evidence, facts = _entry()
        title = f"Runner {number}"
        evidence = replace(
            evidence,
            evidence_id=f"ev-parallel-{number}",
            source_key=f"runner-{number}",
            relative_path=f"{title}/{title}.S01E01.mkv",
            source_locator=f"local://runner/{number}.mkv",
            playback_locator=f"local://runner/{number}.mkv",
        )
        facts = replace(
            facts,
            parsed_fact_id=f"facts-parallel-{number}",
            evidence_id=evidence.evidence_id,
            work_title=title,
            title_candidates=(title,),
        )
        entries.append((evidence, facts))
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-parallel-scrapes", entries)
    revisions.confirm("rev-parallel-scrapes")

    active = 0
    peak_active = 0
    lock = threading.Lock()

    def provider(target):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(0.08)
            return {
                "provider": "tmdb",
                "provider_id": str(target["work_id"]),
                "title": target["preferred_title"],
            }
        finally:
            with lock:
                active -= 1

    results = V4JobRunner(database, metadata_provider=provider).process_available(
        mirror_root=tmp_path / "mirror",
    )

    assert peak_active >= 2
    assert [result.job_type for result in results].count("scrape_work") == 3
    assert all(result.status == "succeeded" for result in results)


def test_successful_scrape_marks_projection_dirty_and_becomes_visible_before_final_job(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "incremental-projection.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-incremental", [_entry()])
    service.confirm("rev-incremental")
    def remote_artwork():
        return SimpleNamespace(artwork_storage_mode="remote")

    monkeypatch.setattr("app.media_v4.jobs.completeness.load_config", remote_artwork)
    monkeypatch.setattr("app.media_v4.jobs.metadata_artifacts.load_config", remote_artwork)
    runner = V4JobRunner(
        database,
        metadata_provider=lambda _target: {
            "provider": "tmdb",
            "provider_id": "9",
            "title": "Runner Online",
            "plot": "published before the final projection job",
            "poster_url": "https://image.tmdb.org/t/p/w780/poster.jpg",
            "fanart_url": "https://image.tmdb.org/t/p/w1280/fanart.jpg",
        },
    )
    before = runner.projection.rebuild()
    jobs = service.list_jobs("rev-incremental")
    mirror = next(item for item in jobs if item["job_type"] == "materialize_mirror")
    scrape = next(item for item in jobs if item["job_type"] == "scrape_work")

    runner.process_job(mirror["job_id"], mirror_root=tmp_path / "mirror")
    runner.process_job(scrape["job_id"], mirror_root=tmp_path / "mirror")

    after = runner.projection.ensure_current()
    assert after.digest != before.digest
    assert after.cards[0]["metadata"]["metadata_state"] == "ready"
    assert after.cards[0]["title"] == "Runner Online"
    with database.connect() as conn:
        final_job = conn.execute(
            "SELECT status FROM jobs WHERE revision_id = ? AND job_type = 'refresh_projection'",
            ("rev-incremental",),
        ).fetchone()
    assert final_job["status"] == "queued"


def test_failed_scrape_blocks_projection_until_the_failed_job_is_retried(tmp_path):
    import pytest

    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "blocked-projection.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-blocked", [_entry()])
    revisions.confirm("rev-blocked")
    runner = V4JobRunner(
        database,
        metadata_provider=lambda _target: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        runner.process_available(mirror_root=tmp_path / "mirror")

    assert runner.process_available(mirror_root=tmp_path / "mirror") == []
    with database.connect() as conn:
        statuses = {
            row["job_type"]: row["status"]
            for row in conn.execute(
                "SELECT job_type, status FROM jobs WHERE revision_id = 'rev-blocked'"
            )
        }
    assert statuses == {
        "materialize_mirror": "succeeded",
        "scrape_work": "failed",
        "refresh_projection": "queued",
    }


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


def test_special_episode_nfo_keeps_distinct_local_title_when_scraper_is_generic():
    from app.media_v4.jobs.metadata_artifacts import _episode_nfo

    payload = _episode_nfo(
        {
            "local_season_number": 0,
            "special_number": 2,
            "season_kind": "special",
            "display_title": "SP02 - 温泉小剧场",
            "title": "特别篇",
        }
    ).decode("utf-8")

    # SP 结构码由 special_number/episode 字段单独承担；NFO 标题只保留
    # 语义正文，避免播放器/前端把编号重复展示成 "SP02 SP02"。
    assert "<title>温泉小剧场</title>" in payload
    assert "<episode>2</episode>" in payload
    assert "<title>特别篇</title>" not in payload


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


def test_cancelling_a_revision_requests_safe_stop_for_running_job_and_cancels_dependents(tmp_path):
    """用户终止来源任务后，运行中任务在边界停止，后续 outbox 不再永久排队。"""

    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "cancel-revision.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-cancel", [_entry()])
    revisions.confirm("rev-cancel")
    with database.connect() as conn:
        mirror_job = conn.execute(
            "SELECT job_id FROM jobs WHERE revision_id = ? AND job_type = 'materialize_mirror'",
            ("rev-cancel",),
        ).fetchone()["job_id"]
        conn.execute(
            "UPDATE jobs SET status = 'running' WHERE job_id = ?",
            (mirror_job,),
        )

    result = V4JobRunner(database).cancel_revision("rev-cancel")

    assert result["revision_id"] == "rev-cancel"
    assert result["running"] == 1
    assert result["cancelled"] == 2
    with database.connect() as conn:
        rows = {
            row["job_type"]: dict(row)
            for row in conn.execute("SELECT job_type, status, cancel_requested FROM jobs WHERE revision_id = ?", ("rev-cancel",))
        }
    assert rows["materialize_mirror"]["status"] == "running"
    assert rows["materialize_mirror"]["cancel_requested"] == 1
    assert rows["scrape_work"]["status"] == "cancelled"
    assert rows["refresh_projection"]["status"] == "cancelled"


def test_stale_running_job_is_recoverable_instead_of_blocking_a_source_forever(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "stale-job.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-stale", [_entry()])
    revisions.confirm("rev-stale")
    with database.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', heartbeat_at = '2000-01-01T00:00:00+00:00' "
            "WHERE revision_id = ? AND job_type = 'materialize_mirror'",
            ("rev-stale",),
        )

    recovered = V4JobRunner(database).recover_stale_jobs(max_age_seconds=1)

    assert recovered == 1
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM jobs WHERE revision_id = ? AND job_type = 'materialize_mirror'",
            ("rev-stale",),
        ).fetchone()
    assert row["status"] == "failed"
    assert "异常中断" in row["last_error"]


def test_cancel_requested_queued_job_is_never_claimed_for_file_writes(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "cancel-before-claim.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-before-claim", [_entry()])
    revisions.confirm("rev-before-claim")
    with database.connect() as conn:
        job_id = conn.execute(
            "SELECT job_id FROM jobs WHERE revision_id = ? AND job_type = 'materialize_mirror'",
            ("rev-before-claim",),
        ).fetchone()["job_id"]
        conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE job_id = ?", (job_id,))

    result = V4JobRunner(database).process_job(job_id, mirror_root=tmp_path / "mirror")

    assert result.status == "cancelled"
    assert not list((tmp_path / "mirror").rglob("*.strm")) if (tmp_path / "mirror").exists() else True


def test_process_available_settles_a_queued_cancel_requested_job(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "cancel-available.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-cancel-available", [_entry()])
    revisions.confirm("rev-cancel-available")
    with database.connect() as conn:
        job_id = conn.execute(
            "SELECT job_id FROM jobs WHERE revision_id = ? AND job_type = 'materialize_mirror'",
            ("rev-cancel-available",),
        ).fetchone()["job_id"]
        conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE job_id = ?", (job_id,))

    results = V4JobRunner(database).process_available(mirror_root=tmp_path / "mirror")

    assert [(result.job_id, result.status) for result in results] == [(job_id, "cancelled")]
    with database.connect() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["status"] == "cancelled"
    assert not list((tmp_path / "mirror").rglob("*.strm")) if (tmp_path / "mirror").exists() else True


def test_running_cleanup_honors_a_termination_request_before_deleting_artifacts(tmp_path):
    from app.media_v4.jobs.cleanup import V4ArtifactCleanup
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "cancel-cleanup.db")
    database.initialize()
    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root-cleanup', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan-cleanup', 'root-cleanup', 1, 'completed');
            INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at)
            VALUES ('rev-cleanup', 'root-cleanup', 'scan-cleanup', 'v4', 'confirmed', 'now');
            INSERT INTO jobs(
                job_id, job_type, revision_id, status, cancel_requested,
                idempotency_key, created_at, updated_at
            ) VALUES (
                'cleanup-job', 'cleanup_superseded_artifacts', 'rev-cleanup', 'running', 1,
                'cleanup:rev-cleanup', 'now', 'now'
            );
            """
        )

    removed = V4ArtifactCleanup(database).process("cleanup-job", tmp_path / "mirror")

    assert removed == ()
    with database.connect() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE job_id = 'cleanup-job'").fetchone()
    assert row["status"] == "cancelled"
