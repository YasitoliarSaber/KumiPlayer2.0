"""V4 job runner：只消费已确认 revision，不重新识别来源。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.paths import get_mirror_root
from app.media_v4.jobs.mirror import MaterializeResult, V4MirrorMaterializer
from app.media_v4.persistence.database import V4Database
from app.media_v4.projection.library import LibrarySnapshot, V4LibraryProjection


@dataclass(frozen=True, slots=True)
class JobRunResult:
    job_id: str
    job_type: str
    status: str
    snapshot: LibrarySnapshot | None = None
    materialized: MaterializeResult | None = None


class V4JobRunner:
    """执行 V4 outbox 中可以本地完成的任务。"""

    _LOCAL_JOB_TYPES = frozenset({"materialize_mirror", "refresh_projection"})

    def __init__(self, database: V4Database):
        self.database = database
        self.materializer = V4MirrorMaterializer(database)
        self.projection = V4LibraryProjection(database)

    def process_job(self, job_id: str, *, mirror_root: str | Path | None = None) -> JobRunResult:
        with self.database.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(job_id)
        if job["status"] == "succeeded":
            return JobRunResult(job_id, job["job_type"], "succeeded")
        if job["job_type"] not in self._LOCAL_JOB_TYPES:
            raise ValueError(f"任务需要显式 provider 执行: {job['job_type']}")

        if job["job_type"] == "materialize_mirror":
            materialized = self.materializer.process(job_id, mirror_root or get_mirror_root())
            return JobRunResult(job_id, job["job_type"], materialized.status, materialized=materialized)

        self._mark_running(job_id)
        try:
            snapshot = self.projection.rebuild()
            self._mark_succeeded(job_id)
            return JobRunResult(job_id, job["job_type"], "succeeded", snapshot=snapshot)
        except Exception as exc:
            self._mark_failed(job_id, str(exc))
            raise

    def process_available(self, *, mirror_root: str | Path | None = None) -> list[JobRunResult]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id
                FROM jobs
                WHERE status = 'queued' AND job_type IN ('materialize_mirror', 'refresh_projection')
                ORDER BY created_at, job_id
                """
            ).fetchall()
        results: list[JobRunResult] = []
        for row in rows:
            results.append(self.process_job(row["job_id"], mirror_root=mirror_root))
        return results

    def _mark_running(self, job_id: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = datetime('now') "
                "WHERE job_id = ? AND status = 'queued'",
                (job_id,),
            )

    def _mark_succeeded(self, job_id: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'succeeded', last_error = '', updated_at = datetime('now') "
                "WHERE job_id = ?",
                (job_id,),
            )

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = datetime('now') "
                "WHERE job_id = ?",
                (error, job_id),
            )
