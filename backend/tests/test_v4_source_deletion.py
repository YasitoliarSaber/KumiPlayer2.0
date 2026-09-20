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
    assert plan["removable_samples"] == ["作品二"]
    assert plan["shared_samples"] == ["作品一"]
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
