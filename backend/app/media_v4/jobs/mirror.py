"""V4 confirmed revision 的镜像物化器。"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.media_v4.jobs.control import cancel_requested, claim_running, heartbeat, mark_cancelled
from app.media_v4.jobs.paths import work_directory_name
from app.media_v4.path_validation import validate_playback_locator, validate_playback_locator_syntax
from app.media_v4.persistence.database import V4Database

# 这些来源只提供目录树/清单，不能在镜像阶段通过挂载盘探测文件存在性。
# ``openlist`` / ``openlist_scan`` 是历史证据中的兼容值，``openlist_api``
# 是当前扫描器写入的值；三者都必须走同一条“只做语法校验”分支。
_LIST_ONLY_INGEST_METHODS = frozenset({
    "directory_tree",
    "txt_tree",
    "txt_snapshot",
    "openlist",
    "openlist_api",
    "openlist_scan",
})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip(" .")
    return cleaned or fallback


def _sample_locators(locators: list[str]) -> list[str]:
    """头/中/尾有界抽样，最多 3 个，避免大库逐 Asset 访问挂载盘。"""

    if not locators:
        return []
    if len(locators) <= 3:
        return locators
    indexes = sorted({0, len(locators) - 1, len(locators) // 2})
    return [locators[index] for index in indexes]


def _remove_created_paths(paths: list[Path]) -> None:
    """只回收本次成功发布的文件，绝不碰任务开始前已存在的目标。"""

    for path in paths:
        path.unlink(missing_ok=True)


def _existing_target_matches(target: Path, locator: str) -> bool:
    try:
        return target.read_text(encoding="utf-8-sig") == locator
    except (OSError, UnicodeError):
        return False


def _publish_target(target: Path, locator: str, created_paths: list[Path]) -> None:
    """以不覆盖既有文件的方式发布一个 .strm，并记录本轮新建文件。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not _existing_target_matches(target, locator):
            raise RuntimeError(f"镜像目标已存在且内容不一致: {target}")
        return

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(locator, encoding="utf-8", newline="\n")
        try:
            # hard link 在同目录内创建目标，遇到同名目标会失败而不会覆盖它。
            os.link(temporary, target)
        except FileExistsError:
            if not _existing_target_matches(target, locator):
                raise RuntimeError(f"镜像目标已存在且内容不一致: {target}") from None
            return
        created_paths.append(target)
    finally:
        temporary.unlink(missing_ok=True)


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
                    COALESCE(se.ingest_method, '') AS ingest_method,
                    0 AS is_movie
                FROM revision_bindings rb
                JOIN works w ON w.work_id = rb.work_id
                JOIN episodes e ON e.episode_id = rb.episode_id
                JOIN seasons s ON s.season_id = e.season_id
                JOIN assets a ON a.asset_id = rb.asset_id
                LEFT JOIN source_evidence se ON se.evidence_id = rb.evidence_id
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
                    COALESCE(se.ingest_method, '') AS ingest_method,
                    1 AS is_movie
                FROM revision_bindings rb
                JOIN works w ON w.work_id = rb.work_id
                JOIN assets a ON a.asset_id = rb.asset_id
                JOIN work_assets wa ON wa.work_id = rb.work_id AND wa.asset_id = rb.asset_id
                LEFT JOIN source_evidence se ON se.evidence_id = rb.evidence_id
                WHERE rb.revision_id = ? AND rb.work_id = ? AND rb.episode_id IS NULL
                ORDER BY a.asset_id
                """,
                (job["revision_id"], job["work_id"]),
            ).fetchall()
            rows = [*rows, *movie_rows]
            if not rows:
                raise RuntimeError("confirmed revision 没有可物化 Asset")
        if not claim_running(self.database, job_id):
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return MaterializeResult("cancelled")
            raise RuntimeError("镜像任务已由其他执行器领取")

        paths: list[str] = []
        created_paths: list[Path] = []
        try:
            # 写任何 .strm 前，非 TXT 资产按头/中/尾有界抽样复核可达性；样本任一
            # 不可达即整批失败，不发布任何 Artifact。TXT 资产不做源盘探测，由
            # 下方逐条纯语法校验兜底。取消检查点保持原有节奏。
            non_txt_locators = [
                str(row["playback_locator"] or row["source_locator"] or "")
                for row in rows
                if str(row["ingest_method"] or "") not in _LIST_ONLY_INGEST_METHODS
            ]
            for locator in _sample_locators(non_txt_locators):
                if cancel_requested(self.database, job_id):
                    mark_cancelled(self.database, job_id)
                    return MaterializeResult("cancelled")
                ok, reason = validate_playback_locator(locator)
                if not ok:
                    raise RuntimeError(reason)
            for row in rows:
                if cancel_requested(self.database, job_id):
                    _remove_created_paths(created_paths)
                    mark_cancelled(self.database, job_id)
                    return MaterializeResult("cancelled")
                locator = str(row["playback_locator"] or row["source_locator"] or "")
                # 分支合同：目录树/TXT/OpenList 与物理扫描由服务端 revision
                # 证据（source_evidence.ingest_method）区分，不是 provider/扩展名/
                # 客户端标记。清单来源只做纯语法校验即可发布；物理来源的可达性已经
                # 在循环前的头/中/尾抽样完成，逐行循环只做语法校验，不得再次
                # 触碰源盘（否则大库会放大成 N+3 次 I/O）。
                ok, reason = validate_playback_locator_syntax(locator)
                if not ok:
                    raise RuntimeError(reason)
                work_dir = work_directory_name(str(row["work_id"]))
                asset_identity = row["fingerprint"] or row["asset_id"]
                asset_tag = _safe_segment(asset_identity[-10:], "asset")
                if row["is_movie"]:
                    target = root / work_dir / f"movie-{asset_tag}.strm"
                    _publish_target(target, str(locator), created_paths)
                    paths.append(str(target))
                    heartbeat(self.database, job_id)
                    continue
                season_number = int(row["local_season_number"] or 0)
                if row["season_kind"] == "special" or season_number == 0:
                    season_dir = "Specials"
                    episode_name = f"SP{int(row['special_number'] or 1):02d}"
                else:
                    season_dir = f"Season {season_number:02d}"
                    episode_name = f"S{season_number:02d}E{int(row['local_episode_number'] or 0):02d}"
                target = root / work_dir / season_dir / f"{episode_name}-{asset_tag}.strm"
                _publish_target(target, str(locator), created_paths)
                paths.append(str(target))
                heartbeat(self.database, job_id)
            if cancel_requested(self.database, job_id):
                _remove_created_paths(created_paths)
                mark_cancelled(self.database, job_id)
                return MaterializeResult("cancelled")
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
                    "UPDATE jobs SET status = 'succeeded', updated_at = ?, heartbeat_at = ?, finished_at = ?, last_error = ? WHERE job_id = ?",
                    (now, now, now, "", job_id),
                )
            return MaterializeResult("succeeded", tuple(paths))
        except Exception as exc:
            _remove_created_paths(created_paths)
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ?, heartbeat_at = ?, finished_at = ? WHERE job_id = ?",
                    (str(exc), _now(), _now(), _now(), job_id),
                )
            raise
