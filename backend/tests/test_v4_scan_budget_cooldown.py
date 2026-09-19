"""请求预算耗尽后的自动缓冲：可取消、保持心跳、有自动轮数上限。

背景（用户反馈）：OpenList 完整扫描跑到请求预算后直接停在「本次巡检已达请求预算，
可继续扫描」，不点继续就什么都不做——既不像进度，也谈不上"安全缓冲"。现在改为
缓冲一小段时间后从目录级 frontier **自动继续**；只有连续多轮仍撞预算才交回人工。
"""

from __future__ import annotations

import time

import pytest
from app.media_v4.persistence.database import V4Database
from app.media_v4.sources import scan_handlers
from app.media_v4.sources.scanner import SourceScanCancelled


class _Runtime:
    def __init__(self, cancel: bool = False):
        self._cancel = cancel

    def cancellation_requested(self) -> bool:
        return self._cancel


def _database(tmp_path) -> V4Database:
    database = V4Database(tmp_path / "cooldown.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-c', 'quark', 'openlist_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-c', 'root-c', 1, 'running', 'reading_source', 'stale', 'now')"
        )
    return database


def _fake_clock(monkeypatch) -> dict[str, float]:
    """假时钟：sleep 推进时间而不是真的等待，测试保持毫秒级。"""

    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def _sleep(seconds: float) -> None:
        clock["now"] += max(0.0, float(seconds))

    monkeypatch.setattr(time, "sleep", _sleep)
    return clock


def test_cooldown_wait_refreshes_heartbeat_until_deadline(tmp_path, monkeypatch):
    database = _database(tmp_path)
    _fake_clock(monkeypatch)

    scan_handlers._wait_for_scan_budget(database, scan_id="scan-c", runtime=_Runtime())

    with database.connect() as conn:
        heartbeat = conn.execute(
            "SELECT heartbeat_at FROM source_scans WHERE scan_id = 'scan-c'"
        ).fetchone()["heartbeat_at"]
    assert str(heartbeat) != "stale", "缓冲期间必须持续刷新心跳，否则会被当成僵尸扫描"
    assert str(heartbeat) != ""


def test_cooldown_wait_honours_cancellation(tmp_path, monkeypatch):
    database = _database(tmp_path)
    _fake_clock(monkeypatch)

    with pytest.raises(SourceScanCancelled):
        scan_handlers._wait_for_scan_budget(
            database, scan_id="scan-c", runtime=_Runtime(cancel=True)
        )


def test_auto_cooldown_has_a_cap_so_it_can_still_fall_back_to_manual_resume():
    # 上限存在：连续撞预算时会回到"可继续扫描"，不会无限缓冲。
    assert scan_handlers.SCAN_BUDGET_MAX_AUTO_COOLDOWNS >= 1
    assert scan_handlers.SCAN_BUDGET_COOLDOWN_SECONDS <= 30
