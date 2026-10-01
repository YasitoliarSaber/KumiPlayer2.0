"""批次末尾恢复的离线持久作业合同。"""

import json

from app.core.config import AppConfig
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


def prepare(tmp_path, monkeypatch):
    config = AppConfig(artwork_storage_mode="remote")
    monkeypatch.setattr("app.media_v4.jobs.metadata_artifacts.load_config", lambda: config)
    monkeypatch.setattr("app.media_v4.jobs.completeness.load_config", lambda: config)
    database = V4Database(tmp_path / "recovery.db")
    database.initialize()
    entries = []
    for title in ("Alpha", "Beta"):
        evidence = to_source_evidence(SourceEntry(
            root_id="fixture-root", scan_id="r1", provider="local", ingest_method="directory_tree",
            relative_path=f"{title}/{title}.S01E01.mkv", playback_locator=f"Z:/fixture/{title}.mkv",
        ))
        entries.append((evidence, V4Parser().parse(evidence)))
    service = V4RevisionService(database)
    service.create_draft("r1", entries)
    service.confirm("r1")
    return database, service


def no_candidates(_target):
    return {"provider": "local", "metadata_state": "waiting_review", "reason_code": "no_candidates"}


def test_recovery_after_all_first_pass_and_once_across_runner_restart(tmp_path, monkeypatch):
    database, service = prepare(tmp_path, monkeypatch)
    calls = []
    def first(target):
        calls.append(("first", target["work_id"]))
        return no_candidates(target)
    def recover(target):
        with database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='scrape_work' AND status IN ('queued','running')").fetchone()[0] == 0
        calls.append(("recover", target["work_id"]))
        return no_candidates(target)
    runner = V4JobRunner(database, metadata_provider=first, recovery_provider=recover)
    runner.process_available(mirror_root=tmp_path / "mirror")
    assert [kind for kind, _ in calls] == ["first", "first", "recover", "recover"]
    V4JobRunner(database, metadata_provider=first, recovery_provider=recover).process_available(mirror_root=tmp_path / "mirror")
    assert len(calls) == 4
    with database.connect() as conn:
        jobs = conn.execute("SELECT * FROM jobs WHERE job_type='recover_work_aliases'").fetchall()
        assert len(jobs) == 2
        assert all(job["status"] == "succeeded" for job in jobs)
    assert service.get_execution_progress("r1")["overall_status"] == "needs_attention"


def test_pending_recovery_blocks_projection_and_cancel_does_not_resurrect(tmp_path, monkeypatch):
    database, _service = prepare(tmp_path, monkeypatch)
    runner = V4JobRunner(database, metadata_provider=no_candidates, recovery_provider=no_candidates)
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status='succeeded',result_json=? WHERE job_type IN ('materialize_mirror','scrape_work')",
                     (json.dumps({"reason_codes": ["no_candidates"]}),))
    runner.schedule_recovery()
    with database.connect() as conn:
        projection = dict(conn.execute("SELECT * FROM jobs WHERE job_type='refresh_projection'").fetchone())
    assert not runner._prerequisites_succeeded(projection)
    runner.cancel_revision("r1")
    runner.schedule_recovery()
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] == 0


def test_auth_and_episode_failures_do_not_enter_alias_recovery(tmp_path, monkeypatch):
    database, _service = prepare(tmp_path, monkeypatch)
    def first(target):
        code = "provider_auth_required" if target["preferred_title"] == "Alpha" else "episode_mapping_incomplete"
        return {"provider": "local", "metadata_state": "waiting_metadata", "reason_code": code}
    def denied(_target):
        raise AssertionError("ineligible recovery")
    V4JobRunner(database, metadata_provider=first, recovery_provider=denied).process_available(mirror_root=tmp_path / "mirror")
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type='recover_work_aliases'").fetchone()[0] == 0


def test_stale_recovery_resumes_same_job_with_bounded_attempts(tmp_path, monkeypatch):
    database, _service = prepare(tmp_path, monkeypatch)
    runner = V4JobRunner(database, metadata_provider=no_candidates, recovery_provider=no_candidates)
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status='succeeded',result_json=? WHERE job_type IN ('materialize_mirror','scrape_work')",
                     (json.dumps({"reason_codes": ["no_candidates"]}),))
    runner.schedule_recovery()
    with database.connect() as conn:
        job = conn.execute("SELECT job_id FROM jobs WHERE job_type='recover_work_aliases' LIMIT 1").fetchone()[0]
        conn.execute("UPDATE jobs SET status='running',attempts=1,heartbeat_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job,))
    runner.recover_stale_jobs()
    with database.connect() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()[0] == "queued"


def test_provider_cooldown_is_persisted_and_does_not_repeat_request(tmp_path, monkeypatch):
    from app.media_v4.jobs import runner as module
    database, _service = prepare(tmp_path, monkeypatch)
    calls = []
    def deferred(_target, **_kwargs):
        calls.append(1)
        return {**no_candidates(_target), 'alias_deferred_seconds': 60}
    monkeypatch.setattr(module, 'recover_metadata', deferred)
    runner = V4JobRunner(database, metadata_provider=no_candidates, recovery_provider=deferred)
    runner.process_available(mirror_root=tmp_path / 'mirror')
    assert len(calls) == 2
    with database.connect() as conn:
        jobs = conn.execute("SELECT status,result_json FROM jobs WHERE job_type='recover_work_aliases'").fetchall()
        assert all(job['status'] == 'queued' and json.loads(job['result_json'])['next_retry_at'] for job in jobs)
    runner.process_available(mirror_root=tmp_path / 'mirror')
    assert len(calls) == 2
