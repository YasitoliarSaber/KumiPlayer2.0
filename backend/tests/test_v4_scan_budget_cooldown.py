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


def _handler_task():
    from types import SimpleNamespace

    return SimpleNamespace(
        request={
            "remote_root": "/动画",
            "mapping_root": "/动画",
            "mount_root": "",
            "provider": "quark",
        },
        root_id="root-c",
        scan_id="scan-c",
    )


def _handler_runtime():
    from types import SimpleNamespace

    return SimpleNamespace(
        cancellation_requested=lambda: False,
        persist_evidence_batch=lambda *_a, **_k: None,
        report_progress=lambda *_a, **_k: None,
    )


def _patch_handler_globals(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr("app.api.openlist_v4._client", lambda _config: object())
    monkeypatch.setattr("app.api.openlist_v4._remote_root", lambda _config: "/动画")
    monkeypatch.setattr("app.core.config.load_config", lambda: SimpleNamespace())


def test_full_scan_auto_continues_after_budget_instead_of_pausing(tmp_path, monkeypatch):
    """核心验证：撞到请求预算后**自动继续**，最终不是 paused，且证据被合并。"""

    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    database = _database(tmp_path)
    rounds: list[int] = []
    waits: list[int] = []

    def fake_scan(_client, **kwargs):
        rounds.append(len(rounds) + 1)
        relative = f"动画/作品/e{len(rounds)}.mkv"
        evidence = to_source_evidence(
            SourceEntry(
                root_id="root-c",
                scan_id="scan-c",
                provider="quark",
                ingest_method="openlist_scan",
                relative_path=relative,
                source_key=relative,
            )
        )
        # 第一轮假装撞预算，第二轮正常跑完
        kwargs["scan_stats"]["budget_exhausted"] = len(rounds) == 1
        kwargs["scan_stats"]["directories_listed"] = 400 * len(rounds)
        return kwargs["scan_id"], [evidence]

    monkeypatch.setattr("app.api.media_v4.scan_openlist_directory", fake_scan)
    monkeypatch.setattr(
        scan_handlers, "_wait_for_scan_budget", lambda *_a, **_k: waits.append(1)
    )
    _patch_handler_globals(monkeypatch)

    evidence = scan_handlers.scan_openlist_full_source(
        database, _handler_task(), _handler_runtime()
    )

    assert len(rounds) == 2, "撞预算后必须自动再跑一轮，而不是停下等人点"
    assert waits == [1], "自动续跑前必须先缓冲一次"
    assert len(evidence) == 2, "多轮收集的证据必须合并返回"


def test_full_scan_pauses_only_after_auto_cooldown_cap(tmp_path, monkeypatch):
    """兜底仍然存在：连续撞预算超过上限时回到"可继续扫描"。"""

    import pytest
    from app.media_v4.sources.scanner import SourceScanPaused

    database = _database(tmp_path)

    def always_budget_exhausted(_client, **kwargs):
        kwargs["scan_stats"]["budget_exhausted"] = True
        kwargs["scan_stats"]["directories_listed"] = 999
        return kwargs["scan_id"], []

    monkeypatch.setattr("app.api.media_v4.scan_openlist_directory", always_budget_exhausted)
    monkeypatch.setattr(scan_handlers, "_wait_for_scan_budget", lambda *_a, **_k: None)
    monkeypatch.setattr(scan_handlers, "SCAN_BUDGET_MAX_AUTO_COOLDOWNS", 2)
    _patch_handler_globals(monkeypatch)

    with pytest.raises(SourceScanPaused):
        scan_handlers.scan_openlist_full_source(
            database, _handler_task(), _handler_runtime()
        )
