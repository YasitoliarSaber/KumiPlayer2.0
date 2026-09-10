"""SourceScan 进程失联恢复合同（C 段）。

执行真相只存在于 SQLite：请求、阶段、心跳、取消标记。进程退出后
daemon 线程消失，数据库中的 running 行必须能被新进程的 SourceScanRunner
按四个分支收口或恢复；来源卡投影不得把失联行渲染成仍在处理，也不得
永久阻止卡片操作。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.media_v4.persistence.schema_v4 import create_schema_v4


def _stale_heartbeat() -> str:
    return (datetime.now(UTC) - timedelta(seconds=600)).isoformat()


def _fresh_heartbeat() -> str:
    return datetime.now(UTC).isoformat()


def _seed_scan(
    database,
    *,
    scan_id: str = "scan-recover",
    root_id: str = "root-recover",
    status: str = "running",
    stage: str = "parsing",
    heartbeat_at: str | None = None,
    total_count: int = 0,
    cancel_requested: int = 0,
    request: dict | None = None,
    archive: dict | None = None,
):
    """插入一条扫描 + 可序列化请求；archive 传 dict 时写入归档引用。"""

    from app.media_v4.persistence.repositories import V4Repository

    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'baidu', 'directory_tree', ?, ?)",
            (root_id, now, now),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, total_count, "
            "heartbeat_at, started_at, cancel_requested) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (scan_id, root_id, status, stage, total_count, heartbeat_at or _stale_heartbeat(), now, cancel_requested),
        )
        conn.execute(
            "INSERT INTO source_scan_requests(scan_id, scan_kind, source_mode, request_json, "
            "input_archive_path, input_sha256, original_filename, attempts, created_at, updated_at) "
            "VALUES (?, 'tree_snapshot', 'tree_snapshot', ?, ?, ?, ?, 0, ?, ?)",
            (
                scan_id,
                json.dumps(request or {"revision_id": "rev-recover", "provider": "baidu"}, ensure_ascii=False),
                str(archive["archive_path"]) if archive else "",
                str(archive["sha256"]) if archive else "",
                str(archive["original_filename"]) if archive else "",
                now,
                now,
            ),
        )
    return V4Repository(database)


def _add_evidence(database, scan_id: str, root_id: str, relative: str):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    evidence = to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id=scan_id,
        provider="baidu",
        ingest_method="directory_tree",
        relative_path=relative,
        source_key=relative,
    ))
    from app.media_v4.persistence.repositories import V4Repository

    V4Repository(database).save_scan_evidence_bulk([evidence])
    return evidence


def test_stale_running_scan_with_cancel_request_is_cancelled_immediately(tmp_path):
    """旧心跳 + cancel_requested：直接原子收口为 cancelled，不等不存在的线程。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = V4Database(tmp_path / "recover.db")
    database.initialize()
    _seed_scan(database, cancel_requested=1)

    recovered = SourceScanRunner(database).recover_stale_scans()

    assert recovered == 1
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, finished_at FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
    assert row["status"] == "cancelled"
    assert row["finished_at"]


def test_stale_running_scan_without_input_fails_explicitly(tmp_path):
    """旧心跳 + 无证据 + 无归档：明确 failed，不永远留在 running。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = V4Database(tmp_path / "recover-fail.db")
    database.initialize()
    _seed_scan(database, stage="reading_source")

    SourceScanRunner(database).recover_stale_scans()

    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
    assert row["status"] == "failed"
    assert "重新扫描" in str(row["error"])


def test_stale_running_scan_with_intact_archive_is_requeued(tmp_path, monkeypatch):
    """旧心跳 + 归档存在且哈希一致：同一 scan_id 重新排队并增加 attempts。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.input_archive import archive_tree_input
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    import app.media_v4.sources.input_archive as archive_module

    monkeypatch.setattr(archive_module, "get_data_dir", lambda: tmp_path / "data")
    archive = archive_tree_input("root-recover", tree)

    database = V4Database(tmp_path / "recover-requeue.db")
    database.initialize()
    _seed_scan(database, stage="reading_source", archive=archive)

    SourceScanRunner(database).recover_stale_scans()

    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, stage FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
        attempts = conn.execute(
            "SELECT attempts FROM source_scan_requests WHERE scan_id = 'scan-recover'"
        ).fetchone()["attempts"]
    assert row["status"] == "queued"
    assert row["stage"] == "queued"
    assert int(attempts) == 1


