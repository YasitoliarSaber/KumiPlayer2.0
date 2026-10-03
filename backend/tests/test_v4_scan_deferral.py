"""持久预算延期只占队列，等待期不能占住唯一来源扫描执行器。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.openlist.models import OpenListEntry, OpenListRateLimitedError
from app.media_v4.persistence.database import V4Database
from app.media_v4.sources import scan_frontier, scan_handlers, source_scan_runner
from app.media_v4.sources.durable_scan import cancel_durable_scan
from app.media_v4.sources.source_scan_runner import (
    InputArchive,
    SourceScanRunner,
    register_source_scan,
    resume_paused_scan,
)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    database = V4Database(tmp_path / "deferral.db")
    database.initialize()
    clock = {"now": datetime(2026, 10, 2, tzinfo=UTC)}
    monkeypatch.setattr(source_scan_runner, "_now", lambda: clock["now"].isoformat())
    finalized = []
    monkeypatch.setattr(SourceScanRunner, "_run_finalizer", lambda _self, task, _runtime, evidence:
                        finalized.append((task.scan_id, evidence)))

    def no_wait(*_args, **_kwargs):
        raise AssertionError("预算等待不能占住扫描线程")

    monkeypatch.setattr(scan_handlers, "_wait_for_scan_budget", no_wait, raising=False)
    monkeypatch.setattr(scan_handlers, "SCAN_BUDGET_COOLDOWN_SECONDS", 15)
    monkeypatch.setattr("app.api.openlist_v4._connection_config", lambda _identity: SimpleNamespace())
    calls = []

    class Client:
        def list_dir(self, remote_path, **_kwargs):
            calls.append(remote_path)
            if remote_path == "/Anime":
                entries = [OpenListEntry(name=name, is_dir=True, remote_path=f"/Anime/{name}", modified=number)
                           for name, number in (("A", 10.0), ("B", 20.0))]
            else:
                name = remote_path.rsplit("/", 1)[1]
                entries = [OpenListEntry(name=f"{name}.S01E01.mkv", remote_path=f"{remote_path}/{name}.S01E01.mkv")]
            return SimpleNamespace(entries=entries, total=len(entries))

    monkeypatch.setattr(scan_handlers, "_guarded_openlist_client", lambda *_args: Client())
    with database.connect() as conn:
        for root_id, provider, ingest in (("root-cloud", "quark", "openlist_scan"),
                                         ("root-tree", "baidu", "directory_tree")):
            conn.execute(
                "INSERT INTO source_roots(root_id,provider,ingest_method,created_at,updated_at) VALUES (?,?,?,?,?)",
                (root_id, provider, ingest, "now", "now"),
            )
    request = {"connection_id": "one", "remote_root": "/Anime", "mapping_root": "/Anime",
               "mount_root": "", "provider": "quark", "directory_budget": 1,
               "revision_id": "rev-cloud", "source_display_name": "Fixture"}
    register_source_scan(database, scan_id="cloud", root_id="root-cloud", scan_kind="openlist_full",
                         source_mode="openlist_full", request=request)
    return SimpleNamespace(database=database, clock=clock, calls=calls, finalized=finalized,
                           request=request, tmp_path=tmp_path)


def state(environment, scan_id="cloud"):
    with environment.database.connect() as conn:
        scan = dict(conn.execute("SELECT * FROM source_scans WHERE scan_id=?", (scan_id,)).fetchone())
        request = dict(conn.execute("SELECT * FROM source_scan_requests WHERE scan_id=?", (scan_id,)).fetchone())
    return scan, request


def advance(environment):
    environment.clock["now"] += timedelta(seconds=16)


def test_budget_deferral_leaves_worker_available_for_real_archived_txt(environment):
    from app.media_v4.sources.input_archive import archive_tree_input

    runner = SourceScanRunner(environment.database)
    runner.run_scan(runner.claim_next_scan())
    scan, request = state(environment)
    assert scan["status"] == "queued" and scan["stage"] == "budget_cooldown"
    assert request["budget_cooldowns"] == 1
    assert datetime.fromisoformat(request["resume_after"]) == environment.clock["now"] + timedelta(seconds=15)
    assert scan_frontier.directory_counts(environment.database, scan_id="cloud")["pending"] == 2
    assert not environment.finalized

    tree = environment.tmp_path / "tree.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    archived = archive_tree_input("root-tree", tree)
    register_source_scan(
        environment.database, scan_id="tree", root_id="root-tree", scan_kind="tree_snapshot",
        source_mode="tree_snapshot", request={"provider": "baidu", "effective_root": "Z:/Fixture",
                                             "identity_root": "Z:/Fixture", "resolution_ok": True},
        archive=InputArchive(archived["archive_path"], archived["sha256"], tree.name),
    )
    ready = runner.claim_next_scan()
    assert ready.scan_id == "tree"
    runner.run_scan(ready)
    assert state(environment, "tree")[0]["status"] == "completed"
    assert environment.finalized[0][0] == "tree"
    assert environment.calls == ["/Anime"]
    assert runner.claim_next_scan() is None


def test_restart_resumes_frontier_evidence_and_directory_observations(environment):
    runner = SourceScanRunner(environment.database)
    runner.run_scan(runner.claim_next_scan())
    restarted = SourceScanRunner(V4Database(environment.database.path))
    assert restarted.claim_next_scan() is None
    advance(environment)
    restarted.run_scan(restarted.claim_next_scan())
    assert state(environment)[0]["status"] == "queued"
    advance(environment)
    restarted.run_scan(restarted.claim_next_scan())
    assert state(environment)[0]["status"] == "completed"
    assert environment.calls == ["/Anime", "/Anime/A", "/Anime/B"]
    assert {e.relative_path for e in environment.finalized[0][1]} == {"A/A.S01E01.mkv", "B/B.S01E01.mkv"}
    with environment.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_evidence WHERE scan_id='cloud'").fetchone()[0] == 2
    from app.media_v4.sources.incremental import _staged_path

    checkpoint = json.loads(_staged_path("cloud").read_text(encoding="utf-8"))
    assert checkpoint["directories"]["A"]["modified"] == 10.0
    assert checkpoint["directories"]["B"]["modified"] == 20.0
    stored = json.loads(state(environment)[1]["request_json"])
    assert {key: stored[key] for key in environment.request} == environment.request
    assert not scan_frontier.directory_counts(environment.database, scan_id="cloud")["total"]


def test_budget_cap_is_persistent_and_manual_resume_resets_it(environment, monkeypatch):
    monkeypatch.setattr(scan_handlers, "SCAN_BUDGET_MAX_AUTO_COOLDOWNS", 1)
    runner = SourceScanRunner(environment.database)
    runner.run_scan(runner.claim_next_scan())
    advance(environment)
    runner = SourceScanRunner(environment.database)
    runner.run_scan(runner.claim_next_scan())
    assert state(environment)[0]["status"] == "paused"
    assert scan_frontier.directory_counts(environment.database, scan_id="cloud")["pending"] == 1
    assert resume_paused_scan(environment.database, scan_id="cloud")
    assert state(environment)[1]["budget_cooldowns"] == 0
    assert state(environment)[1]["resume_after"] == ""
    runner.run_scan(runner.claim_next_scan())
    assert state(environment)[0]["status"] == "completed"


def test_cancel_before_resume_deadline_does_not_make_another_request(environment):
    runner = SourceScanRunner(environment.database)
    runner.run_scan(runner.claim_next_scan())
    assert cancel_durable_scan("cloud", database=environment.database)
    assert state(environment)[0]["status"] == "cancelled"
    advance(environment)
    assert SourceScanRunner(environment.database).claim_next_scan() is None
    assert environment.calls == ["/Anime"]
    assert not environment.finalized
    assert scan_frontier.directory_counts(environment.database, scan_id="cloud")["total"] == 0
    assert state(environment)[1]["resume_after"] == ""


def test_upstream_429_is_not_local_budget_deferral(environment, monkeypatch):
    def limited(*_args, **_kwargs):
        raise OpenListRateLimitedError(retry_after=300)

    monkeypatch.setattr("app.api.media_v4.scan_openlist_directory", limited)
    runner = SourceScanRunner(environment.database)
    runner.run_scan(runner.claim_next_scan())
    scan, request = state(environment)
    assert scan["status"] == "failed"
    assert request["resume_after"] == "" and request["budget_cooldowns"] == 0
    assert runner.claim_next_scan() is None


def test_cancellation_racing_with_deferral_cannot_requeue_scan(environment, monkeypatch):
    defer = scan_handlers._defer_for_scan_budget

    def cancelling(task, runtime):
        with environment.database.connect() as conn:
            conn.execute("UPDATE source_scans SET status='cancelling',cancel_requested=1 WHERE scan_id=?",
                         (task.scan_id,))
        # 在 handler 已通过取消检查、runner 准备提交延期之间到达取消。
        defer(task)

    monkeypatch.setattr(scan_handlers, "_defer_for_scan_budget", cancelling)
    runner = SourceScanRunner(environment.database)
    runner.run_scan(runner.claim_next_scan())
    scan, request = state(environment)
    assert scan["status"] == "cancelled"
    assert request["resume_after"] == "" and request["budget_cooldowns"] == 0
    assert scan_frontier.directory_counts(environment.database, scan_id="cloud")["total"] == 0
    assert not environment.finalized
