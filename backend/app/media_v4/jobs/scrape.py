"""V4 按 Work 去重的刮削任务。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.media_v4.jobs.completeness import assess_metadata_completeness
from app.media_v4.jobs.metadata_artifacts import publish_metadata_artifacts
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

    def process(
        self,
        job_id: str,
        provider: Callable[[dict], dict],
        *,
        mirror_root=None,
    ) -> None:
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
            if work is None and job["work_id"]:
                raise RuntimeError("刮削任务对应的 Work 不存在")
            if job["status"] == "succeeded":
                return
            cursor = conn.execute(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("刮削任务已由其他执行器领取")
            target = dict(work) if work is not None else {"work_id": job["work_id"]}
            target["revision_id"] = job["revision_id"]
            target["provider_bindings"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT provider, media_type, provider_id FROM provider_bindings "
                    "WHERE work_id = ? ORDER BY provider, media_type",
                    (job["work_id"],),
                ).fetchall()
            ]
            target["episodes"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT e.episode_id, e.local_episode_number, e.absolute_episode_number,
                           e.special_number, e.episode_kind, e.display_title,
                           s.season_id, s.local_season_number, s.season_kind
                    FROM episodes e
                    JOIN seasons s ON s.season_id = e.season_id
                    WHERE e.work_id = ? AND EXISTS (
                        SELECT 1 FROM revision_bindings rb
                        WHERE rb.revision_id = ? AND rb.episode_id = e.episode_id
                    )
                    ORDER BY s.local_season_number, e.local_episode_number, e.episode_id
                    """,
                    (job["work_id"], job["revision_id"]),
                ).fetchall()
            ]

        try:
            result = provider(target)
            provider_name = str(result.get("provider") or "").strip()
            provider_id = str(result.get("provider_id") or "").strip()
            metadata_state = str(result.get("metadata_state") or "").strip()
            if not metadata_state:
                metadata_state = "ready" if provider_name != "local" else "waiting_metadata"
            ready = metadata_state == "ready" and provider_name not in {"", "local"} and bool(provider_id)
            valid_episode_ids = {str(item["episode_id"]) for item in target["episodes"]}
            for mapping in result.get("episode_mappings") or []:
                episode_id = str(mapping.get("episode_id") or "")
                if episode_id not in valid_episode_ids:
                    raise ValueError("刮削结果包含不属于当前 revision 的 Episode 映射")
            valid_season_ids = {str(item["season_id"]) for item in target["episodes"]}
            for mapping in result.get("season_mappings") or []:
                season_id = str(mapping.get("season_id") or "")
                if season_id not in valid_season_ids:
                    raise ValueError("刮削结果包含不属于当前 revision 的 Season 映射")
            if mirror_root is not None and job["work_id"] and ready:
                publish_metadata_artifacts(
                    self.database,
                    revision_id=job["revision_id"],
                    work_id=job["work_id"],
                    target=target,
                    metadata=result,
                    mirror_root=mirror_root,
                )
                # P-001 7.7 R3：完整性是 ready 的硬门控。
                complete, reasons = assess_metadata_completeness(
                    self.database,
                    revision_id=job["revision_id"],
                    work_id=job["work_id"],
                    target=target,
                    metadata=result,
                    mirror_root=mirror_root,
                )
                if not complete:
                    result = {
                        **result,
                        "metadata_state": "failed",
                        "reason": "；".join(reasons),
                        "completeness": reasons,
                    }
                    ready = False
            now = _now()
            binding_status = "confirmed" if ready else (metadata_state or "waiting_metadata")
            binding_provider = provider_name if ready else "local"
            binding_provider_id = provider_id if ready else ""
            with self.database.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO scrape_bindings(
                        binding_id, revision_id, work_id, provider, provider_id,
                        metadata_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(revision_id, work_id, provider) DO UPDATE SET
                        provider_id = excluded.provider_id,
                        metadata_json = excluded.metadata_json,
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(uuid.uuid4()),
                        job["revision_id"],
                        job["work_id"],
                        binding_provider,
                        binding_provider_id,
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        binding_status,
                        now,
                        now,
                    ),
                )
                if ready:
                    conn.execute(
                        """
                        INSERT INTO provider_bindings(work_id, provider, media_type, provider_id)
                        SELECT ?, ?, CASE WHEN work_type = 'series' THEN 'tv' ELSE 'movie' END, ?
                        FROM works WHERE work_id = ?
                        ON CONFLICT(work_id, provider, media_type) DO UPDATE SET
                            provider_id = excluded.provider_id
                        """,
                        (job["work_id"], provider_name, provider_id, job["work_id"]),
                    )
                for mapping in result.get("episode_mappings") or []:
                    episode_id = str(mapping.get("episode_id") or "")
                    conn.execute(
                        """
                        INSERT INTO episode_provider_mappings(
                            episode_id, provider, provider_season_number,
                            provider_episode_number, provider_episode_id
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(episode_id, provider) DO UPDATE SET
                            provider_season_number = excluded.provider_season_number,
                            provider_episode_number = excluded.provider_episode_number,
                            provider_episode_id = excluded.provider_episode_id
                        """,
                        (
                            episode_id,
                            provider_name,
                            mapping.get("provider_season_number"),
                            mapping.get("provider_episode_number"),
                            str(mapping.get("provider_episode_id") or ""),
                        ),
                    )
                for mapping in result.get("season_mappings") or []:
                    season_id = str(mapping.get("season_id") or "")
                    conn.execute(
                        """
                        INSERT INTO season_provider_mappings(
                            season_id, provider, provider_season_number, provider_season_id
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(season_id, provider) DO UPDATE SET
                            provider_season_number = excluded.provider_season_number,
                            provider_season_id = excluded.provider_season_id
                        """,
                        (
                            season_id,
                            provider_name,
                            mapping.get("provider_season_number"),
                            str(mapping.get("provider_season_id") or ""),
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