def test_stale_scan_with_complete_evidence_finalizes_offline(tmp_path, monkeypatch):
    """4313/4313 后进程退出：重启后仅使用既有 evidence 完成 draft。

    不创建归档文件——证明该分支不重新读取 TXT、不访问任何源盘。
    """

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    database = V4Database(tmp_path / "recover-finalize.db")
    database.initialize()
    request = {
        "revision_id": "rev-recover",
        "provider": "baidu",
        "source_display_name": "恢复测试",
    }
    _seed_scan(database, stage="parsing", total_count=1, request=request)
    _add_evidence(database, "scan-recover", "root-recover", "Show/Show.S01E01.mkv")

    recovered = SourceScanRunner(database).recover_stale_scans()

    assert recovered == 1
    with database.connect() as conn:
        scan_row = conn.execute(
            "SELECT status, stage FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
        revision = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = 'rev-recover'"
        ).fetchone()
    assert scan_row["status"] == "completed"
    assert revision is not None and revision["status"] == "draft"


def test_fresh_heartbeat_is_not_recovered(tmp_path):
    """新鲜心跳说明仍有执行者：恢复器不得误回收。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = V4Database(tmp_path / "recover-fresh.db")
    database.initialize()
    _seed_scan(database, heartbeat_at=_fresh_heartbeat(), cancel_requested=1)

    recovered = SourceScanRunner(database).recover_stale_scans()

    assert recovered == 0
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
    assert row["status"] == "running"


def test_queued_scan_is_claimed_exactly_once(tmp_path):
    """原子领取：两个执行者竞争同一 queued 行，只有一个成功。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = V4Database(tmp_path / "claim.db")
    database.initialize()
    _seed_scan(database, status="queued", stage="queued", heartbeat_at=_fresh_heartbeat())

    runner = SourceScanRunner(database)
    first = runner.claim_next_scan()
    second = runner.claim_next_scan()

    assert first is not None and first.scan_id == "scan-recover"
    assert second is None
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
    assert row["status"] == "running"


