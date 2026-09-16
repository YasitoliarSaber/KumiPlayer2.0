"""O13：终态扫描的断点必须清理，paused 必须保留。

`source_scan_directories`（按目录数增长）与 `openlist_incremental/staged/<scan_id>.json`
（每个 scan 一个文件）都是"进行中的断点"，只在续扫时有意义。失败/取消的扫描永远不会
再被确认，留着它们会随每次失败/取消单调增长；而 paused 的扫描要能续扫，断点必须保留。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime


def _database(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "cleanup.db")
    database.initialize()
    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-clean', 'baidu', 'directory_tree', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-clean', 'root-clean', 1, 'running', 'reading_source', ?, ?)",
            (now, now),
        )
    return database


def _seed_checkpoints(database, tmp_path, scan_id: str = "scan-clean") -> None:
    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.incremental import stage_scan_state

    scan_frontier.ensure_directories(
        database, scan_id=scan_id, remote_paths=["/Anime", "/Anime/Show"], depth=0,
    )
    stage_scan_state(scan_id, {
        "version": 1,
        "root_id": "root-clean",
        "remote_root": "/Anime",
        "remote_verified": True,
        "directories": {"": {"modified": None, "last_verified_at": 0}},
    })


def _staged_files(tmp_path) -> list[str]:
    staged = tmp_path / "data" / "openlist_incremental" / "staged"
    return sorted(path.name for path in staged.glob("*.json")) if staged.is_dir() else []


def _seeded(tmp_path, monkeypatch):
    database = _database(tmp_path, monkeypatch)
    _seed_checkpoints(database, tmp_path)
    assert _staged_files(tmp_path), "前置条件：暂存检查点已写入"
    return database


def test_failed_scan_clears_frontier_and_staged_checkpoint(tmp_path, monkeypatch):
    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.source_scan_runner import _finish_scan

    database = _seeded(tmp_path, monkeypatch)

    _finish_scan(database, "scan-clean", status="failed", error="上次扫描意外中断，请重新扫描")

    assert scan_frontier.directory_counts(database, scan_id="scan-clean")["total"] == 0
    assert _staged_files(tmp_path) == []


def test_cancelled_scan_clears_frontier_and_staged_checkpoint(tmp_path, monkeypatch):
    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.source_scan_runner import _finish_scan

    database = _seeded(tmp_path, monkeypatch)

    _finish_scan(database, "scan-clean", status="cancelled", error="用户已取消扫描")

    assert scan_frontier.directory_counts(database, scan_id="scan-clean")["total"] == 0
    assert _staged_files(tmp_path) == []


def test_paused_scan_keeps_its_checkpoints_for_resume(tmp_path, monkeypatch):
    """反证：paused 必须保留断点，否则「继续扫描」就失去意义。"""

    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.source_scan_runner import _finish_scan

    database = _seeded(tmp_path, monkeypatch)

    _finish_scan(database, "scan-clean", status="paused", error="本次巡检已达请求预算")

    assert scan_frontier.directory_counts(database, scan_id="scan-clean")["total"] == 2
    assert len(_staged_files(tmp_path)) == 1


def test_discard_is_idempotent_when_no_checkpoint_exists(tmp_path, monkeypatch):
    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.source_scan_runner import _finish_scan

    database = _database(tmp_path, monkeypatch)

    # 没有断点也要能收口（幂等），不能因为清理抛错让终态写不进去。
    _finish_scan(database, "scan-clean", status="failed", error="失败")

    assert scan_frontier.directory_counts(database, scan_id="scan-clean")["total"] == 0
    with database.connect() as conn:
        assert str(conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-clean'"
        ).fetchone()["status"]) == "failed"


def test_staged_state_content_is_valid_json_before_cleanup(tmp_path, monkeypatch):
    """占位校验：清理针对的确实是可解析的暂存状态（避免把别的东西删掉）。"""

    _seeded(tmp_path, monkeypatch)
    staged = tmp_path / "data" / "openlist_incremental" / "staged"
    payload = json.loads(next(staged.glob("*.json")).read_text(encoding="utf-8"))

    assert payload["root_id"] == "root-clean"
    assert payload["directories"]
