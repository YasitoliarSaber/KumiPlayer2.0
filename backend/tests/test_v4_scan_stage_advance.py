"""O9：读取完成必须推进扫描阶段，否则失联恢复只能整库重扫。

背景：`recover_stale_scans` 的"离线续跑"分支要求 `stage ∈ {parsing, normalizing,
preparing_preview, recognizing}`，用以区分"读取已完成、只差 finalizer"与"读到一半
就崩了"。但执行期没有任何代码推进阶段——handler 全程只写 `reading_source`，第一次
变成 `parsing` 要等 finalizer 上报进度。于是"最后一批证据已落库、finalizer 尚未开始"
这一刻崩溃，会被判成"上次扫描意外中断，请重新扫描"。

**不能**只删掉阶段条件：`total_count` 在读取期间会被同步到 processed_count
（`durable_scan._update_scan_progress`），读到一半崩溃时两个数字同样相等，
那样会把残缺证据当成完整证据建立 draft。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta


def _stale_heartbeat() -> str:
    return (datetime.now(UTC) - timedelta(seconds=600)).isoformat()


def _seed_scan(database, *, scan_id: str = "scan-stage", root_id: str = "root-stage", request: dict) -> None:
    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'baidu', 'directory_tree', ?, ?)",
            (root_id, now, now),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES (?, ?, 1, 'queued', 'queued', ?, ?)",
            (scan_id, root_id, now, now),
        )
        conn.execute(
            "INSERT INTO source_scan_requests(scan_id, scan_kind, source_mode, request_json, "
            "input_archive_path, input_sha256, original_filename, attempts, created_at, updated_at) "
            "VALUES (?, 'tree_snapshot', 'tree_snapshot', ?, '', '', '', 0, ?, ?)",
            (scan_id, json.dumps(request, ensure_ascii=False), now, now),
        )


def _evidence(scan_id: str, root_id: str, relative: str):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id=scan_id,
        provider="baidu",
        ingest_method="directory_tree",
        relative_path=relative,
        source_key=relative,
    ))


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "stage.db")
    database.initialize()
    return database


def test_read_completion_advances_stage_before_finalizer(tmp_path, monkeypatch):
    """读取完成、finalizer 即将开始时的阶段必须是 parsing。

    注意区分两种"失败"：进程被杀时 `_finish_scan` 不会执行，数据库里留下的是
    finalizer 开始前那一刻的状态（本用例断言的就是它）；而被捕获的异常会把阶段
    一并写成终态 `failed`（那是正确的收口语义，不是本用例要覆盖的场景）。
    """

    from app.media_v4.sources import source_scan_runner as runner_module
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = _database(tmp_path)
    request = {"revision_id": "rev-stage", "provider": "baidu", "source_display_name": "阶段测试"}
    _seed_scan(database, request=request)
    evidence = [_evidence("scan-stage", "root-stage", "Show/Show.S01E01.mkv")]

    def fake_handler(_database, _task, _runtime):
        return evidence

    monkeypatch.setattr(runner_module, "get_handler", lambda _kind: fake_handler)
    seen: dict[str, str] = {}

    def exploding_finalizer(*_args, **_kwargs):
        with database.connect() as conn:
            seen["stage"] = str(conn.execute(
                "SELECT stage FROM source_scans WHERE scan_id = 'scan-stage'"
            ).fetchone()["stage"])
        raise RuntimeError("finalizer 崩了")

    monkeypatch.setattr(runner_module, "draft_finalizer", exploding_finalizer)

    runner = SourceScanRunner(database)
    task = runner.claim_next_scan()
    assert task is not None
    runner.run_scan(task)

    assert seen.get("stage") == "parsing", (
        f"finalizer 开始前阶段必须已是 parsing（读到一半崩溃才应停在 reading_source），实际 {seen.get('stage')}"
    )
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, stage, total_count FROM source_scans WHERE scan_id = 'scan-stage'"
        ).fetchone()
    assert row["status"] == "failed", "被捕获的异常仍应收口为 failed"
    assert int(row["total_count"]) == len(evidence)


def test_recovery_resumes_offline_when_read_had_finished(tmp_path, monkeypatch):
    """同一条扫描把心跳改旧后恢复：必须走离线续跑，而不是"请重新扫描"。"""

    from app.media_v4.sources import source_scan_runner as runner_module
    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = _database(tmp_path)
    request = {"revision_id": "rev-stage", "provider": "baidu", "source_display_name": "阶段测试"}
    _seed_scan(database, request=request)
    evidence = [_evidence("scan-stage", "root-stage", "Show/Show.S01E01.mkv")]

    monkeypatch.setattr(
        runner_module, "get_handler", lambda _kind: (lambda _db, _task, _runtime: evidence)
    )

    def exploding_finalizer(*_args, **_kwargs):
        raise RuntimeError("finalizer 崩了")

    monkeypatch.setattr(runner_module, "draft_finalizer", exploding_finalizer)
    runner = SourceScanRunner(database)
    runner.run_scan(runner.claim_next_scan())

    # 恢复 finalizer，并把这条失败行改回失联的 running 状态。
    monkeypatch.undo()
    monkeypatch.setattr(
        runner_module, "get_handler", lambda _kind: (lambda _db, _task, _runtime: evidence)
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_scans SET status = 'running', stage = 'parsing', heartbeat_at = ?, "
            "finished_at = '', error = '' WHERE scan_id = 'scan-stage'",
            (_stale_heartbeat(),),
        )

    recovered = SourceScanRunner(database).recover_stale_scans()

    assert recovered == 1
    with database.connect() as conn:
        scan = conn.execute(
            "SELECT status, error FROM source_scans WHERE scan_id = 'scan-stage'"
        ).fetchone()
        revision = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = 'rev-stage'"
        ).fetchone()
    assert scan["status"] == "completed", f"应离线续跑完成，实际 {scan['status']} / {scan['error']}"
    assert revision is not None and revision["status"] == "draft"


def test_partial_read_is_not_treated_as_complete(tmp_path):
    """反证：读到一半崩溃（阶段仍是 reading_source）绝不能被当成"证据完整"。

    `total_count` 在读取期间会被同步到 processed_count，所以两个数字相等并不能证明
    读取结束——这正是恢复分支必须同时看阶段的原因。
    """

    from app.media_v4.sources.source_scan_runner import SourceScanRunner

    database = _database(tmp_path)
    request = {"revision_id": "rev-partial", "provider": "baidu"}
    _seed_scan(database, scan_id="scan-stage", request=request)
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_scans SET status = 'running', stage = 'reading_source', total_count = 1, "
            "processed_count = 1, heartbeat_at = ? WHERE scan_id = 'scan-stage'",
            (_stale_heartbeat(),),
        )
    from app.media_v4.persistence.repositories import V4Repository

    V4Repository(database).save_scan_evidence_bulk([_evidence("scan-stage", "root-stage", "A.mkv")])

    recovered = SourceScanRunner(database).recover_stale_scans()

    assert recovered == 1
    with database.connect() as conn:
        scan = conn.execute(
            "SELECT status, error FROM source_scans WHERE scan_id = 'scan-stage'"
        ).fetchone()
        revision = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = 'rev-partial'"
        ).fetchone()
    assert scan["status"] == "failed"
    assert "重新扫描" in str(scan["error"])
    assert revision is None, "残缺读取不得建立 draft"
