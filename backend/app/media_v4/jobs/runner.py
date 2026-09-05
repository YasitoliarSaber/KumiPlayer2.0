"""V4 job runner：只消费已确认 revision，不重新识别来源。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.paths import get_mirror_root
from app.media_v4.jobs.cleanup import V4ArtifactCleanup
from app.media_v4.jobs.control import cancel_requested, claim_running, heartbeat, mark_cancelled, now
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

    # Work 级刮削会等待多个外部元数据请求；它们之间没有共享媒体写入，
    # 因此可以有限并发。保守上限避免大来源同时打满 TMDB 或 SQLite 写锁。
    _MAX_PARALLEL_SCRAPE_JOBS = 3

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
        if bool(job["cancel_requested"]):
            mark_cancelled(self.database, job_id)
            # 取消是用户可预期的终态，不应被后台 worker 或手动运行入口
            # 当作执行异常。这样“取队列后才收到终止请求”的竞态不会留下
            # 无意义的 409/错误日志，且仍保证不会领取或写入任何产物。
            return JobRunResult(job_id, job["job_type"], "cancelled")
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
            return JobRunResult(job_id, job["job_type"], self._current_status(job_id))

        if job["job_type"] == "cleanup_superseded_artifacts":
            self.cleanup.process(job_id, mirror_root or get_mirror_root())
            return JobRunResult(job_id, job["job_type"], self._current_status(job_id))

        if not claim_running(self.database, job_id):
            raise ValueError("任务已由其他执行器领取")
        try:
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return JobRunResult(job_id, job["job_type"], "cancelled")
            snapshot = self.projection.rebuild()
            heartbeat(self.database, job_id)
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return JobRunResult(job_id, job["job_type"], "cancelled")
            self._mark_succeeded(job_id)
            return JobRunResult(job_id, job["job_type"], "succeeded", snapshot=snapshot)
        except Exception as exc:
            self._mark_failed(job_id, str(exc))
            raise

    def process_available(self, *, mirror_root: str | Path | None = None) -> list[JobRunResult]:
        self.recover_stale_jobs()
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
        scrape_job_ids: list[str] = []
        deferred_job_ids: list[str] = []
        for row in rows:
            with self.database.connect() as conn:
                job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            if job is None:
                continue
            if bool(job["cancel_requested"]):
                # 队列快照和二次读取之间可能收到终止请求。正常取消路径会
                # 同时改写 status；这里仍须收口异常/恢复场景，不能静默跳过
                # 而留下永远排队的任务。复用 process_job 的取消分支，确保
                # 不会领取任务或产生任何文件写入。
                if job["status"] == "queued":
                    results.append(self.process_job(str(row["job_id"]), mirror_root=mirror_root))
                continue
            job_type = str(job["job_type"])
            if job_type in {"refresh_projection", "cleanup_superseded_artifacts"}:
                # 它们依赖本轮全部 scrape 的终态，必须等有限并发批次收口。
                deferred_job_ids.append(str(row["job_id"]))
                continue
            if not self._prerequisites_succeeded(dict(job)):
                continue
            if job_type == "scrape_work":
                scrape_job_ids.append(str(row["job_id"]))
                continue
            results.append(self.process_job(row["job_id"], mirror_root=mirror_root))

        if scrape_job_ids:
            workers = min(self._MAX_PARALLEL_SCRAPE_JOBS, len(scrape_job_ids))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v4-scrape") as executor:
                futures = [
                    executor.submit(self.process_job, job_id, mirror_root=mirror_root)
                    for job_id in scrape_job_ids
                ]
                # 按队列顺序读取结果，保持 API/测试的稳定可观察顺序；如果任一
                # 任务失败，executor 仍先安全等待已领取的同批任务退出。
                results.extend(future.result() for future in futures)

        for job_id in deferred_job_ids:
            with self.database.connect() as conn:
                job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                continue
            if bool(job["cancel_requested"]):
                if job["status"] == "queued":
                    results.append(self.process_job(job_id, mirror_root=mirror_root))
                continue
            if not self._prerequisites_succeeded(dict(job)):
                continue
            results.append(self.process_job(job_id, mirror_root=mirror_root))
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

    def cancel_revision(self, revision_id: str) -> dict[str, int | str]:
        """请求终止一个已确认导入；运行中任务在下一安全边界收口。"""

        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT revision_id FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            stamp = now()
            running = conn.execute(
                """
                UPDATE jobs
                SET cancel_requested = 1, heartbeat_at = ?, updated_at = ?
                WHERE revision_id = ? AND status = 'running'
                """,
                (stamp, stamp, revision_id),
            ).rowcount
            cancelled = conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', cancel_requested = 1, last_error = '',
                    heartbeat_at = ?, finished_at = ?, updated_at = ?
                WHERE revision_id = ? AND status = 'queued'
                """,
                (stamp, stamp, stamp, revision_id),
            ).rowcount
        return {"revision_id": revision_id, "running": int(running), "cancelled": int(cancelled)}

    def recover_stale_jobs(self, *, max_age_seconds: int = 120) -> int:
        """回收崩溃遗留的 running 行，避免来源卡永久显示进行中。"""

        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT job_id, cancel_requested, heartbeat_at, updated_at FROM jobs WHERE status = 'running'"
            ).fetchall()
        stale_ids: list[str] = []
        threshold = datetime.now(UTC).timestamp() - max(1, max_age_seconds)
        for row in rows:
            raw = str(row["heartbeat_at"] or row["updated_at"] or "")
            try:
                age = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                age = 0
            if age <= threshold:
                stale_ids.append(str(row["job_id"]))
        if not stale_ids:
            return 0
        stamp = now()
        with self.database.connect() as conn:
            for job_id in stale_ids:
                row = conn.execute(
                    "SELECT cancel_requested FROM jobs WHERE job_id = ? AND status = 'running'",
                    (job_id,),
                ).fetchone()
                if row is None:
                    continue
                if bool(row["cancel_requested"]):
                    conn.execute(
                        "UPDATE jobs SET status = 'cancelled', finished_at = ?, heartbeat_at = ?, updated_at = ? WHERE job_id = ?",
                        (stamp, stamp, stamp, job_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed', last_error = '任务异常中断，可重试',
                            finished_at = ?, heartbeat_at = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (stamp, stamp, stamp, job_id),
                    )
        return len(stale_ids)

    def _mark_running(self, job_id: str) -> bool:
        return claim_running(self.database, job_id)

    def _current_status(self, job_id: str) -> str:
        with self.database.connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return str(row["status"])

    def _mark_succeeded(self, job_id: str) -> None:
        stamp = now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'succeeded', last_error = '', heartbeat_at = ?, finished_at = ?, updated_at = ? "
                "WHERE job_id = ?",
                (stamp, stamp, stamp, job_id),
            )

    def _mark_failed(self, job_id: str, error: str) -> None:
        stamp = now()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', last_error = ?, heartbeat_at = ?, finished_at = ?, updated_at = ? "
                "WHERE job_id = ?",
                (error, stamp, stamp, stamp, job_id),
            )