def test_runner_persists_complete_result_after_streaming_subset(tmp_path, monkeypatch):
    """runner 结算时补齐流式 adapter 未抽查的 OpenList 基线证据。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources import source_scan_runner
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = V4Database(tmp_path / "runner-stream-complete.db")
    database.initialize()
    _seed_scan(
        database,
        status="queued",
        stage="queued",
        heartbeat_at=_fresh_heartbeat(),
        request={"revision_id": ""},
    )
    all_evidence = [
        to_source_evidence(SourceEntry(
            root_id="root-recover",
            scan_id="scan-recover",
            provider="baidu",
            ingest_method="openlist_api",
            relative_path=f"Show/Show.S01E0{episode}.mkv",
            source_key=f"/Show/Show.S01E0{episode}.mkv",
        ))
        for episode in range(1, 5)
    ]
    def streaming_handler(_database, _task, runtime):
        runtime.persist_evidence_batch(all_evidence[:2])
        return all_evidence

    monkeypatch.setattr(source_scan_runner, "get_handler", lambda _kind: streaming_handler)
    runner = SourceScanRunner(database)
    task = runner.claim_next_scan()
    assert task is not None

    runner.run_scan(task)

    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, stage, total_count FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
        stored = conn.execute(
            "SELECT relative_path FROM source_evidence WHERE scan_id = 'scan-recover' "
            "ORDER BY relative_path"
        ).fetchall()
    assert row["status"] == "completed"
    assert row["stage"] == "ready"
    assert int(row["total_count"]) == len(all_evidence)
    assert [item["relative_path"] for item in stored] == [
        item.relative_path for item in all_evidence
    ]


def test_runner_shutdown_does_not_turn_active_scan_into_user_cancel(tmp_path, monkeypatch):
    """应用关闭只停止领取新任务，不得伪造用户取消当前扫描。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources import source_scan_runner
    from app.media_v4.sources.scan_state import scan_is_stale
    from app.media_v4.sources.source_scan_runner import SourceScanRunner, _CancelledScan

    database = V4Database(tmp_path / "runner-shutdown.db")
    database.initialize()
    _seed_scan(
        database,
        status="queued",
        stage="queued",
        heartbeat_at=_fresh_heartbeat(),
        request={"revision_id": ""},
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_handler(_database, _task, runtime):
        started.set()
        release.wait(timeout=2)
        if runtime.cancellation_requested():
            raise _CancelledScan()
        return []

    monkeypatch.setattr(source_scan_runner, "get_handler", lambda _kind: blocking_handler)
    runner = SourceScanRunner(database, poll_interval=0.01, stale_after=1)
    runner.start()
    assert started.wait(timeout=2)
    worker_thread = runner._thread

    runner.stop(timeout=0.05)
    with database.connect() as conn:
        heartbeat = str(conn.execute(
            "SELECT heartbeat_at FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()[0])
    assert scan_is_stale(heartbeat, max_age_seconds=1)
    release.set()

    deadline = time.monotonic() + 2
    status = "running"
    while time.monotonic() < deadline:
        with database.connect() as conn:
            status = str(conn.execute(
                "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
            ).fetchone()[0])
        if status == "completed":
            break
        time.sleep(0.01)

    assert status == "completed"
    assert worker_thread is not None and worker_thread.daemon is True


def test_scan_recovers_after_worker_process_is_terminated(tmp_path, monkeypatch):
    """独立进程中断后，新的 runner 能从归档重新排队并收口。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.input_archive import archive_tree_input

    data_dir = tmp_path / "data"
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    tree = tmp_path / "subprocess-tree.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    archive = archive_tree_input("root-recover", tree)

    database = V4Database(tmp_path / "subprocess-recovery.db")
    database.initialize()
    _seed_scan(
        database,
        status="queued",
        stage="queued",
        heartbeat_at=_fresh_heartbeat(),
        request={"revision_id": ""},
        archive=archive,
    )

    db_path = str(tmp_path / "subprocess-recovery.db").replace("\\", "/")
    marker_path = str(tmp_path / "worker-started.txt").replace("\\", "/")
    project_root = Path(__file__).resolve().parents[2]
    worker_code = f"""
import time
from pathlib import Path

from app.media_v4.sources import source_scan_runner
from app.media_v4.sources.source_scan_runner import SourceScanRunner
from app.media_v4.persistence.database import V4Database

def handler(_database, _task, runtime):
    Path(r"{marker_path}").write_text("started", encoding="utf-8")
    while True:
        runtime.heartbeat()
        time.sleep(0.02)

source_scan_runner.get_handler = lambda _kind: handler
runner = SourceScanRunner(V4Database(r"{db_path}"), poll_interval=0.02, stale_after=1)
runner.start()
while not Path(r"{marker_path}").is_file():
    time.sleep(0.01)
while True:
    time.sleep(1)
"""
    environment = os.environ.copy()
    backend_path = str(project_root / "backend")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (backend_path, environment.get("PYTHONPATH", "")) if item
    )
    process = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        cwd=project_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        status = "queued"
        while time.monotonic() < deadline:
            with database.connect() as conn:
                status = str(conn.execute(
                    "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
                ).fetchone()[0])
            if status == "running" and os.path.isfile(marker_path):
                break
            time.sleep(0.02)
        assert status == "running"
        assert os.path.isfile(marker_path)
    finally:
        process.terminate()
        process.wait(timeout=3)

    time.sleep(1.2)
    recovery_code = f"""
import time
from app.media_v4.sources import source_scan_runner
from app.media_v4.sources.source_scan_runner import SourceScanRunner
from app.media_v4.persistence.database import V4Database

def handler(_database, _task, _runtime):
    return []

source_scan_runner.get_handler = lambda _kind: handler
database = V4Database(r"{db_path}")
runner = SourceScanRunner(database, poll_interval=0.02, stale_after=1)
runner.start()
deadline = time.monotonic() + 5
status = "running"
while time.monotonic() < deadline:
    with database.connect() as conn:
        status = str(conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()[0])
    if status == "completed":
        break
    time.sleep(0.02)
runner.stop()
if status != "completed":
    raise SystemExit("unexpected final status: " + status)
"""
    recovery = subprocess.run(
        [sys.executable, "-c", recovery_code],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert recovery.returncode == 0, recovery.stderr or recovery.stdout
    with database.connect() as conn:
        status = str(conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()[0])
    assert status == "completed"


def test_runner_loop_logs_unattributed_failures_and_keeps_running(tmp_path, caplog, monkeypatch):
    """runner 自身故障必须留下诊断，不得静默退出。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = V4Database(tmp_path / "runner-log.db")
    database.initialize()

    def broken_recovery():
        raise RuntimeError("synthetic runner failure")

    runner = SourceScanRunner(database, poll_interval=0.01)
    monkeypatch.setattr(runner, "recover_stale_scans", broken_recovery)
    with caplog.at_level(logging.ERROR, logger="app.media_v4.sources.source_scan_runner"):
        runner.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(
            record.getMessage() == "source scan worker loop failed" for record in caplog.records
        ):
            time.sleep(0.01)
        runner.stop()

    assert any(record.getMessage() == "source scan worker loop failed" for record in caplog.records)


def test_cancel_on_stale_running_scan_is_finalized_immediately(tmp_path):
    """取消失联扫描：从 running/cancelling 直接收口为 cancelled。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import cancel_durable_scan

    database = V4Database(tmp_path / "cancel-stale.db")
    database.initialize()
    _seed_scan(database)

    assert cancel_durable_scan("scan-recover", database=database) is True

    with database.connect() as conn:
        row = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
    assert row["status"] == "cancelled"


def test_cancel_on_queued_scan_is_cancelled_immediately(tmp_path):
    """queued 从未被领取：取消直接变 cancelled，不经过 cancelling。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import cancel_durable_scan

    database = V4Database(tmp_path / "cancel-queued.db")
    database.initialize()
    _seed_scan(database, status="queued", stage="queued", heartbeat_at=_fresh_heartbeat())

    assert cancel_durable_scan("scan-recover", database=database) is True

    with database.connect() as conn:
        row = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-recover'"
        ).fetchone()
    assert row["status"] == "cancelled"


def test_zombie_scan_does_not_block_card_delete(tmp_path):
    """失联的 running 行不得永久阻止来源卡删除。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.source_libraries import hide_source_card, list_source_cards

    database = V4Database(tmp_path / "zombie-card.db")
    database.initialize()
    _seed_scan(database, stage="reading_source")
    assert len(list_source_cards(database)) == 1

    assert hide_source_card(database, "root-recover")["hidden"] is True
    assert list_source_cards(database) == []


def test_zombie_scan_is_not_projected_as_active(tmp_path):
    """来源卡不得把失联行渲染成仍在处理；必须显示中断并可重新扫描。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.source_libraries import list_source_cards

    database = V4Database(tmp_path / "zombie-projection.db")
    database.initialize()
    _seed_scan(database, stage="reading_source")

    cards = list_source_cards(database)

    assert len(cards) == 1
    card = cards[0]
    assert card["overall_status"] == "needs_attention"
    assert card["active_task"] is None
    assert "中断" in card["progress"]["message"] or "重新扫描" in card["progress"]["message"]
    assert "resume" in card["available_actions"]


# ── C2：旧 v15 夹具迁移 + C3：归档幂等与篡改拒绝 ────────────────────────────


def _build_v15_database(path) -> None:
    """构造物理 v15 库：完整结构去掉 v16/v17 增量后回拨版本号。

    迁移链的中间函数依赖全量表结构，因此夹具先建完整 schema，再删除
    v15 之后的增量列与新表，模拟一个真实的旧版本库。
    """

    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        create_schema_v4(conn)
        conn.execute("DROP TABLE source_scan_requests")
        conn.execute("ALTER TABLE jobs DROP COLUMN cancel_requested")
        conn.execute("ALTER TABLE jobs DROP COLUMN heartbeat_at")
        conn.execute("ALTER TABLE jobs DROP COLUMN started_at")
        conn.execute("ALTER TABLE jobs DROP COLUMN finished_at")
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-v15', 'local', 'local_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-v15', 'root-v15', 1, 'completed', 'now')"
        )
        conn.execute(
            "INSERT INTO source_evidence(evidence_id, scan_id, root_id, source_key, relative_path, entry_kind) "
            "VALUES ('ev-v15', 'scan-v15', 'root-v15', 'Show/E01.mkv', 'Show/E01.mkv', 'video')"
        )
        conn.execute("PRAGMA user_version = 15")
        conn.commit()
    finally:
        conn.close()


def test_v15_database_fixture_survives_migration_to_v17(tmp_path):
    """旧 v15 库迁移到 v17：既有 source_scans/source_evidence 原样保留。

    v15 夹具只建三张核心表并用外键衔接真实迁移链；迁移是加法的，
    旧行的列值与新表并存，不触发任何一次性重置。
    """

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.schema_v4 import V4_SCHEMA_VERSION

    db_path = tmp_path / "legacy-v15.db"
    _build_v15_database(db_path)

    database = V4Database(db_path)
    database.initialize()

    with database.connect() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == V4_SCHEMA_VERSION
        scan = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-v15'"
        ).fetchone()
        assert scan is not None and scan["status"] == "completed"
        evidence = conn.execute(
            "SELECT COUNT(*) FROM source_evidence WHERE scan_id = 'scan-v15'"
        ).fetchone()[0]
        assert int(evidence) == 1
        requests_table = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'source_scan_requests'"
        ).fetchone()[0]
        assert int(requests_table) == 1


