"""V4 按 Work 去重的刮削任务。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.media_v4.domain.models import ResolvedWork
from app.media_v4.jobs.completeness import assess_metadata_completeness
from app.media_v4.jobs.control import cancel_requested, claim_running, heartbeat, mark_cancelled
from app.media_v4.jobs.metadata_artifacts import publish_metadata_artifacts
from app.media_v4.parsing.parser import show_type_from_import_family
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.resolution.candidates import work_identity_title_inputs


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _retain_successful_details(previous: dict, current: dict, target: dict) -> dict:
    """同批次同身份重试保留已成功资料；本次错误与完成状态仍由 current 决定。"""
    result = dict(current)
    if current.get("work_metadata_status") == "unavailable" and (
        previous.get("work_metadata_status") == "ready" or previous.get("metadata_state") == "ready"
    ):
        for key in (
            "title", "original_title", "year", "plot", "rating", "genres", "studios",
            "premiered", "runtime", "poster_url", "fanart_url", "clearlogo_url",
            "poster_file_path", "fanart_file_path", "clearlogo_file_path",
        ):
            if key in previous:
                result[key] = previous[key]
        result["work_metadata_status"] = "ready"
    valid_ids = {str(episode["episode_id"]) for episode in target["episodes"]}
    mappings = {
        str(mapping.get("episode_id") or ""): mapping
        for mapping in previous.get("episode_mappings") or []
        if isinstance(mapping, dict)
        and str(mapping.get("episode_id") or "") in valid_ids
        and str(mapping.get("provider_episode_id") or "").strip()
    }
    mappings.update({
        str(mapping.get("episode_id") or ""): mapping
        for mapping in current.get("episode_mappings") or []
        if str(mapping.get("provider_episode_id") or "").strip()
    })
    if mappings:
        result["episode_mappings"] = list(mappings.values())
        result["episode_mapping_status"] = "partial"
    return result


def _provider_binding_conflict(
    conn,
    *,
    work_id: str,
    work_type: str,
    provider: str,
    provider_id: str,
) -> str:
    """在写入前把 Provider 全局唯一约束转为可恢复的领域状态。"""

    media_type = "tv" if work_type == "series" else "movie"
    owner = conn.execute(
        "SELECT pb.work_id FROM provider_bindings pb "
        "JOIN works w ON w.work_id = pb.work_id "
        "WHERE pb.provider = ? AND pb.media_type = ? AND pb.provider_id = ? "
        "AND w.status = 'active'",
        (provider, media_type, provider_id),
    ).fetchone()
    if owner is not None and str(owner["work_id"]) != work_id:
        return "该在线作品已关联到另一部作品，请返回检查识别结果或选择正确候选"
    existing = conn.execute(
        "SELECT provider_id FROM provider_bindings WHERE work_id = ? AND provider = ? AND media_type = ?",
        (work_id, provider, media_type),
    ).fetchone()
    if existing is not None and str(existing["provider_id"]) != provider_id:
        return "当前作品已有不一致的在线身份，请返回检查识别结果后重新确认"
    return ""


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
                "SELECT DISTINCT rb.work_id FROM revision_bindings rb "
                "JOIN works w ON w.work_id = rb.work_id "
                "WHERE rb.revision_id = ? AND w.status = 'active' ORDER BY rb.work_id",
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
                "SELECT j.* FROM jobs j JOIN works w ON w.work_id = j.work_id "
                "WHERE j.revision_id = ? AND j.job_type = 'scrape_work' "
                "AND w.status = 'active' ORDER BY j.job_id",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def requeue_work(self, revision_id: str, work_id: str) -> dict:
        """仅重置一个已确认作品的刮削任务，供人工确认候选后继续执行。"""

        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None or revision["status"] != "confirmed":
                raise RuntimeError("只有 confirmed revision 才能重试刮削")
            work = conn.execute(
                "SELECT status FROM works WHERE work_id = ?", (work_id,)
            ).fetchone()
            if work is None or work["status"] != "active":
                raise RuntimeError("该作品已退出媒体库，不能重试刮削")
            bound = conn.execute(
                "SELECT 1 FROM revision_bindings WHERE revision_id = ? AND work_id = ? LIMIT 1",
                (revision_id, work_id),
            ).fetchone()
            if bound is None:
                raise KeyError((revision_id, work_id))
            job = conn.execute(
                "SELECT * FROM jobs WHERE revision_id = ? AND work_id = ? AND job_type = 'scrape_work'",
                (revision_id, work_id),
            ).fetchone()
            if job is None:
                raise RuntimeError("该作品缺少可重试的刮削任务")
            if job["status"] == "running":
                raise RuntimeError("该作品正在获取媒体信息")
            now = _now()
            conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', cancel_requested = 0, last_error = '',
                    heartbeat_at = '', started_at = '', finished_at = '', updated_at = ?
                WHERE job_id = ?
                """,
                (now, job["job_id"]),
            )
            refreshed = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job["job_id"],)
            ).fetchone()
        return dict(refreshed)

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
            work = conn.execute(
                "SELECT * FROM works WHERE work_id = ? AND status = 'active'",
                (job["work_id"],),
            ).fetchone()
            if work is None and job["work_id"]:
                raise RuntimeError("刮削任务对应的作品不存在或已退出媒体库")
            if job["status"] == "succeeded":
                return
        if not claim_running(self.database, job_id):
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return
            raise RuntimeError("刮削任务已由其他执行器领取")
        with self.database.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            target = dict(work) if work is not None else {"work_id": job["work_id"]}
            target["revision_id"] = job["revision_id"]
            # 沿 confirmed membership 传递原有标题证据，不能在任务里重新识别路径。
            facts = V4Repository(self.database).list_confirmed_work_facts(
                str(job["revision_id"]), str(job["work_id"]), conn=conn,
            )
            target["identity_titles"] = work_identity_title_inputs(
                ResolvedWork(
                    work_key=str(job["work_id"]),
                    preferred_title=str(target.get("preferred_title") or ""),
                    year=target.get("year"),
                    media_type="movie" if target.get("work_type") == "movie" else "tv",
                ),
                facts,
            )[:8]
            if not str(target.get("show_type") or "").strip():
                import_families = {
                    str(row["import_family"] or "").strip().casefold()
                    for row in conn.execute(
                        """
                        SELECT DISTINCT se.import_family
                        FROM revision_bindings rb
                        JOIN source_evidence se ON se.evidence_id = rb.evidence_id
                        WHERE rb.revision_id = ? AND rb.work_id = ?
                        """,
                        (job["revision_id"], job["work_id"]),
                    ).fetchall()
                    if str(row["import_family"] or "").strip()
                }
                if len(import_families) == 1:
                    target["show_type"] = show_type_from_import_family(
                        next(iter(import_families)),
                        str(target.get("work_type") or ""),
                    )
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
                           s.season_id, s.local_season_number, s.season_kind,
                           spm.provider_season_number, epm.provider_episode_number
                    FROM episodes e
                    JOIN seasons s ON s.season_id = e.season_id
                    LEFT JOIN season_provider_mappings spm
                      ON spm.season_id = s.season_id AND spm.provider = 'tmdb'
                    LEFT JOIN episode_provider_mappings epm
                      ON epm.episode_id = e.episode_id AND epm.provider = 'tmdb'
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
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return
            result = provider(target)
            heartbeat(self.database, job_id)
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return
            provider_name = str(result.get("provider") or "").strip()
            provider_id = str(result.get("provider_id") or "").strip()
            metadata_state = str(result.get("metadata_state") or "").strip()
            if not metadata_state:
                metadata_state = "ready" if provider_name != "local" else "waiting_metadata"
            has_provider_identity = provider_name not in {"", "local"} and bool(provider_id)
            binding_conflict = False
            ready = metadata_state == "ready" and has_provider_identity
            if has_provider_identity:
                with self.database.connect() as conn:
                    conflict_reason = _provider_binding_conflict(
                        conn,
                        work_id=str(job["work_id"]),
                        work_type=str(target.get("work_type") or ""),
                        provider=provider_name,
                        provider_id=provider_id,
                    )
                if conflict_reason:
                    ready = False
                    binding_conflict = True
                    result = {
                        **result,
                        "metadata_state": "waiting_review",
                        "reason": conflict_reason,
                        "reason_code": "provider_identity_conflict",
                    }
            if ready and mirror_root is None:
                # 完整性门控必经：镜像根从配置解析，不能由调用方是否传参决定。
                from app.core.paths import get_mirror_root

                try:
                    mirror_root = get_mirror_root()
                except Exception:
                    mirror_root = None
                if not mirror_root:
                    ready = False
                    result = {
                        **result,
                        "metadata_state": "waiting_metadata",
                        "reason": "镜像目录未配置，无法物化元数据产物",
                        "reason_code": "mirror_root_missing",
                    }
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
            if has_provider_identity and not binding_conflict and metadata_state in {
                "source_unavailable", "waiting_metadata", "failed",
            }:
                with self.database.connect() as conn:
                    previous_row = conn.execute(
                        "SELECT metadata_json FROM scrape_bindings "
                        "WHERE revision_id = ? AND work_id = ? AND provider = ? AND provider_id = ? "
                        "AND status != 'waiting_review'",
                        (job["revision_id"], job["work_id"], provider_name, provider_id),
                    ).fetchone()
                if previous_row is not None:
                    try:
                        previous = json.loads(previous_row["metadata_json"] or "{}")
                    except (TypeError, ValueError):
                        previous = {}
                    if isinstance(previous, dict):
                        result = _retain_successful_details(previous, result, target)
            if mirror_root and job["work_id"] and ready:
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
            binding_status = "confirmed" if ready else (
                str(result.get("metadata_state") or "") or "waiting_metadata"
            )
            result = {
                **result,
                "metadata_state": "ready" if ready else binding_status,
            }
            # 元数据尚未完整时仍保留已确认的 Provider 身份，下一次重试
            # 应直接沿用该 ID；只有身份冲突或本地结果才退回 local。
            identity_ready = has_provider_identity and not binding_conflict
            binding_provider = provider_name if identity_ready else "local"
            binding_provider_id = provider_id if identity_ready else ""
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return
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
                if identity_ready:
                    # D5：provider_bindings 的 (provider, media_type, provider_id)
                    # 全局唯一。该身份已被其他 work 占用时不得覆盖，也不得让
                    # UNIQUE 约束炸掉任务——记录明确错误，等待人工合并重复条目。
                    work_row = conn.execute(
                        "SELECT work_type FROM works WHERE work_id = ?",
                        (job["work_id"],),
                    ).fetchone()
                    binding_media_type = (
                        "tv" if str((work_row["work_type"] if work_row else "") or "") == "series" else "movie"
                    )
                    from app.media_v4.persistence.identity_lifecycle import (
                        release_inactive_provider_identity,
                    )

                    release_inactive_provider_identity(
                        conn,
                        binding_provider,
                        binding_media_type,
                        binding_provider_id,
                    )
                    owner = conn.execute(
                        "SELECT work_id FROM provider_bindings WHERE provider = ? AND media_type = ? AND provider_id = ?",
                        (binding_provider, binding_media_type, binding_provider_id),
                    ).fetchone()
                    if owner is None or str(owner["work_id"]) == str(job["work_id"]):
                        conn.execute(
                            """
                            INSERT INTO provider_bindings(work_id, provider, media_type, provider_id)
                            SELECT ?, ?, CASE WHEN work_type = 'series' THEN 'tv' ELSE 'movie' END, ?
                            FROM works WHERE work_id = ?
                            ON CONFLICT(work_id, provider, media_type) DO UPDATE SET
                                provider_id = excluded.provider_id
                            """,
                            (job["work_id"], binding_provider, binding_provider_id, job["work_id"]),
                        )
                    else:
                        # 预检查与真正写入之间可能有并发任务抢先占用身份。
                        # 这时必须把本次结果降级为待人工复核，并禁止继续写入
                        # 全局 Provider/Season/Episode 映射，避免把冲突身份扩散到
                        # 当前 Work 的播放链路。
                        identity_ready = False
                        binding_status = "waiting_review"
                        result = {
                            **result,
                            "metadata_state": "waiting_review",
                            "reason": "该在线作品已关联到另一部作品，请返回检查识别结果或选择正确候选",
                            "reason_code": "provider_identity_conflict",
                        }
                        conn.execute(
                            """
                            UPDATE scrape_bindings
                            SET status = 'waiting_review', metadata_json = ?, updated_at = ?
                            WHERE revision_id = ? AND work_id = ? AND provider = ?
                            """,
                            (
                                json.dumps(result, ensure_ascii=False, sort_keys=True),
                                now,
                                job["revision_id"],
                                job["work_id"],
                                binding_provider,
                            ),
                        )
                if identity_ready:
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
                    "UPDATE jobs SET status = 'succeeded', updated_at = ?, heartbeat_at = ?, finished_at = ?, last_error = '' WHERE job_id = ?",
                    (now, now, now, job_id),
                )
                # 每个 Work 刮削结果落库后都使投影失效。前端轮询会按需合并重建，
                # 因而已成功的作品无需等待整批任务结束即可进入对应作品页。
                conn.execute(
                    """
                    INSERT INTO v4_meta(key, value) VALUES ('library_projection_dirty', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (now,),
                )
        except Exception as exc:
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ?, heartbeat_at = ?, finished_at = ? WHERE job_id = ?",
                    (str(exc), _now(), _now(), _now(), job_id),
                )
            raise

    def list_bindings(self, revision_id: str) -> list[dict]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scrape_bindings WHERE revision_id = ? ORDER BY work_id, provider",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]
