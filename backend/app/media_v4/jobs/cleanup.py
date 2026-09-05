"""清理已经被新 revision 取代、且不再被当前图引用的生成产物。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.media_v4.jobs.control import cancel_requested, claim_running, heartbeat, mark_cancelled
from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class V4ArtifactCleanup:
    def __init__(self, database: V4Database):
        self.database = database

    def process(self, job_id: str, mirror_root: str | Path) -> tuple[str, ...]:
        root = Path(mirror_root).resolve(strict=False)
        with self.database.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["job_type"] != "cleanup_superseded_artifacts":
                raise ValueError(f"不是清理任务: {job['job_type']}")
            if job["status"] == "succeeded":
                return ()
            prerequisites = conn.execute(
                """
                SELECT status FROM jobs
                WHERE revision_id = ? AND job_id != ?
                  AND job_type IN ('materialize_mirror', 'scrape_work', 'refresh_projection')
                """,
                (job["revision_id"], job_id),
            ).fetchall()
            if any(row["status"] != "succeeded" for row in prerequisites):
                raise RuntimeError("当前 revision 尚未完整发布，拒绝清理旧产物")
            rows = conn.execute(
                """
                SELECT a.artifact_id, a.target_path
                FROM artifacts a
                JOIN import_revisions old ON old.revision_id = a.revision_id
                JOIN import_revisions current ON current.revision_id = ?
                WHERE old.root_id = current.root_id AND old.status = 'superseded'
                  AND NOT EXISTS (
                      SELECT 1 FROM artifacts active_artifact
                      JOIN import_revisions active_revision
                        ON active_revision.revision_id = active_artifact.revision_id
                      WHERE active_revision.status = 'confirmed'
                        AND active_artifact.target_path = a.target_path
                  )
                ORDER BY a.target_path
                """,
                (job["revision_id"],),
            ).fetchall()

        if not claim_running(self.database, job_id):
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return ()
            raise RuntimeError("清理任务已由其他执行器领取")

        removed: list[str] = []
        removable_ids: list[str] = []
        try:
            for row in rows:
                if cancel_requested(self.database, job_id):
                    mark_cancelled(self.database, job_id)
                    return tuple(removed)
                path = Path(row["target_path"])
                resolved = path.resolve(strict=False)
                if resolved == root or root not in resolved.parents:
                    raise RuntimeError(f"旧产物越出受管镜像目录，拒绝清理: {path}")
                if path.exists() or path.is_symlink():
                    if path.is_dir() and not path.is_symlink():
                        raise RuntimeError(f"产物记录意外指向目录，拒绝清理: {path}")
                    path.unlink()
                    removed.append(str(path))
                    parent = path.parent
                    while parent != root and root in parent.parents:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                removable_ids.append(str(row["artifact_id"]))
                heartbeat(self.database, job_id)
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return tuple(removed)
            with self.database.connect() as conn:
                conn.executemany(
                    "DELETE FROM artifacts WHERE artifact_id = ?",
                    [(artifact_id,) for artifact_id in removable_ids],
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'succeeded', last_error = '', updated_at = ?, heartbeat_at = ?, finished_at = ?
                    WHERE job_id = ?
                    """,
                    (_now(), _now(), _now(), job_id),
                )
            return tuple(removed)
        except Exception as exc:
            with self.database.connect() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', last_error = ?, updated_at = ?, heartbeat_at = ?, finished_at = ?
                    WHERE job_id = ?
                    """,
                    (str(exc), _now(), _now(), _now(), job_id),
                )
            raise
