"""V4 按 Work 去重的刮削任务。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class V4ScrapeService:
    """刮削只写 provider binding，不覆盖本地作品图编号。"""

    def __init__(self, database: V4Database):
        self.database = database

    def enqueue_for_revision(self, revision_id: str) -> list[dict]:
        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            if revision["status"] != "confirmed":
                raise RuntimeError("只有 confirmed revision 才能创建刮削任务")
            work_rows = conn.execute(
                "SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id = ? ORDER BY work_id",
                (revision_id,),
            ).fetchall()
            now = _now()
            for row in work_rows:
                work_id = row["work_id"]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs(
                        job_id, job_type, revision_id, work_id, idempotency_key,
                        created_at, updated_at
                    ) VALUES (?, 'scrape_work', ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), revision_id, work_id, f"scrape_work:{revision_id}:{work_id}", now, now),
                )
            rows = conn.execute(
                "SELECT * FROM jobs WHERE revision_id = ? AND job_type = 'scrape_work' ORDER BY job_id",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def process(self, job_id: str, provider: Callable[[dict], dict]) -> None:
        with self.database.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["job_type"] != "scrape_work":
                raise ValueError(f"不是刮削任务: {job['job_type']}")
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (job["revision_id"],),
            ).fetchone()
            if revision is None or revision["status"] != "confirmed":
                raise RuntimeError("只有 confirmed revision 才能执行刮削")
            work = conn.execute("SELECT * FROM works WHERE work_id = ?", (job["work_id"],)).fetchone()
            if work is None:
                raise RuntimeError("刮削任务对应的 Work 不存在")
            if job["status"] == "succeeded":
                return
            conn.execute("UPDATE jobs SET status = 'running', updated_at = ? WHERE job_id = ?", (_now(), job_id))

        try:
            result = provider(dict(work))
            provider_name = str(result.get("provider") or "").strip()
            provider_id = str(result.get("provider_id") or "").strip()
            if not provider_name or not provider_id:
                raise ValueError("刮削结果缺少 provider/provider_id")
            now = _now()
            with self.database.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO scrape_bindings(
                        binding_id, revision_id, work_id, provider, provider_id,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(revision_id, work_id, provider) DO UPDATE SET
                        provider_id = excluded.provider_id,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(uuid.uuid4()),
                        job["revision_id"],
                        job["work_id"],
                        provider_name,
                        provider_id,
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE jobs SET status = 'succeeded', updated_at = ?, last_error = '' WHERE job_id = ?",
                    (now, job_id),
                )
        except Exception as exc:
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ?",
                    (str(exc), _now(), job_id),
                )
            raise

    def list_bindings(self, revision_id: str) -> list[dict]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scrape_bindings WHERE revision_id = ? ORDER BY work_id, provider",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]
