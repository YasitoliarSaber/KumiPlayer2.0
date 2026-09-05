"""V4 任务门面只暴露 jobs 表。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_tasks_facade_reads_v4_jobs_without_legacy_manager(tmp_path, monkeypatch):
    from app.api import media_v4, tasks_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "tasks.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(tasks_v4.router)
    client = TestClient(application)

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
            VALUES ('job', 'refresh_projection', 'rev', 'refresh:rev', 'now', 'now');
            """
        )

    listed = client.get("/api/tasks")
    assert listed.status_code == 200
    assert listed.json()["tasks"][0]["task_id"] == "job"
    assert listed.json()["tasks"][0]["status"] == "pending"
    assert listed.json()["tasks"][0]["message"] == "等待任务执行"

    cancelled = client.post("/api/tasks/job/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["message"] == "任务已终止"


def test_running_task_requests_safe_termination_and_cancels_its_revision(tmp_path, monkeypatch):
    from app.api import media_v4, tasks_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "running-task.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(tasks_v4.router)
    client = TestClient(application)

    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan', 'root', 1, 'completed');
            INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at)
            VALUES ('rev', 'root', 'scan', 'fixture', 'confirmed', 'now');
            INSERT INTO jobs(job_id, job_type, revision_id, status, idempotency_key, created_at, updated_at)
            VALUES ('job', 'refresh_projection', 'rev', 'running', 'refresh:rev', 'now', 'now');
            """
        )

    response = client.post("/api/tasks/job/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["cancel_requested"] is True
    assert response.json()["message"] == "正在终止"
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, cancel_requested FROM jobs WHERE job_id = 'job'"
        ).fetchone()
    assert row["status"] == "running"
    assert row["cancel_requested"] == 1


def test_failed_task_can_be_explicitly_requeued(tmp_path, monkeypatch):
    from app.api import media_v4, tasks_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "retry-task.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(tasks_v4.router)
    client = TestClient(application)
    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan', 'root', 1, 'completed');
            INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at)
            VALUES ('rev', 'root', 'scan', 'fixture', 'confirmed', 'now');
            INSERT INTO jobs(job_id, job_type, revision_id, status, last_error, idempotency_key, created_at, updated_at)
            VALUES ('job', 'refresh_projection', 'rev', 'failed', 'temporary', 'refresh:rev', 'now', 'now');
            """
        )

    response = client.post("/api/tasks/job/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["cancel_requested"] is False
    assert response.json()["error"] == ""
    assert response.json()["message"] == "等待任务执行"


def test_tasks_facade_hides_technical_job_error_details(tmp_path, monkeypatch):
    from app.api import media_v4, tasks_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "task-error-projection.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(tasks_v4.router)
    client = TestClient(application)
    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan', 'root', 1, 'completed');
            INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at)
            VALUES ('rev', 'root', 'scan', 'fixture', 'confirmed', 'now');
            INSERT INTO jobs(
                job_id, job_type, revision_id, status, last_error,
                idempotency_key, created_at, updated_at
            ) VALUES (
                'job', 'refresh_projection', 'rev', 'failed',
                'sqlite3.IntegrityError: UNIQUE constraint failed: provider_bindings.provider_id',
                'refresh:rev', 'now', 'now'
            );
            """
        )

    response = client.get("/api/tasks/job")

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "媒体身份与已有记录冲突，请检查识别结果后重试"
    assert payload["error"] == "媒体身份与已有记录冲突，请检查识别结果后重试"
    assert "IntegrityError" not in payload["message"]
    assert "provider_bindings" not in payload["error"]


def test_cancelled_task_can_be_explicitly_requeued(tmp_path, monkeypatch):
    from app.api import media_v4, tasks_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "retry-cancelled-task.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(tasks_v4.router)
    client = TestClient(application)
    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan', 'root', 1, 'completed');
            INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at)
            VALUES ('rev', 'root', 'scan', 'fixture', 'confirmed', 'now');
            INSERT INTO jobs(
                job_id, job_type, revision_id, status, cancel_requested,
                idempotency_key, created_at, updated_at
            ) VALUES ('job', 'refresh_projection', 'rev', 'cancelled', 1, 'refresh:rev', 'now', 'now');
            """
        )

    response = client.post("/api/tasks/job/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["cancel_requested"] is False


def test_retrying_a_cancelled_task_restores_its_cancelled_revision_dependents(tmp_path, monkeypatch):
    from app.api import media_v4, tasks_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "retry-cancelled-revision.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(tasks_v4.router)
    client = TestClient(application)
    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan', 'root', 1, 'completed');
            INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at)
            VALUES ('rev', 'root', 'scan', 'fixture', 'confirmed', 'now');
            INSERT INTO jobs(
                job_id, job_type, revision_id, work_id, status, cancel_requested,
                idempotency_key, created_at, updated_at
            ) VALUES
                ('mirror', 'materialize_mirror', 'rev', 'work', 'succeeded', 0, 'mirror:rev:work', 'now', 'now'),
                ('scrape', 'scrape_work', 'rev', 'work', 'cancelled', 1, 'scrape:rev:work', 'now', 'now'),
                ('projection', 'refresh_projection', 'rev', '', 'cancelled', 1, 'projection:rev', 'now', 'now');
            """
        )

    response = client.post("/api/tasks/scrape/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    with database.connect() as conn:
        statuses = {
            row["job_id"]: (row["status"], row["cancel_requested"])
            for row in conn.execute(
                "SELECT job_id, status, cancel_requested FROM jobs WHERE revision_id = 'rev'"
            )
        }
    assert statuses == {
        "mirror": ("succeeded", 0),
        "scrape": ("queued", 0),
        "projection": ("queued", 0),
    }