def test_archive_reuse_is_idempotent_for_same_content(tmp_path, monkeypatch):
    """同一内容哈希重复选择：复用不可变归档，不产生第二份文件。"""

    from app.media_v4.sources import input_archive

    data_dir = tmp_path / "data"
    monkeypatch.setattr(input_archive, "get_data_dir", lambda: data_dir)
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")

    first = input_archive.archive_tree_input("root-a", tree)
    second = input_archive.archive_tree_input("root-a", tree)

    assert first == second
    archived = list((data_dir / "source_inputs" / "root-a").glob("*.txt"))
    assert len(archived) == 1


def test_archive_rejects_tampered_content_on_recovery(tmp_path, monkeypatch):
    """归档被篡改后哈希不一致：恢复路径显式拒绝，不得使用被篡改输入。"""

    from app.media_v4.sources import input_archive

    data_dir = tmp_path / "data"
    monkeypatch.setattr(input_archive, "get_data_dir", lambda: data_dir)
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    archive = input_archive.archive_tree_input("root-a", tree)

    archived_file = input_archive.resolve_archive_path(archive["archive_path"])
    archived_file.write_text("Show/Show.S01E99.mkv\n", encoding="utf-8")

    assert input_archive.archive_is_intact(archive["archive_path"], archive["sha256"]) is False


def test_tree_scan_works_after_original_txt_is_deleted(tmp_path, monkeypatch):
    """原 TXT 被删除后，归档仍支撑扫描完成（C3：受控归档是唯一输入依据）。"""


    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    data_dir = tmp_path / "data"
    monkeypatch.setattr("app.media_v4.sources.input_archive.get_data_dir", lambda: data_dir)
    database = V4Database(tmp_path / "archived-scan.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    mount = tmp_path / "百度网盘"
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="", baidu_root=str(mount),
        openlist_mount_root="", openlist_remote_root="/", openlist_routes=[],
    ))

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)
    response = client.post("/api/v4/sources/scans", json={
        "source": "tree", "tree_file": str(tree), "provider": "baidu",
    })
    assert response.status_code == 200, response.text
    scan_id = response.json()["scan_id"]

    # 任务登记后立刻删除原 TXT：扫描与任何重试都只能依赖归档。
    tree.unlink()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = get_durable_scan(database, scan_id)["status"]
        if state not in {"queued", "running"}:
            break
        time.sleep(0.02)

    assert get_durable_scan(database, scan_id)["status"] == "completed"
