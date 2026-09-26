"""V4 confirmed revision 的镜像物化器。

只消费当前 confirmed binding 的观察定位符（``rb.evidence_id`` 对应的
``source_evidence``）与 ``rb.asset_id``；``revision_bindings.resolved_json``
存在时使用其中的 ``content_class``/``media_type``，缺失时走明确 legacy
分类（只按已有列推导），**禁止**再调用 parser / recognition。

目标名全部由 ``artifact_paths`` 唯一实现；不允许 fingerprint 尾串、截短 ID
或 ``S00E00`` / ``Specials`` 兜底。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from app.media_v4.domain.identity import (
    CONTENT_CLASS_ATTACHED_SPECIAL,
    CONTENT_CLASS_MOVIE,
    CONTENT_CLASS_REGULAR,
    CONTENT_CLASS_UNKNOWN,
    SEASON_KIND_REGULAR,
    SEASON_KIND_UNASSIGNED,
)
from app.media_v4.jobs.artifact_paths import (
    NonMaterializableArtifact,
    guard_target_path,
    locator_digest,
    mirror_relative_path,
)
from app.media_v4.jobs.control import cancel_requested, claim_running, heartbeat, mark_cancelled
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

MEDIA_TYPE_MOVIE = "movie"
WORK_TYPE_MOVIE = "movie"
EPISODE_KIND_SPECIAL = "special"


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    artifact_paths: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    outcome: str = ""
    reason_codes: tuple[str, ...] = ()
    produced_artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Classification:
    """一个 binding 的分类；``legacy=True`` 表示只按旧列推导。"""

    content_class: str
    is_movie: bool
    season_kind: str | None
    local_season_number: int | None
    local_episode_number: int | None
    absolute_episode_number: int | None
    special_number: int | None
    legacy: bool


@dataclass(frozen=True, slots=True)
class _PlannedMirror:
    """一个逻辑 Episode 的镜像计划项；``(episode_id, asset_id, locator)`` 是去重单位。"""

    episode_id: str | None
    asset_id: str
    evidence_id: str
    locator: str
    relative_path: str
    digest: str
    is_movie: bool


def _parse_resolved(raw) -> dict | None:
    """只接受 ``contract_version == 1`` 的 resolved_json；其余按 legacy 处理。"""

    try:
        payload = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        version = int(payload.get("contract_version") or 0)
    except (TypeError, ValueError):
        return None
    if version != 1:
        return None
    return payload


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _classify(row) -> _Classification:
    """resolved_json 优先；缺失时走明确 legacy 分类，不重新识别。"""

    resolved = _parse_resolved(row["resolved_json"])
    legacy = resolved is None
    content_class = str((resolved or {}).get("content_class") or "") or None
    media_type = str((resolved or {}).get("media_type") or "") or None
    work_type = str(row["work_type"] or "")
    episode_kind = str(row["episode_kind"] or "")

    season_kind = str(row["season_kind"] or "") or None
    local_season = _as_int(row["local_season_number"])
    local_episode = _as_int(row["local_episode_number"])
    absolute_episode = _as_int(row["absolute_episode_number"])
    special_number = _as_int(row["special_number"])

    is_movie = row["episode_id"] is None and (
        media_type == MEDIA_TYPE_MOVIE or work_type == WORK_TYPE_MOVIE
    )

    if content_class is None:
        if is_movie:
            content_class = CONTENT_CLASS_MOVIE
        elif episode_kind == EPISODE_KIND_SPECIAL or season_kind == EPISODE_KIND_SPECIAL:
            content_class = CONTENT_CLASS_ATTACHED_SPECIAL
        elif special_number is not None:
            content_class = CONTENT_CLASS_ATTACHED_SPECIAL
        elif season_kind == SEASON_KIND_UNASSIGNED or local_season is None:
            content_class = CONTENT_CLASS_UNKNOWN
        else:
            content_class = CONTENT_CLASS_REGULAR

    # C-002/C-008：旧 ``regular/0`` 不是已证明特别篇。既不能按 0 重新分类为
    # special，也不能继续写 S00E00；这里只把它降为“未知季”。
    if season_kind == SEASON_KIND_REGULAR and (local_season is None or local_season <= 0):
        season_kind = SEASON_KIND_UNASSIGNED
        local_season = None

    return _Classification(
        content_class=content_class,
        is_movie=is_movie,
        season_kind=season_kind,
        local_season_number=local_season,
        local_episode_number=local_episode,
        absolute_episode_number=absolute_episode,
        special_number=special_number,
        legacy=legacy,
    )


class V4MirrorMaterializer:
    """只消费已确认 revision 的 Asset；不调用 parser 或 recognition。"""

    def __init__(self, database: V4Database):
        self.database = database

    def _binding_rows(self, conn, revision_id: str, work_id: str):
        """locator 来自 ``rb.evidence_id`` 的观察；asset ID 来自 ``rb.asset_id``。"""

        return conn.execute(
            """
            SELECT DISTINCT
                rb.revision_id, rb.work_id, rb.episode_id, rb.asset_id, rb.evidence_id,
                rb.resolved_json,
                w.work_type,
                s.local_season_number, s.season_kind,
                e.local_episode_number, e.absolute_episode_number,
                e.special_number, e.episode_kind,
                COALESCE(se.ingest_method, '') AS ingest_method,
                se.playback_locator AS observation_playback_locator,
                se.source_locator AS observation_source_locator
            FROM revision_bindings rb
            JOIN works w ON w.work_id = rb.work_id
            JOIN source_evidence se ON se.evidence_id = rb.evidence_id
            LEFT JOIN episodes e ON e.episode_id = rb.episode_id
            LEFT JOIN seasons s ON s.season_id = e.season_id
            WHERE rb.revision_id = ? AND rb.work_id = ?
            ORDER BY rb.episode_id, rb.asset_id, rb.evidence_id
            """,
            (revision_id, work_id),
        ).fetchall()

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
                return MaterializeResult(
                    "succeeded", tuple(row["target_path"] for row in existing_paths)
                )
            rows = self._binding_rows(conn, job["revision_id"], job["work_id"])

        if not claim_running(self.database, job_id):
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return MaterializeResult("cancelled")
            raise RuntimeError("镜像任务已由其他执行器领取")

        created_paths: list[Path] = []
        reason_codes: list[str] = []
        pending: list[tuple[_PlannedMirror, Path]] = []
        produced: tuple[str, ...] | None = None
        try:
            # 非 TXT/OpenList 资产按头/中/尾有界抽样复核可达性；TXT/OpenList 只做
            # 语法校验，绝不探测挂载盘。抽样在任何写盘之前完成。
            non_txt_locators = [
                str(row["observation_playback_locator"] or row["observation_source_locator"] or "")
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

            # 先算出全部目标与内容：同路径不同内容立即失败，绝不先写半批。
            planned: list[_PlannedMirror] = []
            for row in rows:
                asset_id = str(row["asset_id"] or "")
                if not asset_id:
                    # 被排除的附属内容可以保留观察，但没有 Asset → 没有产物。
                    reason_codes.append("legacy_excluded")
                    continue
                classification = _classify(row)
                locator = str(
                    row["observation_playback_locator"] or row["observation_source_locator"] or ""
                )
                try:
                    relative_path = mirror_relative_path(
                        work_id=str(row["work_id"]),
                        asset_id=asset_id,
                        playback_locator=locator,
                        is_movie=classification.is_movie,
                        content_class=classification.content_class,
                        season_kind=classification.season_kind,
                        local_season_number=classification.local_season_number,
                        local_episode_number=classification.local_episode_number,
                        absolute_episode_number=classification.absolute_episode_number,
                        special_number=classification.special_number,
                    )
                except NonMaterializableArtifact:
                    # 旧明确 special/附属 binding：跳过并标注 legacy_excluded，
                    # 不重新解析路径，也不生成 Specials/S00E00。
                    reason_codes.append("legacy_excluded")
                    continue
                ok, reason = validate_playback_locator_syntax(locator)
                if not ok:
                    raise RuntimeError(reason)
                planned.append(
                    _PlannedMirror(
                        episode_id=str(row["episode_id"]) if row["episode_id"] else None,
                        asset_id=asset_id,
                        evidence_id=str(row["evidence_id"]),
                        locator=locator,
                        relative_path=relative_path,
                        digest=locator_digest(locator),
                        is_movie=classification.is_movie,
                    )
                )

            unique: list[_PlannedMirror] = []
            seen: dict[str, str] = {}
            for entry in planned:
                existing = seen.get(entry.relative_path)
                if existing is None:
                    seen[entry.relative_path] = entry.locator
                    unique.append(entry)
                elif existing != entry.locator:
                    raise RuntimeError(f"镜像目标冲突：同一路径需要不同内容: {entry.relative_path}")

            if not unique:
                return self._settle_without_products(
                    job_id,
                    revision_id=str(job["revision_id"]),
                    work_id=str(job["work_id"]),
                    reason_codes=reason_codes,
                )

            # 目标预先检查：越界/过长名与已存在但内容不一致的目标都在写盘前拦截。
            for entry in unique:
                target = root.joinpath(*PurePosixPath(entry.relative_path).parts)
                guard_target_path(entry.relative_path)
                if target.exists() and not _existing_target_matches(target, entry.locator):
                    raise RuntimeError(f"镜像目标已存在且内容不一致: {target}")
                pending.append((entry, target))

            for entry, target in pending:
                if cancel_requested(self.database, job_id):
                    _remove_created_paths(created_paths)
                    mark_cancelled(self.database, job_id)
                    return MaterializeResult("cancelled")
                _publish_target(target, entry.locator, created_paths)
                heartbeat(self.database, job_id)
            if cancel_requested(self.database, job_id):
                _remove_created_paths(created_paths)
                mark_cancelled(self.database, job_id)
                return MaterializeResult("cancelled")

            produced = self._register_artifacts(
                job_id=job_id,
                revision_id=str(job["revision_id"]),
                work_id=str(job["work_id"]),
                pending=pending,
                created_paths=created_paths,
                reason_codes=reason_codes,
            )
        except Exception as exc:
            _remove_created_paths(created_paths)
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ?, heartbeat_at = ?, finished_at = ? WHERE job_id = ?",
                    (str(exc), _now(), _now(), _now(), job_id),
                )
            raise

        if produced is None:
            # 发布事务内发现 cancel_requested：只撤销本次自建文件，再收口 cancelled。
            _remove_created_paths(created_paths)
            mark_cancelled(self.database, job_id)
            return MaterializeResult("cancelled")
        outcome = "reused" if not created_paths else "published"
        return MaterializeResult(
            "succeeded",
            tuple(str(target) for _entry, target in pending),
            outcome=outcome,
            reason_codes=tuple(sorted(set(reason_codes))),
            produced_artifact_ids=produced,
        )

    def _settle_without_products(
        self,
        job_id: str,
        *,
        revision_id: str,
        work_id: str,
        reason_codes: list[str],
    ) -> MaterializeResult:
        """没有可物化对象（例如只含旧明确 special binding）时如实收口。"""

        codes = tuple(sorted(set(reason_codes) or {"legacy_excluded"}))
        if cancel_requested(self.database, job_id):
            mark_cancelled(self.database, job_id)
            return MaterializeResult("cancelled")
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT status, cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise RuntimeError("镜像任务不再处于运行状态")
            if bool(row["cancel_requested"]):
                return MaterializeResult("cancelled")
            now = _now()
            conn.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', last_error = '', updated_at = ?, heartbeat_at = ?,
                    finished_at = ?, result_json = ?
                WHERE job_id = ?
                """,
                (now, now, now, json.dumps({
                    "outcome": "skipped",
                    "revision_id": revision_id,
                    "work_id": work_id,
                    "produced_artifact_ids": [],
                    "reason_codes": list(codes),
                }, ensure_ascii=False), job_id),
            )
        return MaterializeResult("skipped", (), (), outcome="skipped", reason_codes=codes)

    def _register_artifacts(
        self,
        *,
        job_id: str,
        revision_id: str,
        work_id: str,
        pending: list[tuple[_PlannedMirror, Path]],
        created_paths: list[Path],
        reason_codes: list[str],
    ) -> tuple[str, ...] | None:
        """事务内复查任务/revision/取消标记后再写 artifacts、refs 与 result_json。

        返回 ``None`` 表示发布事务内发现取消请求；调用方负责撤销自建文件并收口
        cancelled（不在此处嵌套写连接）。
        """

        now = _now()
        outcome = "reused" if not created_paths else "published"
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT status, cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if row is not None and bool(row["cancel_requested"]):
                return None
            if (
                row is None
                or row["status"] != "running"
                or revision is None
                or revision["status"] != "confirmed"
            ):
                raise RuntimeError("镜像发布前检查失败：任务不再运行或 revision 不再是 confirmed")

            produced: list[str] = []
            for entry, target in pending:
                artifact_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT OR IGNORE INTO artifacts(
                        artifact_id, revision_id, work_id, artifact_type,
                        target_path, digest, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'mirror', ?, ?, 'published', ?, ?)
                    """,
                    (artifact_id, revision_id, work_id, str(target), entry.digest, now, now),
                )
                stored = conn.execute(
                    "SELECT artifact_id FROM artifacts "
                    "WHERE revision_id = ? AND artifact_type = 'mirror' AND target_path = ?",
                    (revision_id, str(target)),
                ).fetchone()
                stored_id = str(stored["artifact_id"]) if stored is not None else artifact_id
                conn.execute(
                    """
                    INSERT OR IGNORE INTO artifact_references(
                        reference_id, artifact_id, revision_id, work_id,
                        snapshot_id, role, subject_id, created_at
                    ) VALUES (?, ?, ?, ?, NULL, 'mirror', ?, ?)
                    """,
                    (str(uuid.uuid4()), stored_id, revision_id, work_id, entry.asset_id, now),
                )
                if stored_id not in produced:
                    produced.append(stored_id)
            conn.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', last_error = '', updated_at = ?, heartbeat_at = ?,
                    finished_at = ?, result_json = ?
                WHERE job_id = ?
                """,
                (now, now, now, json.dumps({
                    "outcome": outcome,
                    "revision_id": revision_id,
                    "work_id": work_id,
                    "produced_artifact_ids": produced,
                    "reason_codes": sorted(set(reason_codes)),
                }, ensure_ascii=False), job_id),
            )
        return tuple(produced)
