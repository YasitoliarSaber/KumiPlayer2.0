"""V4 job runner：只消费已确认 revision，不重新识别来源。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import get_mirror_root
from app.media_v4.jobs.cleanup import V4ArtifactCleanup
from app.media_v4.jobs.metadata import default_metadata_provider
from app.media_v4.jobs.mirror import MaterializeResult, V4MirrorMaterializer
from app.media_v4.jobs.scrape import V4ScrapeService
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

    _LOCAL_JOB_TYPES = frozenset({
        "materialize_mirror",
        "scrape_work",
        "refresh_projection",
        "cleanup_superseded_artifacts",
    })

    def __init__(self, database: V4Database, metadata_provider: Callable[[dict], dict] | None = None):
        self.database = database
        self.materializer = V4MirrorMaterializer(database)
        self.cleanup = V4ArtifactCleanup(database)
        self.scrape = V4ScrapeService(database)
        self.projection = V4LibraryProjection(database)
        self.metadata_provider = metadata_provider or default_metadata_provider

    def process_job(self, job_id: str, *, mirror_root: str | Path | None = None) -> JobRunResult:
        with self.database.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise KeyError(job_id)
        if job["status"] == "succeeded":
            return JobRunResult(job_id, job["job_type"], "succeeded")
        if job["status"] != "queued":
            raise ValueError(f"任务状态不可执行: {job['status']}")
        if job["job_type"] not in self._LOCAL_JOB_TYPES:
            raise ValueError(f"未知 V4 任务: {job['job_type']}")
        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (job["revision_id"],),
            ).fetchone()
        if revision is None or revision["status"] != "confirmed":
            raise ValueError("只有 confirmed revision 的排队任务可以执行")
        if not self._prerequisites_succeeded(dict(job)):
            raise ValueError("任务的前置步骤尚未全部成功")

        if job["job_type"] == "materialize_mirror":
            materialized = self.materializer.process(job_id, mirror_root or get_mirror_root())
            return JobRunResult(job_id, job["job_type"], materialized.status, materialized=materialized)

        if job["job_type"] == "scrape_work":
            self.scrape.process(
                job_id,
                self.metadata_provider,
                mirror_root=mirror_root or get_mirror_root(),
            )
            return JobRunResult(job_id, job["job_type"], "succeeded")

        if job["job_type"] == "cleanup_superseded_artifacts":
            self.cleanup.process(job_id, mirror_root or get_mirror_root())
            return JobRunResult(job_id, job["job_type"], "succeeded")

        if not self._mark_running(job_id):
            raise ValueError("任务已由其他执行器领取")
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
                WHERE status = 'queued'
                  AND job_type IN (
                      'materialize_mirror', 'scrape_work', 'refresh_projection',
                      'cleanup_superseded_artifacts'
                  )
                ORDER BY CASE job_type
                    WHEN 'materialize_mirror' THEN 1
                    WHEN 'scrape_work' THEN 2
                    WHEN 'refresh_projection' THEN 3
                    WHEN 'cleanup_superseded_artifacts' THEN 4
                    ELSE 9 END,
                    created_at, job_id
                """
            ).fetchall()
        results: list[JobRunResult] = []
        for row in rows:
            with self.database.connect() as conn:
                job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            if job is None or not self._prerequisites_succeeded(dict(job)):
                continue
            results.append(self.process_job(row["job_id"], mirror_root=mirror_root))
        return results

    def _prerequisites_succeeded(self, job: dict) -> bool:
        job_type = str(job["job_type"])
        if job_type == "materialize_mirror":
            return True
        params: tuple[object, ...]
        if job_type == "scrape_work":
            predicate = "job_type = 'materialize_mirror' AND work_id = ?"
            params = (job["revision_id"], job.get("work_id"))
        elif job_type == "refresh_projection":
            predicate = "job_type IN ('materialize_mirror', 'scrape_work')"
            params = (job["revision_id"],)
        elif job_type == "cleanup_superseded_artifacts":
            predicate = "job_type IN ('materialize_mirror', 'scrape_work', 'refresh_projection')"
            params = (job["revision_id"],)
        else:
            return False
        with self.database.connect() as conn:
            blocked = conn.execute(
                f"SELECT 1 FROM jobs WHERE revision_id = ? AND {predicate} "
                "AND status != 'succeeded' LIMIT 1",
                params,
            ).fetchone()
        return blocked is None

    def _mark_running(self, job_id: str) -> bool:
        with self.database.connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = datetime('now') "
                "WHERE job_id = ? AND status = 'queued'",
                (job_id,),
            )
        return cursor.rowcount == 1

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
