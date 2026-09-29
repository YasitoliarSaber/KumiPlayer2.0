"""按来源卡片删除媒体库：安全性测试。

删除是破坏性操作，因此这里锁死三条红线：
1. **共享作品必须保留**——同一作品被两个来源的 revision 绑定时，删掉一个来源
   不能把作品从媒体库里打穿；
2. **镜像根之外的文件绝不删除**——artifacts 里若混入镜像根之外的路径（例如来源
   真实媒体），只能报告、不能 unlink；
3. **有进行中的扫描/作业时拒绝执行**，且不产生任何副作用。

另含"RESTRICT 顺序"回归：`revision_bindings.work_id` 是 RESTRICT，执行必须先删
绑定行再删作品行，否则删除会直接失败。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.media_v4.maintenance.source_deletion import (
    delete_source_library,
    plan_source_deletion,
)
from app.media_v4.persistence.database import V4Database
from app.media_v4.sources.scan_state import STALE_SCAN_AFTER_SECONDS


def _database(tmp_path) -> V4Database:
    database = V4Database(tmp_path / "deletion.db")
    database.initialize()
    return database


def _seed_root(conn, root_id: str, *, enabled: int = 1) -> None:
    conn.execute(
        "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at, enabled) "
        "VALUES (?, 'quark', 'openlist_scan', 'now', 'now', ?)",
        (root_id, enabled),
    )


def _seed_revision(conn, revision_id: str, root_id: str) -> None:
    # import_revisions.scan_id 有指向 source_scans 的外键，必须先登记扫描行。
    conn.execute(
        "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
        "VALUES (?, ?, 1, 'completed', 'ready', 'now', 'now')",
        (f"scan-{revision_id}", root_id),
    )
    conn.execute(
        "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at) "
        "VALUES (?, ?, ?, 'fixture', 'confirmed', 'now')",
        (revision_id, root_id, f"scan-{revision_id}"),
    )


def _seed_work(conn, work_id: str, title: str) -> None:
    conn.execute(
        "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
        "VALUES (?, ?, 'series', ?, 'now', 'now')",
        (work_id, f"title:{title}", title),
    )


def _seed_evidence(conn, evidence_id: str, root_id: str, revision_id: str) -> None:
    # scan_id 必须指向已登记的扫描行（source_evidence.scan_id 有外键）。
    conn.execute(
        "INSERT INTO source_evidence(evidence_id, scan_id, root_id, source_key, relative_path, "
        "entry_kind, provider, source_locator, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, 'video', 'quark', ?, ?)",
        (evidence_id, f"scan-{revision_id}", root_id, evidence_id, f"{evidence_id}.mkv", evidence_id, evidence_id),
    )


def _seed_binding(conn, binding_id: str, revision_id: str, work_id: str, evidence_id: str) -> None:
    conn.execute(
        "INSERT INTO revision_bindings(binding_id, revision_id, evidence_id, work_id) VALUES (?, ?, ?, ?)",
        (binding_id, revision_id, evidence_id, work_id),
    )


def _seed_artifact(conn, artifact_id: str, revision_id: str, work_id: str, target_path: str) -> None:
    conn.execute(
        "INSERT INTO artifacts(artifact_id, revision_id, work_id, artifact_type, target_path, status, "
        "created_at, updated_at) VALUES (?, ?, ?, 'mirror', ?, 'published', 'now', 'now')",
        (artifact_id, revision_id, work_id, target_path),
    )


def _two_sources(tmp_path):
    """来源 A 独占 one/two；来源 B 也绑定其中 one（共享）。"""

    database = _database(tmp_path)
    mirror = tmp_path / "mirror"
    (mirror / "A").mkdir(parents=True, exist_ok=True)
    with database.connect() as conn:
        _seed_root(conn, "root-a")
        _seed_root(conn, "root-b")
        _seed_revision(conn, "rev-a", "root-a")
        _seed_revision(conn, "rev-b", "root-b")
        for work_id, title in (("work-one", "作品一"), ("work-two", "作品二"), ("work-three", "作品三")):
            _seed_work(conn, work_id, title)
        for index, work_id in enumerate(("work-one", "work-two"), start=1):
            evidence_id = f"ev-a{index}"
            _seed_evidence(conn, evidence_id, "root-a", "rev-a")
            _seed_binding(conn, f"bind-a{index}", "rev-a", work_id, evidence_id)
        _seed_evidence(conn, "ev-b1", "root-b", "rev-b")
        _seed_binding(conn, "bind-b1", "rev-b", "work-one", "ev-b1")
        _seed_work(conn, "work-b-own", "B 自己的作品")
        _seed_evidence(conn, "ev-b2", "root-b", "rev-b")
        _seed_binding(conn, "bind-b2", "rev-b", "work-b-own", "ev-b2")
        for index, work_id in enumerate(("work-one", "work-two"), start=1):
            _seed_artifact(
                conn,
                f"art-a{index}",
                "rev-a",
                work_id,
                str(mirror / "A" / f"file{index}.strm"),
            )
    for index in (1, 2):
        (mirror / "A" / f"file{index}.strm").write_text("x", encoding="utf-8")
    return database, mirror


def test_plan_keeps_works_shared_with_other_sources(tmp_path):
    database, mirror = _two_sources(tmp_path)

    plan = plan_source_deletion(database, "root-a", mirror_root=mirror)

    assert plan["works_removable"] == 1, plan          # work-two 只属于 A
    assert plan["works_shared"] == 1, plan             # work-one 被 B 也绑定
    assert plan["current_works_total"] == 2, plan
    assert plan["current_works_leaving"] == 1, plan
    assert plan["current_works_shared"] == 1, plan
    # 用户 2026-09-28 决定：确认框不再列作品示例（后端也不再为 UI 查标题）。
    assert plan["removable_samples"] == []
    assert plan["shared_samples"] == []
    # 只统计会真正回收的产物：共享作品（work-one）的产物必须保留，不计入
    assert plan["artifact_files"] == 1
    assert plan["blockers"] == []


def test_superseded_reference_does_not_count_as_sharing(tmp_path):
    """阶段 2：只有**当前有效**的引用才算"别人还需要它"。

    旧实现把其他来源的 superseded/草稿 revision 也当成共享，于是应该回收的产物永远
    留着（实测口径不一致）。这里把另一来源的导入标为 superseded 后再预览。
    """

    database, mirror = _two_sources(tmp_path)
    with database.connect() as conn:
        conn.execute("UPDATE import_revisions SET status = 'superseded' WHERE revision_id = 'rev-b'")

    plan = plan_source_deletion(database, "root-a", mirror_root=mirror)

    assert plan["works_shared"] == 0, f"已被取代的引用不得再算成共享：{plan}"
    assert plan["works_removable"] == 2, plan


def test_delete_removes_only_unshared_works_and_their_files(tmp_path):
    database, mirror = _two_sources(tmp_path)

    result = delete_source_library(database, "root-a", mirror_root=mirror)

    assert result["ok"] is True, result
    assert result["works_leaving_library"] == 1, result   # 只处理不与 B 共享的作品
    assert result["removed_files"] == 1                    # 只回收它自己的产物文件
    assert result["shared_works_kept"] == 1
    with database.connect() as conn:
        # 已确认事实不可删（触发器 + RESTRICT）：作品行保留，靠来源退役退出活动库。
        assert conn.execute("SELECT COUNT(*) FROM works WHERE work_id = 'work-two'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE work_id = 'work-two'"
        ).fetchone()[0] == 0, "离开媒体库的作品，其镜像产物应被回收"
        assert conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE work_id = 'work-one'"
        ).fetchone()[0] == 1, "共享作品的产物必须保留（否则媒体墙上会出现缺文件的作品）"
        root = conn.execute("SELECT enabled, retired_at FROM source_roots WHERE root_id = 'root-a'").fetchone()
        assert str(root["retired_at"]) != "", "来源应被退役（作品退出活动库）"
        assert int(root["enabled"]) == 0


def test_delete_never_touches_files_outside_the_mirror_root(tmp_path):
    """红线：镜像根之外的路径（例如来源真实媒体）只能报告，绝不能删。"""

    database = _database(tmp_path)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    outside = tmp_path / "outside" / "REAL_SOURCE.mkv"
    outside.parent.mkdir()
    outside.write_text("real media", encoding="utf-8")
    with database.connect() as conn:
        _seed_root(conn, "root-x")
        _seed_revision(conn, "rev-x", "root-x")
        _seed_work(conn, "work-x", "作品X")
        _seed_evidence(conn, "ev-x", "root-x", "rev-x")
        _seed_binding(conn, "bind-x", "rev-x", "work-x", "ev-x")
        _seed_artifact(conn, "art-x", "rev-x", "work-x", str(outside))

    plan = plan_source_deletion(database, "root-x", mirror_root=mirror)
    # 只报告数量：预览不得把本地绝对路径返回给前端（项目红线）。
    assert plan["files_outside_mirror_count"] == 1
    assert "files_outside_mirror" not in plan
    assert plan["artifact_files"] == 0, "镜像根之外的文件不得计入可删除文件"

    result = delete_source_library(database, "root-x", mirror_root=mirror)

    assert result["files_outside_mirror_skipped"] == 1
    assert outside.is_file(), "来源真实媒体必须原样保留"
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE artifact_id = 'art-x'").fetchone()[0] == 1


def test_delete_refuses_while_source_has_active_scan(tmp_path):
    database, mirror = _two_sources(tmp_path)
    fresh = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-a', 'root-a', 2, 'running', 'reading_source', ?, ?)",
            (fresh, fresh),
        )

    plan = plan_source_deletion(database, "root-a", mirror_root=mirror)
    assert plan["blockers"], "有进行中的扫描时必须报告阻断"

    result = delete_source_library(database, "root-a", mirror_root=mirror)

    assert result["ok"] is False
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM works WHERE work_id = 'work-two'").fetchone()[0] == 1, (
            "被阻断时不得产生任何删除副作用"
        )


def test_stale_zombie_scan_does_not_block_deletion(tmp_path):
    """心跳失联的扫描永远到不了终态，不能永久阻断用户删除（审核发现的缺陷）。"""

    database, mirror = _two_sources(tmp_path)
    zombie = (datetime.now(UTC) - timedelta(seconds=STALE_SCAN_AFTER_SECONDS + 3600)).isoformat()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-zombie', 'root-a', 2, 'running', 'reading_source', ?, ?)",
            (zombie, zombie),
        )

    plan = plan_source_deletion(database, "root-a", mirror_root=mirror)
    assert plan["blockers"] == [], "僵尸扫描不得阻断删除"

    result = delete_source_library(database, "root-a", mirror_root=mirror)
    assert result["ok"] is True, result


def test_api_preview_and_delete_requires_explicit_confirmation(tmp_path, monkeypatch):
    from app.api import media_v4
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    database, mirror = _two_sources(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    preview = client.get("/api/v4/sources/libraries/root-a/deletion-preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["works_removable"] == 1 and body["works_shared"] == 1
    assert body["artifact_files"] == 1

    assert client.get("/api/v4/sources/libraries/nope/deletion-preview").status_code == 404

    unconfirmed = client.post("/api/v4/sources/libraries/root-a/deletion", json={"confirm": False})
    assert unconfirmed.status_code == 400, "未确认不得开始删除"

    confirmed = client.post("/api/v4/sources/libraries/root-a/deletion", json={"confirm": True})
    assert confirmed.status_code == 200, confirmed.text
    job_id = confirmed.json()["job_id"]
    with database.connect() as conn:
        job = conn.execute("SELECT job_type, status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert str(job["job_type"]) == "delete_source_library"
    assert str(job["status"]) == "queued"


def test_deletion_job_runs_end_to_end_and_retires_the_source(tmp_path, monkeypatch):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion

    database, mirror = _two_sources(tmp_path)
    # 投影重建是删除后的收口步骤，其自身正确性由投影测试覆盖；这里隔离它，
    # 让本用例专注"删除语义与作业接线"。
    monkeypatch.setattr("app.media_v4.jobs.runner.V4LibraryProjection.rebuild", lambda _self: {})
    job_id = enqueue_source_deletion(database, "root-a")

    result = V4JobRunner(database).process_job(job_id, mirror_root=mirror)

    assert result.status == "succeeded", result
    with database.connect() as conn:
        assert str(conn.execute(
            "SELECT retired_at FROM source_roots WHERE root_id = 'root-a'"
        ).fetchone()["retired_at"]) != ""
        assert conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE work_id = 'work-two'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE work_id = 'work-one'"
        ).fetchone()[0] == 1, "共享作品的产物必须保留"


def test_cancelled_deletion_returns_cancelled_instead_of_raising(tmp_path):
    """取消必须是可预期终态。

    审核发现的缺陷：旧实现用 ``raise KeyboardInterrupt`` 表达取消，它是
    ``BaseException``，会穿透 runner 的 ``except Exception``，把后台 worker 打死并让
    作业永远停在 running。现在改为在删除函数内收口成 ``cancelled`` 结果。
    """

    database, mirror = _two_sources(tmp_path)

    result = delete_source_library(
        database, "root-a", mirror_root=mirror, should_cancel=lambda: True
    )

    assert result["ok"] is False
    assert result.get("cancelled") is True
    assert result["reason"] == "已取消"
    with database.connect() as conn:
        root = conn.execute("SELECT retired_at FROM source_roots WHERE root_id = 'root-a'").fetchone()
        assert str(root["retired_at"]) == "", "取消后不得退役来源"


def test_cancellation_after_cleanup_starts_cannot_leave_visible_work_without_artifact(tmp_path):
    database, mirror = _two_sources(tmp_path)
    cancel = False

    def on_progress(_done, _total):
        nonlocal cancel
        with database.connect() as conn:
            retired = conn.execute("SELECT retired_at FROM source_roots WHERE root_id = 'root-a'").fetchone()[0]
        assert retired, "每批文件清理前必须先让来源退出可见媒体库"
        cancel = True

    result = delete_source_library(
        database, "root-a", mirror_root=mirror, batch_size=1,
        progress=on_progress, should_cancel=lambda: cancel,
    )

    assert result["ok"] is True
    with database.connect() as conn:
        root = conn.execute("SELECT retired_at FROM source_roots WHERE root_id = 'root-a'").fetchone()
        assert str(root["retired_at"]) != "", "开始清理后来源必须先退役"
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE work_id = 'work-two'").fetchone()[0] == 0


def test_deletion_unlink_failure_keeps_artifact_row_for_retry(tmp_path, monkeypatch):
    database, mirror = _two_sources(tmp_path)
    target = mirror / "A" / "file2.strm"
    original_unlink = type(target).unlink

    def fail_one(path, *args, **kwargs):
        if path == target:
            raise PermissionError("fixture locked")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(type(target), "unlink", fail_one)
        result = delete_source_library(database, "root-a", mirror_root=mirror)

    assert result["ok"] is False
    assert target.exists()
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE work_id = 'work-two'").fetchone()[0] == 1


def test_deletion_job_retries_after_file_failure_then_finishes(tmp_path, monkeypatch):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion

    database, mirror = _two_sources(tmp_path)
    monkeypatch.setattr("app.media_v4.jobs.runner.V4LibraryProjection.rebuild", lambda _self: {})
    job_id = enqueue_source_deletion(database, "root-a")
    target = mirror / "A" / "file2.strm"
    original_unlink = type(target).unlink
    failures = 0

    def fail_once(path, *args, **kwargs):
        nonlocal failures
        if path == target and failures == 0:
            failures += 1
            raise PermissionError("fixture locked")
        return original_unlink(path, *args, **kwargs)

    runner = V4JobRunner(database)
    with monkeypatch.context() as patcher:
        patcher.setattr(type(target), "unlink", fail_once)
        first = runner.process_job(job_id, mirror_root=mirror)
        assert first.status == "queued"
        assert target.exists()
        second = runner.process_job(job_id, mirror_root=mirror)

    assert second.status == "succeeded"
    assert not target.exists()
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE work_id = 'work-two'").fetchone()[0] == 0


def test_exhausted_cleanup_remains_visible_and_can_be_retried(tmp_path, monkeypatch):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion
    from app.media_v4.projection.source_libraries import list_source_cards

    database, mirror = _two_sources(tmp_path)
    monkeypatch.setattr("app.media_v4.jobs.runner.V4LibraryProjection.rebuild", lambda _self: {})
    target = mirror / "A" / "file2.strm"
    original_unlink = type(target).unlink

    def fail_locked(path, *args, **kwargs):
        if path == target:
            raise PermissionError("fixture locked")
        return original_unlink(path, *args, **kwargs)

    runner = V4JobRunner(database)
    job_id = enqueue_source_deletion(database, "root-a")
    with monkeypatch.context() as patcher:
        patcher.setattr(type(target), "unlink", fail_locked)
        assert [runner.process_job(job_id, mirror_root=mirror).status for _ in range(3)] == [
            "queued", "queued", "failed"
        ]

    assert target.exists()
    card = next(item for item in list_source_cards(database) if item["root_id"] == "root-a")
    assert card["deletion_retry_required"] is True
    assert card["counts_scope"] == "cleanup"
    assert card["work_count"] is None
    assert plan_source_deletion(database, "root-a", mirror_root=mirror)["artifact_files"] == 1

    retry_id = enqueue_source_deletion(database, "root-a")
    assert retry_id != job_id
    queued_card = next(item for item in list_source_cards(database) if item["root_id"] == "root-a")
    assert queued_card["active_task"]["status"] == "queued"
    assert runner.process_job(retry_id, mirror_root=mirror).status == "succeeded"
    assert not target.exists()
    assert not any(item["root_id"] == "root-a" for item in list_source_cards(database))


def test_stale_deletion_job_resumes_instead_of_staying_running(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion

    database, _mirror = _two_sources(tmp_path)
    job_id = enqueue_source_deletion(database, "root-a")
    with database.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = 1, heartbeat_at = '2020-01-01T00:00:00+00:00' "
            "WHERE job_id = ?", (job_id,),
        )

    assert V4JobRunner(database).recover_stale_jobs() == 1
    with database.connect() as conn:
        row = conn.execute("SELECT status, cancel_requested FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert str(row["status"]) == "queued"
        assert not row["cancel_requested"]


def test_source_card_deletion_task_has_no_old_import_percent_or_running_cancel():
    from app.media_v4.projection.source_libraries import _active_task

    task = _active_task(None, {"job_type": "delete_source_library", "status": "running", "cancel_requested": 0}, "rev-a", {"percent": 80})

    assert task is not None
    assert task["percent"] is None
    assert task["can_cancel"] is False


def test_failed_deletion_is_visible_on_source_card_for_retry(tmp_path):
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion
    from app.media_v4.projection.source_libraries import list_source_cards

    database, _mirror = _two_sources(tmp_path)
    job_id = enqueue_source_deletion(database, "root-a")
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'failed', last_error = 'fixture failure' WHERE job_id = ?", (job_id,))

    card = next(item for item in list_source_cards(database) if item["root_id"] == "root-a")

    assert card["deletion_retry_required"] is True
    assert card["overall_status"] == "needs_attention"
    assert "清理未完成" in card["last_error"]


def test_retired_source_with_failed_cleanup_has_retry_card_without_restoring_works(tmp_path):
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion
    from app.media_v4.projection.source_libraries import list_source_cards

    database, _mirror = _two_sources(tmp_path)
    job_id = enqueue_source_deletion(database, "root-a")
    with database.connect() as conn:
        conn.execute("UPDATE source_roots SET retired_at = '2026-09-29T00:00:00+00:00', enabled = 0 WHERE root_id = 'root-a'")
        conn.execute("UPDATE jobs SET status = 'failed' WHERE job_id = ?", (job_id,))

    card = next(item for item in list_source_cards(database) if item["root_id"] == "root-a")

    assert card["deletion_retry_required"] is True
    assert card["work_count"] is None
    assert card["counts_scope"] == "cleanup"


def test_retired_source_cleanup_card_stays_visible_while_queued_and_running(tmp_path):
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion
    from app.media_v4.projection.source_libraries import list_source_cards

    database, _mirror = _two_sources(tmp_path)
    job_id = enqueue_source_deletion(database, "root-a")
    with database.connect() as conn:
        conn.execute("UPDATE source_roots SET retired_at = '2026-09-29T00:00:00+00:00', enabled = 0 WHERE root_id = 'root-a'")

    for status in ("queued", "running"):
        with database.connect() as conn:
            conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
        card = next(item for item in list_source_cards(database) if item["root_id"] == "root-a")
        assert card["counts_scope"] == "cleanup"
        assert card["work_count"] is None
        assert card["active_task"]["status"] == status
        assert card["active_task"]["can_cancel"] is False
        assert card["deletion_retry_required"] is False


def test_runner_maps_cancelled_outcome_to_cancelled_job(tmp_path, monkeypatch):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion

    database, mirror = _two_sources(tmp_path)
    monkeypatch.setattr(
        "app.media_v4.jobs.runner.V4LibraryProjection.rebuild", lambda _self: {}
    )
    monkeypatch.setattr(
        "app.media_v4.maintenance.source_deletion.delete_source_library",
        lambda *_args, **_kwargs: {"ok": False, "cancelled": True, "reason": "已取消"},
    )
    job_id = enqueue_source_deletion(database, "root-a")

    result = V4JobRunner(database).process_job(job_id, mirror_root=mirror)

    assert result.status == "cancelled", result
    with database.connect() as conn:
        status = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()["status"]
    assert str(status) == "cancelled"


def test_deletion_never_invokes_recognition(tmp_path, monkeypatch):
    """红线（用户明确要求）：按来源删除绝不重跑识别，否则会像旧版那样卡住。"""

    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion
    from app.media_v4.parsing.parser import V4Parser
    from app.media_v4.resolution.resolver import MediaResolver

    def _explode(*_args, **_kwargs):
        raise AssertionError("按来源删除不得调用识别/解析链路")

    monkeypatch.setattr(MediaResolver, "resolve", _explode)
    monkeypatch.setattr(V4Parser, "parse", _explode)
    monkeypatch.setattr("app.media_v4.jobs.runner.V4LibraryProjection.rebuild", lambda _self: {})
    database, mirror = _two_sources(tmp_path)
    job_id = enqueue_source_deletion(database, "root-a")

    result = V4JobRunner(database).process_job(job_id, mirror_root=mirror)

    assert result.status == "succeeded", result


def test_queued_jobs_do_not_block_source_deletion(tmp_path):
    """排队中的同源作业不得永久阻断"按来源删除媒体库"（用户真实故障）。

    实测（用户库，只读核对）：来源卡停在"0/8 待处理"，该来源的排队作业长期不动，
    于是每次点"确认删除媒体库"都被 409 拒绝、**根本没有入队**——
    jobs 表里 delete_source_library 只有 2 条且都是 succeeded。

    规则：**queued** 作业让路并被取消（它们尚未开始，删除会把整个来源媒体库带走）；
    **running** 的作业仍然阻断删除（在途任务不得互相踩）。
    """

    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion

    database, _mirror = _two_sources(tmp_path)
    with database.connect() as conn:
        revision = conn.execute(
            "SELECT revision_id FROM import_revisions WHERE root_id = 'root-a' "
            "AND status = 'confirmed' ORDER BY created_at DESC, revision_id DESC LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, "
            "status, created_at, updated_at) "
            "VALUES ('job-queued', 'scrape_work', ?, '', 'queued-key', 'queued', ?, ?)",
            (
                str(revision["revision_id"]),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    job_id = enqueue_source_deletion(database, "root-a")  # 不得抛 ValueError

    assert job_id.startswith("job_")
    with database.connect() as conn:
        queued_status = conn.execute(
            "SELECT status FROM jobs WHERE job_id = 'job-queued'"
        ).fetchone()["status"]
    assert str(queued_status) == "cancelled", "排队作业必须让路并被取消"


def test_second_deletion_after_succeeded_does_not_hit_unique_key(tmp_path):
    """重新导入后再删除不得撞 `jobs.idempotency_key` 的 UNIQUE 约束。

    实测（用户库，只读核对）：后端日志里是
    `sqlite3.IntegrityError: UNIQUE constraint failed: jobs.idempotency_key` ——
    旧幂等键只含 root_id，而已完成的删除作业行不会消失，于是第二次删除直接 500，
    用户看到"点了没反应"。修复后键按 **revision** 唯一：同一次导入仍去重，
    新导入（新 revision）可以重新删除。
    """

    from app.media_v4.maintenance.source_deletion import enqueue_source_deletion

    database, _mirror = _two_sources(tmp_path)

    def _mark_succeeded(job_id: str) -> None:
        with database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'succeeded' WHERE job_id = ?", (job_id,)
            )

    # 第一次删除：入队后置为已完成（模拟真实历史作业行仍然存在）。
    first = enqueue_source_deletion(database, "root-a")
    _mark_succeeded(first)

    # 再点一次：必须能再次入队，绝不能抛 IntegrityError（这就是用户遇到的 500）。
    second = enqueue_source_deletion(database, "root-a")

    assert second.startswith("job_")
    with database.connect() as conn:
        keys = [
            str(row["idempotency_key"])
            for row in conn.execute(
                "SELECT idempotency_key FROM jobs WHERE job_type = 'delete_source_library'"
            )
        ]
    assert len(keys) == len(set(keys)), f"幂等键必须唯一，实际 {keys}"


def test_current_impact_is_separate_from_historical_maintenance_set(tmp_path):
    """STEP-002 / F-003：卡片作品数与删除提示数是两个集合。

    历史 superseded revision 遗留的 work_id 只属于维护集合（产物回收），不能当成
    “会离开媒体库的作品数”；反过来也不能为了把数字改小而少回收产物。
    """

    database = _database(tmp_path)
    mirror = tmp_path / "mirror"
    (mirror / "A").mkdir(parents=True, exist_ok=True)
    with database.connect() as conn:
        _seed_root(conn, "root-a")
        _seed_root(conn, "root-b")
        _seed_revision(conn, "rev-a", "root-a")
        _seed_revision(conn, "rev-b", "root-b")
        for index in range(1, 6):
            _seed_work(conn, f"cur-{index}", f"当前作品{index}")
            _seed_evidence(conn, f"ev-a{index}", "root-a", "rev-a")
            _seed_binding(conn, f"bind-a{index}", "rev-a", f"cur-{index}", f"ev-a{index}")
        # 历史：同一来源的 superseded revision 遗留 20 个不同 work_id。
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-rev-a-old', 'root-a', 0, 'completed', 'ready', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at) "
            "VALUES ('rev-a-old', 'root-a', 'scan-rev-a-old', 'fixture', 'superseded', 'now')"
        )
        for index in range(1, 21):
            _seed_work(conn, f"hist-{index}", f"历史作品{index}")
            _seed_evidence(conn, f"ev-old{index}", "root-a", "rev-a-old")
            _seed_binding(conn, f"bind-old{index}", "rev-a-old", f"hist-{index}", f"ev-old{index}")
        # 其他来源当前共享两个当前作品。
        for index in (1, 2):
            _seed_evidence(conn, f"ev-b{index}", "root-b", "rev-b")
            _seed_binding(conn, f"bind-b{index}", "rev-b", f"cur-{index}", f"ev-b{index}")

    plan = plan_source_deletion(database, "root-a", mirror_root=mirror)

    assert plan["current_works_total"] == 5, plan
    assert plan["current_works_leaving"] == 3, plan
    assert plan["current_works_shared"] == 2, plan
    assert plan["works_removable"] == 3, "对外口径必须与 current_works_leaving 一致"
    assert plan["historical_work_ids_examined"] >= 20, "历史维护集合仍须包含遗留 work_id"
    # 重复预览无副作用。
    again = plan_source_deletion(database, "root-a", mirror_root=mirror)
    assert again == plan
