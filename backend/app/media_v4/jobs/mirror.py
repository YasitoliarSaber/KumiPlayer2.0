"""V4 confirmed revision 的镜像物化器。"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.media_v4.jobs.paths import work_directory_name
from app.media_v4.path_validation import validate_playback_locator
from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip(" .")
    return cleaned or fallback


@dataclass(frozen=True, slots=True)
class MaterializeResult:
    status: str
    artifact_paths: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


class V4MirrorMaterializer:
    """只消费已确认 revision 的 Asset；不调用 parser 或 recognition。"""

    def __init__(self, database: V4Database):
        self.database = database

    def process(self, job_id: str, mirror_root: str | Path) -> MaterializeResult:
        root = Path(mirror_root)
        with self.database.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["job_type"] != "materialize_mirror":
                raise ValueError(f"不是镜像任务: {job['job_type']}")
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (job["revision_id"],),
            ).fetchone()
            if revision is None or revision["status"] != "confirmed":
                raise RuntimeError("只有 confirmed revision 才能生成镜像")
            if job["status"] == "succeeded":
                existing_paths = conn.execute(
                    """
                    SELECT target_path FROM artifacts
                    WHERE revision_id = ? AND work_id = ? AND artifact_type = 'mirror'
                    """,
                    (job["revision_id"], job["work_id"]),
                ).fetchall()
                return MaterializeResult("succeeded", tuple(row["target_path"] for row in existing_paths))
            rows = conn.execute(
                """
                SELECT DISTINCT
                    rb.revision_id, rb.work_id, rb.episode_id,
                    w.preferred_title,
                    s.local_season_number, s.season_kind,
                    e.local_episode_number, e.special_number,
                    a.asset_id, a.fingerprint, a.playback_locator, a.source_locator,
                    0 AS is_movie
                FROM revision_bindings rb
                JOIN works w ON w.work_id = rb.work_id
                JOIN episodes e ON e.episode_id = rb.episode_id
                JOIN seasons s ON s.season_id = e.season_id
                JOIN assets a ON a.asset_id = rb.asset_id
                WHERE rb.revision_id = ? AND rb.work_id = ?
                ORDER BY e.episode_id, a.asset_id
                """,
                (job["revision_id"], job["work_id"]),
            ).fetchall()
            movie_rows = conn.execute(
                """
                SELECT DISTINCT
                    rb.revision_id, rb.work_id, NULL AS episode_id,
                    w.preferred_title,
                    NULL AS local_season_number, '' AS season_kind,
                    NULL AS local_episode_number, NULL AS special_number,
                    a.asset_id, a.fingerprint, a.playback_locator, a.source_locator,
                    1 AS is_movie
                FROM revision_bindings rb
                JOIN works w ON w.work_id = rb.work_id
                JOIN assets a ON a.asset_id = rb.asset_id
                JOIN work_assets wa ON wa.work_id = rb.work_id AND wa.asset_id = rb.asset_id
                WHERE rb.revision_id = ? AND rb.work_id = ? AND rb.episode_id IS NULL
                ORDER BY a.asset_id
                """,
                (job["revision_id"], job["work_id"]),
            ).fetchall()
            rows = [*rows, *movie_rows]
            if not rows:
                raise RuntimeError("confirmed revision 没有可物化 Asset")
            cursor = conn.execute(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("镜像任务已由其他执行器领取")

        paths: list[str] = []
        try:
            # 写任何 .strm 前重新校验全部播放定位；任一不可达即整批失败，
            # 不发布任何 Artifact，也不让 scrape/projection 继续。
            for row in rows:
                locator = str(row["playback_locator"] or row["source_locator"] or "")
                ok, reason = validate_playback_locator(locator)
                if not ok:
                    raise RuntimeError(reason)
            for row in rows:
                work_dir = work_directory_name(str(row["work_id"]))
                asset_identity = row["fingerprint"] or row["asset_id"]
                asset_tag = _safe_segment(asset_identity[-10:], "asset")
                locator = row["playback_locator"] or row["source_locator"]
                if row["is_movie"]:
                    target = root / work_dir / f"movie-{asset_tag}.strm"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        temporary.write_text(locator, encoding="utf-8", newline="\n")
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    paths.append(str(target))
                    continue
                season_number = int(row["local_season_number"] or 0)
                if row["season_kind"] == "special" or season_number == 0:
                    season_dir = "Specials"
                    episode_name = f"SP{int(row['special_number'] or 1):02d}"
                else:
                    season_dir = f"Season {season_number:02d}"
                    episode_name = f"S{season_number:02d}E{int(row['local_episode_number'] or 0):02d}"
                target = root / work_dir / season_dir / f"{episode_name}-{asset_tag}.strm"
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_text(locator, encoding="utf-8", newline="\n")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                paths.append(str(target))
            with self.database.connect() as conn:
                now = _now()
                for path in paths:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO artifacts(
                            artifact_id, revision_id, work_id, artifact_type,
                            target_path, status, created_at, updated_at
                        ) VALUES (?, ?, ?, 'mirror', ?, 'published', ?, ?)
                        """,
                        (str(uuid.uuid4()), job["revision_id"], job["work_id"], path, now, now),
                    )
                conn.execute(
                    "UPDATE jobs SET status = 'succeeded', updated_at = ?, last_error = ? WHERE job_id = ?",
                    (now, "", job_id),
                )
            return MaterializeResult("succeeded", tuple(paths))
        except Exception as exc:
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE job_id = ?",
                    (str(exc), _now(), job_id),
                )
            raise
