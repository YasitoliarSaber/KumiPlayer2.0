"""Ongoing acceptance through real scanners, durable runner, finalizer and V4 jobs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import media_v4, tracking_v4
from app.core.config import AppConfig
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.playback.store import V4PlaybackStore
from app.media_v4.projection.library import V4LibraryProjection
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.source_scan_runner import SourceScanRunner
from app.media_v4.tracking.refresh import list_refresh_sources, refresh_all_sources

from .source_disk_guard import guard_source_disk_io
from .test_v4_media_import_lifecycle_contract import bindings, external_provider, immutable_rows, setup


def empty_video(source: Path, relative: str) -> Path:
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def run_registered(database, scan_id: str) -> str:
    runner = SourceScanRunner(database)
    task = runner.claim_next_scan()
    assert task is not None and task.scan_id == scan_id
    runner.run_scan(task)
    with database.connect() as conn:
        state = dict(conn.execute("SELECT * FROM source_scans WHERE scan_id=?", (scan_id,)).fetchone())
        request = json.loads(conn.execute("SELECT request_json FROM source_scan_requests WHERE scan_id=?", (scan_id,)).fetchone()[0])
    assert state["status"] == "completed", state
    return request["revision_id"]


def environment(tmp_path, monkeypatch):
    import builtins

    database, source, mirror = setup(tmp_path, monkeypatch)
    config = AppConfig(artwork_storage_mode="local")
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(tracking_v4, "load_config", lambda: config)
    monkeypatch.setattr("app.core.config.load_config", lambda: config)
    monkeypatch.setattr("app.media_v4.sources.source_scan_runner.get_source_scan_runner",
                        lambda _: SimpleNamespace(wake=lambda: None))
    real_path_open, real_open = Path.open, builtins.open
    def fixture_write_only(path, mode="r", *args, **kwargs):
        assert Path(path).suffix != ".mkv" or mode not in {"r", "rb"}, "media fixture must never be read"
        return real_path_open(path, mode, *args, **kwargs)
    def no_media_reads(path, mode="r", *args, **kwargs):
        if isinstance(path, (str, Path)):
            assert Path(path).suffix != ".mkv" or mode not in {"r", "rb"}, "media fixture must never be read"
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fixture_write_only)
    monkeypatch.setattr(builtins, "open", no_media_reads)
    return database, source, mirror, config


def confirm_ready(database, revision, mirror):
    service = V4RevisionService(database)
    assert service.get_status(revision) == "draft"
    service.confirm(revision)
    V4JobRunner(database, metadata_provider=external_provider).process_available(mirror_root=mirror)


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    database, source, mirror, _config = environment(tmp_path, monkeypatch)
    first_file = empty_video(source, "Show/Show.S01E01.mkv")
    requested = media_v4._start_durable_local_scan(media_v4.SourceScanRequest(
        source="local", root_path=str(source), content_scope="ongoing", revision_id="first",
    ))
    assert run_registered(database, requested["scan_id"]) == "first"
    confirm_ready(database, "first", mirror)
    first = bindings(database, "first")["Show/Show.S01E01.mkv"]
    with database.connect() as conn:
        metadata = conn.execute("SELECT status, metadata_json FROM scrape_bindings WHERE revision_id='first' AND work_id=?",
                                (first["work_id"],)).fetchone()
        assert metadata["status"] == "confirmed"
        assert json.loads(metadata["metadata_json"])["metadata_state"] == "ready"
        assert conn.execute("SELECT COUNT(*) FROM provider_bindings WHERE work_id=? AND provider='tmdb'", (first["work_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status!='succeeded'").fetchone()[0] == 0
    store = V4PlaybackStore(database)
    store.save_progress(first["work_id"], first["episode_id"], first["asset_id"], 123, 1500, False)
    return SimpleNamespace(database=database, source=source, mirror=mirror, root_id=requested["root_id"],
                           first=first, first_file=first_file, stat=first_file.stat(), store=store,
                           immutable=immutable_rows(database))


def refresh(context, *, include_scrape=True) -> str:
    result = refresh_all_sources(context.database, include_scrape=include_scrape)
    assert len(result) == 1 and result[0]["root_id"] == context.root_id
    assert result[0]["status"] == "queued", result
    return run_registered(context.database, result[0]["task_id"])


def assert_baseline_preserved(context):
    assert context.store.get_progress(context.first["episode_id"], context.first["asset_id"])["position"] == 123
    after = immutable_rows(context.database)
    for table, rows in context.immutable.items():
        assert all(after[table][key] == value for key, value in rows.items()), table
    with context.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM works WHERE work_id=?", (context.first["work_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE asset_id=?", (context.first["asset_id"],)).fetchone()[0] == 1


def test_local_new_episode_auto_confirms_scrapes_and_unchanged_does_not_duplicate(baseline):
    context = baseline
    empty_video(context.source, "Show/Show.S01E02.mkv")
    revision = refresh(context)
    service = V4RevisionService(context.database)
    assert service.get_status(revision) == "confirmed"
    updated = bindings(context.database, revision)
    old = updated["Show/Show.S01E01.mkv"]
    new = updated["Show/Show.S01E02.mkv"]
    assert all(old[key] == context.first[key] for key in ("work_id", "episode_id", "asset_id"))
    assert new["work_id"] == old["work_id"] and new["episode_id"] != old["episode_id"]
    assert_baseline_preserved(context)
    assert context.first_file.stat().st_size == context.stat.st_size == 0
    assert context.first_file.stat().st_mtime_ns == context.stat.st_mtime_ns
    assert list_refresh_sources(context.database)[0]["activity_kind"] == "execution"
    V4JobRunner(context.database, metadata_provider=external_provider).process_available(mirror_root=context.mirror)
    card = next(card for card in V4LibraryProjection(context.database).ensure_current().cards if card["work_id"] == old["work_id"])
    assert card["metadata"]["metadata_state"] == "ready"
    assert card["metadata"]["episode_mapping_status"] == "complete"
    mappings = {item["episode_id"]: item for item in card["metadata"]["episode_mappings"]}
    assert set(mappings) == {old["episode_id"], new["episode_id"]}
    assert mappings[new["episode_id"]]["title"] and mappings[new["episode_id"]]["still_url"]
    with context.database.connect() as conn:
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("jobs", "works", "episodes", "assets")}
    unchanged = refresh(context)
    assert service.get_status(unchanged) == "superseded"
    with context.database.connect() as conn:
        assert before == {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    source = list_refresh_sources(context.database)[0]
    assert source["status"] == "completed" and source["latest_revision_id"] == revision and not source["draft_revision_id"]
    assert_baseline_preserved(context)


@pytest.mark.parametrize("change", ["new_work", "new_season", "no_number", "replacement", "missing", "preview_requested", "duplicate_episode"])
def test_uncertain_changes_remain_preview_and_preserve_confirmed_facts(baseline, tmp_path, change):
    context = baseline
    if change == "new_work":
        empty_video(context.source, "Other/Other.S01E01.mkv")
    elif change == "new_season":
        empty_video(context.source, "Show/Show.S02E01.mkv")
    elif change == "no_number":
        empty_video(context.source, "Show/Show.mkv")
    elif change == "replacement":
        # Change only the fixture file's size; no media bytes are read or supplied.
        with context.first_file.open("r+b") as fixture:
            fixture.truncate(32)
    elif change == "missing":
        context.first_file.rename(tmp_path / "retained-empty-fixture.mkv")
    elif change == "duplicate_episode":
        empty_video(context.source, "Show/Show.S01E01.2160p.mkv")
    else:
        empty_video(context.source, "Show/Show.S01E02.mkv")
    with context.database.connect() as conn:
        before_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    revision = refresh(context, include_scrape=change != "preview_requested")
    assert V4RevisionService(context.database).get_status(revision) == "draft"
    assert_baseline_preserved(context)
    with context.database.connect() as conn:
        assert conn.execute("SELECT status FROM import_revisions WHERE revision_id='first'").fetchone()[0] == "confirmed"
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
    source = list_refresh_sources(context.database)[0]
    assert source["status"] == "needs_confirmation" and source["draft_revision_id"] == revision
    assert refresh_all_sources(context.database)[0]["status"] == "needs_confirmation"


@pytest.mark.parametrize("conflict", ["changed_scope", "wrong_root", "nested_scope"])
def test_scope_or_target_conflict_rolls_back_registration_without_changing_baseline(baseline, conflict):
    from fastapi import HTTPException

    context = baseline
    with context.database.connect() as conn:
        before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                  for table in ("source_roots", "source_scans", "source_scan_requests", "import_revisions", "jobs")}
    if conflict == "changed_scope":
        request = media_v4.SourceScanRequest(source="local", root_path=str(context.source), target_root_id=context.root_id,
                                             content_scope="completed", revision_id="conflict")
    else:
        child = context.source / "nested"
        child.mkdir()
        request = media_v4.SourceScanRequest(source="local", root_path=str(child),
                                             target_root_id=context.root_id if conflict == "wrong_root" else "",
                                             content_scope="ongoing" if conflict == "wrong_root" else "completed", revision_id="conflict")
    with pytest.raises(HTTPException) as error:
        media_v4._start_durable_local_scan(request)
    assert error.value.status_code == 409
    with context.database.connect() as conn:
        assert before == {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in before}
    assert_baseline_preserved(context)


def test_txt_new_export_is_copied_and_auto_appended_without_media_disk_io(tmp_path, monkeypatch):
    import builtins

    database, _source, mirror, config = environment(tmp_path, monkeypatch)
    mount = tmp_path / "snapshot-mount"
    export_dir = mount / "Ongoing"
    export_dir.mkdir(parents=True)
    old_txt, new_txt = export_dir / "Ongoing_目录树_20261001.txt", export_dir / "Ongoing_目录树_20261002.txt"
    old_txt.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    new_txt.write_text("Show/Show.S01E01.mkv\nShow/Show.S01E02.mkv\n", encoding="utf-8")
    config.baidu_root = str(mount)
    guard_source_disk_io(monkeypatch, [mount])
    copies = []
    real_open = builtins.open
    def copied_once(path, mode="r", *args, **kwargs):
        if Path(path) in (old_txt, new_txt):
            copies.append((Path(path).name, mode))
            assert mode == "rb"
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", copied_once)
    first = media_v4._start_durable_tree_scan(media_v4.SourceScanRequest(
        source="tree", provider="baidu", tree_file=str(old_txt), content_scope="ongoing", revision_id="tree-first",
    ))
    assert run_registered(database, first["scan_id"]) == "tree-first"
    confirm_ready(database, "tree-first", mirror)
    old = bindings(database, "tree-first")["Show/Show.S01E01.mkv"]
    store = V4PlaybackStore(database)
    store.save_progress(old["work_id"], old["episode_id"], old["asset_id"], 123, 1500, False)
    before = immutable_rows(database)
    assert refresh_all_sources(database)[0]["status"] == "needs_new_txt"
    update = media_v4._start_durable_tree_scan(media_v4.SourceScanRequest(
        source="tree", provider="baidu", tree_file=str(new_txt), target_root_id=first["root_id"],
        content_scope="ongoing", revision_id="tree-next",
    ))
    assert update["root_id"] == first["root_id"]
    assert run_registered(database, update["scan_id"]) == "tree-next"
    assert V4RevisionService(database).get_status("tree-next") == "confirmed"
    V4JobRunner(database, metadata_provider=external_provider).process_available(mirror_root=mirror)
    current = bindings(database, "tree-next")
    assert {key: current["Show/Show.S01E01.mkv"][key] for key in ("work_id", "episode_id", "asset_id")} == {
        key: old[key] for key in ("work_id", "episode_id", "asset_id")}
    assert current["Show/Show.S01E02.mkv"]["work_id"] == old["work_id"]
    assert store.get_progress(old["episode_id"], old["asset_id"])["position"] == 123
    after = immutable_rows(database)
    assert all(after[table][key] == value for table, rows in before.items() for key, value in rows.items())
    assert copies == [(old_txt.name, "rb"), (new_txt.name, "rb")]
    card = next(card for card in V4LibraryProjection(database).ensure_current().cards if card["work_id"] == old["work_id"])
    assert card["metadata"]["metadata_state"] == "ready" and card["metadata"]["episode_mapping_status"] == "complete"


@pytest.mark.parametrize("source_kind", ["openlist", "hybrid"])
def test_remote_real_incremental_scan_auto_appends_and_unchanged_keeps_jobs(tmp_path, monkeypatch, source_kind):
    from app.api import openlist_v4
    from app.integrations.openlist.models import OpenListEntry
    from app.integrations.openlist.providers import OpenListRouteConfig

    database, _source, mirror, config = environment(tmp_path, monkeypatch)
    mount = tmp_path / "remote-mount"
    config.openlist_mount_root = str(mount)
    config.openlist_server_url = "https://offline.example.test"
    config.openlist_remote_root = "/"
    config.openlist_routes = [OpenListRouteConfig(route_id="ongoing", remote_prefix="/Ongoing", provider_id="baidu")]
    monkeypatch.setattr(media_v4, "_select_openlist_config", lambda _: config)
    monkeypatch.setattr(media_v4, "_openlist_credentials", lambda _: ("fixture-user", "", "found"))
    monkeypatch.setattr(tracking_v4, "resolve_openlist_credentials", lambda: ("fixture-user", "", "found"))
    monkeypatch.setattr(openlist_v4, "_connection_config", lambda _: config)
    calls, numbers, sizes = [], [1], {1: 0, 2: 0}
    class Client:
        def list_dir(self, path, **kwargs):
            calls.append(path)
            assert kwargs["refresh"] is False
            if path == "/Ongoing":
                entries = [OpenListEntry(name="Show", is_dir=True, remote_path="/Ongoing/Show", modified=100.0)]
            else:
                assert path == "/Ongoing/Show"
                entries = [OpenListEntry(name=f"Show.S01E{number:02d}.mkv", remote_path=f"{path}/Show.S01E{number:02d}.mkv",
                                         size=sizes[number], modified=10.0) for number in numbers]
            return SimpleNamespace(entries=entries, total=len(entries), skipped_entries=0)
    monkeypatch.setattr(openlist_v4, "_client", lambda _: Client())
    guard_source_disk_io(monkeypatch, [mount])
    tree = tmp_path / "Ongoing_目录树_20261001.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    first = media_v4.start_durable_scan(media_v4.SourceScanRequest(
        source=source_kind, root_path="/Ongoing", tree_file=str(tree) if source_kind == "hybrid" else "",
        scan_mode="full", content_scope="ongoing", revision_id="remote-first",
    ))
    assert run_registered(database, first["scan_id"]) == "remote-first"
    confirm_ready(database, "remote-first", mirror)
    old = bindings(database, "remote-first")["Show/Show.S01E01.mkv"]
    store = V4PlaybackStore(database)
    store.save_progress(old["work_id"], old["episode_id"], old["asset_id"], 123, 1500, False)
    numbers.append(2)
    result = refresh_all_sources(database)[0]
    assert result["status"] == "queued"
    revision = run_registered(database, result["task_id"])
    assert V4RevisionService(database).get_status(revision) == "confirmed"
    current = bindings(database, revision)
    assert {key: current["Show/Show.S01E01.mkv"][key] for key in ("work_id", "episode_id", "asset_id")} == {
        key: old[key] for key in ("work_id", "episode_id", "asset_id")}
    assert current["Show/Show.S01E02.mkv"]["work_id"] == old["work_id"]
    V4JobRunner(database, metadata_provider=external_provider).process_available(mirror_root=mirror)
    card = next(card for card in V4LibraryProjection(database).ensure_current().cards if card["work_id"] == old["work_id"])
    assert card["metadata"]["metadata_state"] == "ready" and card["metadata"]["episode_mapping_status"] == "complete"
    assert store.get_progress(old["episode_id"], old["asset_id"])["position"] == 123
    with database.connect() as conn:
        before_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    repeated = refresh_all_sources(database)[0]
    unchanged = run_registered(database, repeated["task_id"])
    assert V4RevisionService(database).get_status(unchanged) == "superseded"
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
    assert set(calls) == {"/Ongoing", "/Ongoing/Show"}
    sizes[1] = 32
    replacement = refresh_all_sources(database)[0]
    replacement_revision = run_registered(database, replacement["task_id"])
    assert V4RevisionService(database).get_status(replacement_revision) == "draft"
    assert store.get_progress(old["episode_id"], old["asset_id"])["position"] == 123
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs


def test_cancelled_refresh_can_restart_without_losing_baseline_or_progress(baseline):
    from app.media_v4.sources.durable_scan import cancel_durable_scan

    context = baseline
    empty_video(context.source, "Show/Show.S01E02.mkv")
    queued = refresh_all_sources(context.database)[0]
    assert cancel_durable_scan(queued["task_id"], database=context.database)
    assert SourceScanRunner(context.database).claim_next_scan() is None
    assert list_refresh_sources(context.database)[0]["status"] == "cancelled"
    with context.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_revisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status!='succeeded'").fetchone()[0] == 0
    assert_baseline_preserved(context)
    retried = refresh_all_sources(context.database)[0]
    assert retried["task_id"] != queued["task_id"]
    revision = run_registered(context.database, retried["task_id"])
    assert V4RevisionService(context.database).get_status(revision) == "confirmed"
    V4JobRunner(context.database, metadata_provider=external_provider).process_available(mirror_root=context.mirror)
    assert list_refresh_sources(context.database)[0]["status"] == "completed"
    assert_baseline_preserved(context)


@pytest.mark.parametrize("crash_after_confirmation", [False, True])
def test_restart_recovers_real_evidence_and_finalizer_without_repeat_scan_or_jobs(baseline, monkeypatch, crash_after_confirmation):
    from app.media_v4.persistence.database import V4Database

    context = baseline
    empty_video(context.source, "Show/Show.S01E02.mkv")
    queued = refresh_all_sources(context.database)[0]
    runner = SourceScanRunner(context.database)
    task = runner.claim_next_scan()
    real_finalize = runner._run_finalizer
    def interrupt_after_read(task, runtime, evidence):
        assert len(evidence) == 2
        if crash_after_confirmation:
            real_finalize(task, runtime, evidence)
        raise KeyboardInterrupt("simulated process interruption at the persistent finalizer boundary")
    monkeypatch.setattr(runner, "_run_finalizer", interrupt_after_read)
    with pytest.raises(KeyboardInterrupt):
        runner.run_scan(task)
    with context.database.connect() as conn:
        scan = conn.execute("SELECT status,total_count FROM source_scans WHERE scan_id=?", (queued["task_id"],)).fetchone()
        assert scan["status"] == "running" and scan["total_count"] == 2
        request = json.loads(conn.execute("SELECT request_json FROM source_scan_requests WHERE scan_id=?", (queued["task_id"],)).fetchone()[0])
        conn.execute("UPDATE source_scans SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE scan_id=?", (queued["task_id"],))
        before_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with monkeypatch.context() as recovery_guard:
        guard_source_disk_io(recovery_guard, [context.source])
        restarted = SourceScanRunner(V4Database(context.database.path))
        assert restarted.recover_stale_scans() == 1
        assert restarted.recover_stale_scans() == 0
    assert V4RevisionService(context.database).get_status(request["revision_id"]) == "confirmed"
    with context.database.connect() as conn:
        assert conn.execute("SELECT status FROM source_scans WHERE scan_id=?", (queued["task_id"],)).fetchone()[0] == "completed"
        after_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert after_jobs == before_jobs if crash_after_confirmation else after_jobs > before_jobs
        assert conn.execute("SELECT COUNT(*) FROM source_evidence WHERE scan_id=?", (queued["task_id"],)).fetchone()[0] == 2
    V4JobRunner(context.database, metadata_provider=external_provider).process_available(mirror_root=context.mirror)
    assert list_refresh_sources(context.database)[0]["status"] == "completed"
    assert_baseline_preserved(context)


def test_library_scope_is_explicit_even_when_watched_completed_or_refresh_disabled(baseline, monkeypatch):
    from app.api import library_v4
    from app.media_v4.projection.source_libraries import list_source_cards

    context = baseline
    monkeypatch.setattr(library_v4, "get_database", lambda: context.database)
    monkeypatch.setattr(library_v4, "queue_library_artwork", lambda _: None)
    with context.database.connect() as conn:
        conn.execute("INSERT INTO tracking_states(work_id, provider, provider_id, metadata_json, updated_at) "
                     "VALUES (?, 'local', '', ?, 'now')", (context.first["work_id"], json.dumps({"status": "completed"})))
    work = next(work for work in library_v4.get_library(compact=True)["works"] if work["work_id"] == context.first["work_id"])
    assert work["content_scope"] == "ongoing" and work["watch_status"]["status"] == "completed"
    assert next(card for card in list_source_cards(context.database) if card["root_id"] == context.root_id)["content_scope"] == "ongoing"
    with context.database.connect() as conn:
        conn.execute("UPDATE source_roots SET enabled=0 WHERE root_id=?", (context.root_id,))
    assert refresh_all_sources(context.database) == []
    disabled = next(work for work in library_v4.get_library(compact=True)["works"] if work["work_id"] == context.first["work_id"])
    assert disabled.get("content_scope") == "ongoing", disabled
    with context.database.connect() as conn:
        conn.execute("UPDATE source_roots SET retired_at='now' WHERE root_id=?", (context.root_id,))
    assert context.first["work_id"] not in library_v4._content_scope_map([context.first["work_id"]])
    assert all(work.get("content_scope") != "ongoing" for work in library_v4.get_library(compact=True)["works"]
               if work["work_id"] == context.first["work_id"])


def test_multi_work_update_only_enqueues_changed_work_and_retains_unchanged_results(tmp_path, monkeypatch):
    from app.media_v4.persistence.metadata_lifecycle import collect_cleanup_candidates

    database, source, mirror, _config = environment(tmp_path, monkeypatch)
    for title in ("Show", "Second"):
        empty_video(source, f"{title}/{title}.S01E01.mkv")
    calls = []
    def provider(target):
        calls.append(target["preferred_title"])
        return {**external_provider(target), "provider_id": "321" if target["preferred_title"] == "Show" else "654"}
    first = media_v4._start_durable_local_scan(media_v4.SourceScanRequest(
        source="local", root_path=str(source), content_scope="ongoing", revision_id="multi-first",
    ))
    run_registered(database, first["scan_id"])
    service = V4RevisionService(database)
    service.confirm("multi-first")
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    initial = bindings(database, "multi-first")
    show, second = initial["Show/Show.S01E01.mkv"], initial["Second/Second.S01E01.mkv"]
    store = V4PlaybackStore(database)
    store.save_progress(second["work_id"], second["episode_id"], second["asset_id"], 123, 1500, False)
    with database.connect() as conn:
        snapshot_id = conn.execute("SELECT snapshot_id FROM revision_metadata_refs WHERE revision_id='multi-first' AND work_id=?", (second["work_id"],)).fetchone()[0]
        old_mirrors = {row["artifact_id"]: dict(row) for row in conn.execute(
            "SELECT * FROM artifacts WHERE revision_id='multi-first' AND work_id=? AND artifact_type='mirror'", (second["work_id"],))}
    assert len(old_mirrors) == 1
    immutable = immutable_rows(database)
    empty_video(source, "Show/Show.S01E02.mkv")
    queued = refresh_all_sources(database)[0]
    revision = run_registered(database, queued["task_id"])
    assert service.get_status(revision) == "confirmed"
    with database.connect() as conn:
        work_jobs = {(row["work_id"], row["job_type"]) for row in conn.execute(
            "SELECT work_id,job_type FROM jobs WHERE revision_id=? AND job_type IN ('materialize_mirror','scrape_work')", (revision,))}
    failures = []
    if work_jobs != {(show["work_id"], "materialize_mirror"), (show["work_id"], "scrape_work")}:
        failures.append("unchanged Second received materialize_mirror/scrape_work jobs")
    calls.clear()
    # The real cleanup job only collects candidates and reports removed_count=0.
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    if calls != ["Show"]:
        failures.append(f"provider calls were {calls!r}")
    current = bindings(database, revision)["Second/Second.S01E01.mkv"]
    assert {key: current[key] for key in ("work_id", "episode_id", "asset_id")} == {key: second[key] for key in ("work_id", "episode_id", "asset_id")}
    assert store.get_progress(second["episode_id"], second["asset_id"])["position"] == 123
    with database.connect() as conn:
        retained = conn.execute("SELECT snapshot_id FROM revision_metadata_refs WHERE revision_id=? AND work_id=?", (revision, second["work_id"])).fetchone()
        assert retained and retained[0] == snapshot_id
        refs = {row[0] for row in conn.execute("SELECT artifact_id FROM artifact_references WHERE revision_id=? AND work_id=? AND snapshot_id IS NULL AND role='mirror'", (revision, second["work_id"]))}
        if not set(old_mirrors) <= refs:
            failures.append("unchanged Second mirror references missing in current revision")
        for artifact_id, before in old_mirrors.items():
            assert dict(conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()) == before
        assert not (set(old_mirrors) & {row["artifact_id"] for row in collect_cleanup_candidates(conn, revision, mirror)})
        cleanup = conn.execute("SELECT result_json FROM jobs WHERE revision_id=? AND job_type='cleanup_superseded_artifacts'", (revision,)).fetchone()
        assert cleanup and json.loads(cleanup[0])["removed_count"] == 0
    detail = service.get_work_execution_detail(revision, second["work_id"])
    if detail["mirror"]["artifact_count"] != 1:
        failures.append("unchanged Second detail lost its one mirror artifact")
    progress = service.get_execution_progress(revision)
    unit = next(unit for unit in progress["work_units"] if unit["work_id"] == second["work_id"])
    if unit["overall_status"] != "completed" or progress["overall_status"] != "completed":
        failures.append(f"execution status Second={unit['overall_status']} revision={progress['overall_status']}")
    card = next(card for card in V4LibraryProjection(database).ensure_current().cards if card["work_id"] == second["work_id"])
    assert card["metadata"]["metadata_state"] == "ready" and card["metadata"]["episode_mapping_status"] == "complete"
    after = immutable_rows(database)
    assert all(after[table][key] == value for table, rows in immutable.items() for key, value in rows.items())
    assert failures == [], failures


def test_sync_scan_scope_conflict_is_http409_and_preserves_all_persistent_rows(baseline):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    context = baseline
    tables = ("source_roots", "source_scans", "source_scan_requests", "source_evidence", "parsed_facts", "import_revisions", "jobs")
    with context.database.connect() as conn:
        before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables}
    app = FastAPI()
    app.include_router(media_v4.router)
    response = TestClient(app, raise_server_exceptions=False).post("/api/v4/sources/scan", json={
        "source": "local", "root_path": str(context.source), "content_scope": "completed", "revision_id": "sync-conflict",
    })
    with context.database.connect() as conn:
        changed = [table for table in tables if before[table] != [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]]
    assert (response.status_code, changed) == (409, []), (response.status_code, changed)
    assert_baseline_preserved(context)


@pytest.mark.parametrize("auxiliary, add_episode", [("PV02.mkv", False), ("PV02.mkv", True), ("OP01.mkv", True)])
def test_auxiliary_files_never_enter_regular_numbering_or_block_new_episode(baseline, auxiliary, add_episode):
    context = baseline
    empty_video(context.source, "Show/" + auxiliary)
    if add_episode:
        empty_video(context.source, "Show/Show.S01E02.mkv")
    with context.database.connect() as conn:
        before_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    revision = refresh(context)
    status = V4RevisionService(context.database).get_status(revision)
    assert status == ("confirmed" if add_episode else "superseded"), (auxiliary, add_episode, status)
    if add_episode:
        current = bindings(context.database, revision)
        assert set(current) == {"Show/Show.S01E01.mkv", "Show/Show.S01E02.mkv"}
        V4JobRunner(context.database, metadata_provider=external_provider).process_available(mirror_root=context.mirror)
        card = next(card for card in V4LibraryProjection(context.database).ensure_current().cards if card["work_id"] == context.first["work_id"])
        assert card["metadata"]["metadata_state"] == "ready"
        assert len(card["metadata"]["episode_mappings"]) == 2
    else:
        with context.database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
    with context.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_evidence WHERE relative_path=?", ("Show/" + auxiliary,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM revision_bindings rb JOIN source_evidence se ON se.evidence_id=rb.evidence_id WHERE se.relative_path=?",
                            ("Show/" + auxiliary,)).fetchone()[0] == 0
    assert_baseline_preserved(context)


def test_unchanged_assessment_race_preserves_new_manual_override_as_preview(baseline, monkeypatch):
    from app.media_v4.tracking import ongoing

    context = baseline
    real_assess, real_finish = ongoing.assess_update, ongoing.finish_ongoing_update
    assessments, outcomes = [], []
    def edit_between_assessment_and_settle(database, revision_id, **kwargs):
        result = real_assess(database, revision_id, **kwargs)
        assessments.append(result)
        if len(assessments) == 1:
            assert result == "unchanged"
            service = V4RevisionService(database)
            evidence = service._load_revision_entries(revision_id)[0][0]
            service.apply_override(revision_id, evidence.evidence_id, {"episode_candidate": 7})
        return result
    def record_finish(database, revision_id):
        result = real_finish(database, revision_id)
        outcomes.append(result)
        return result
    monkeypatch.setattr(ongoing, "assess_update", edit_between_assessment_and_settle)
    monkeypatch.setattr(ongoing, "finish_ongoing_update", record_finish)
    with context.database.connect() as conn:
        before_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    revision = refresh(context)
    service = V4RevisionService(context.database)
    assert (outcomes, service.get_status(revision)) == (["review"], "draft")
    assert service._load_revision_entries(revision)[0][1].episode_candidate == 7
    with context.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
        assert conn.execute("SELECT status FROM import_revisions WHERE revision_id='first'").fetchone()[0] == "confirmed"
    assert list_refresh_sources(context.database)[0]["status"] == "needs_confirmation"
    assert_baseline_preserved(context)
