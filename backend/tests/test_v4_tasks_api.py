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

    cancelled = client.post("/api/tasks/job/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
