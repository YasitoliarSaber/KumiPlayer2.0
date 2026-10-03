"""请求预算耗尽后的持久延期：可取消、有自动轮数上限。

背景（用户反馈）：OpenList 完整扫描跑到请求预算后直接停在「本次巡检已达请求预算，
可继续扫描」，不点继续就什么都不做——既不像进度，也谈不上"安全缓冲"。现在改为
在队列中缓冲后从目录级 frontier 自动继续，等待期释放扫描执行线程。
"""

from __future__ import annotations

import pytest

from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.sources import scan_frontier, scan_handlers
from app.media_v4.sources.scanner import SourceScanCancelled
from app.media_v4.sources.source_scan_runner import SourceScanDeferred


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


def test_cooldown_returns_to_runner_without_waiting():
    with pytest.raises(SourceScanDeferred) as deferred:
        scan_handlers._defer_for_scan_budget(_handler_task(), _Runtime())
    assert deferred.value.delay_seconds == scan_handlers.SCAN_BUDGET_COOLDOWN_SECONDS


def test_cooldown_deferral_honours_cancellation():
    with pytest.raises(SourceScanCancelled):
        scan_handlers._defer_for_scan_budget(_handler_task(), _Runtime(cancel=True))


def test_auto_cooldown_has_a_cap_so_it_can_still_fall_back_to_manual_resume():
    # 上限存在：连续撞预算时会回到"可继续扫描"，不会无限缓冲。
    assert scan_handlers.SCAN_BUDGET_MAX_AUTO_COOLDOWNS >= 1
    assert scan_handlers.SCAN_BUDGET_COOLDOWN_SECONDS <= 30


def _handler_task(budget_cooldowns=0):
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
        budget_cooldowns=budget_cooldowns,
    )


def _handler_runtime(database):
    from types import SimpleNamespace

    return SimpleNamespace(
        cancellation_requested=lambda: False,
        persist_evidence_batch=V4Repository(database).save_scan_evidence_bulk,
        report_progress=lambda *_a, **_k: None,
    )


def _patch_handler_globals(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr("app.api.openlist_v4._client", lambda _config: object())
    monkeypatch.setattr("app.api.openlist_v4._remote_root", lambda _config: "/动画")
    monkeypatch.setattr("app.core.config.load_config", lambda: SimpleNamespace())


def test_full_scan_defers_then_continues_with_all_persisted_evidence(tmp_path, monkeypatch):
    """一轮用尽预算后退出；重新领取时延续同一身份和已落库证据。"""

    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    database = _database(tmp_path)
    rounds: list[int] = []
    scan_frontier.ensure_directories(database, scan_id="scan-c", remote_paths=["/pending"])

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
    _patch_handler_globals(monkeypatch)

    with pytest.raises(SourceScanDeferred):
        scan_handlers.scan_openlist_full_source(database, _handler_task(), _handler_runtime(database))
    evidence = scan_handlers.scan_openlist_full_source(
        database, _handler_task(1), _handler_runtime(database)
    )

    assert len(rounds) == 2
    assert len(evidence) == 1
    assert len(V4Repository(database).list_scan_evidence("scan-c")) == 2


def test_full_scan_pauses_only_after_auto_cooldown_cap(tmp_path, monkeypatch):
    """兜底仍然存在：连续撞预算超过上限时回到"可继续扫描"。"""

    import pytest

    from app.media_v4.sources.scanner import SourceScanPaused

    database = _database(tmp_path)
    scan_frontier.ensure_directories(database, scan_id="scan-c", remote_paths=["/pending"])

    def always_budget_exhausted(_client, **kwargs):
        kwargs["scan_stats"]["budget_exhausted"] = True
        kwargs["scan_stats"]["directories_listed"] = 999
        return kwargs["scan_id"], []

    monkeypatch.setattr("app.api.media_v4.scan_openlist_directory", always_budget_exhausted)
    monkeypatch.setattr(scan_handlers, "SCAN_BUDGET_MAX_AUTO_COOLDOWNS", 2)
    _patch_handler_globals(monkeypatch)

    with pytest.raises(SourceScanPaused):
        scan_handlers.scan_openlist_full_source(
            database, _handler_task(2), _handler_runtime(database)
        )
