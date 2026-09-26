"""Real local scanner -> durable finalize -> confirm -> runner lifecycle, no success-row stubs."""
from __future__ import annotations

import hashlib
import json
import threading
import base64
from types import SimpleNamespace

import pytest
import httpx
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.persistence.database import V4Database
from app.media_v4.playback.session import V4PlaybackManager
from app.media_v4.playback.store import V4PlaybackStore
from app.media_v4.projection.library import V4LibraryProjection
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources import durable_scan
from app.media_v4.sources.scan_finalize import draft_finalizer
from app.media_v4.sources.scanner import _root_id, scan_local_directory


def scan(database, source, revision, monkeypatch, *, confirm_revision=True):
    root_id = _root_id(source)
    with database.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO source_roots(root_id,provider,ingest_method,source_locator,playback_locator,created_at,updated_at) VALUES (?,'local','local_scan',?,?,'now','now')",
                     (root_id, str(source), str(source)))
    threads = []
    def thread_factory(**kwargs):
        worker = threading.Thread(**kwargs)
        threads.append(worker)
        return worker
    monkeypatch.setattr(durable_scan, 'threading', SimpleNamespace(Thread=thread_factory, Event=threading.Event))
    def scanner(scan_id, **kwargs):
        _, sid, entries = scan_local_directory(source, scan_id=scan_id, **kwargs)
        return sid, entries
    durable_scan.create_durable_scan(database, scan_id=revision, root_id=root_id, kind='full',
        scan_fn=scanner, finalize_fn=draft_finalizer(database, revision_id=revision, root_id=root_id, scan_id=revision))
    threads[0].join(timeout=15)
    assert not threads[0].is_alive()
    state = durable_scan.get_durable_scan(database, revision)
    assert state['status'] == 'completed', state
    if confirm_revision:
        V4RevisionService(database).confirm(revision)


def external_provider(target):
    if target['work_type'] == 'unknown':
        return {'provider': 'local', 'metadata_state': 'waiting_review', 'reason_code': 'unknown_media_type'}
    return {'provider': 'tmdb', 'provider_id': '321', 'metadata_state': 'ready',
            'title': target['preferred_title'], 'poster_url': 'https://image.tmdb.org/t/p/original/p.jpg',
            'fanart_url': 'https://image.tmdb.org/t/p/original/f.jpg',
            'episode_mappings': [dict(e, title='Online episode', provider_season_number=e['local_season_number'],
                                     provider_episode_number=e['local_episode_number']) for e in target['episodes']]}


def setup(tmp_path, monkeypatch):
    from app.core.config import AppConfig
    config = AppConfig(artwork_storage_mode='local')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts.load_config', lambda: config)
    monkeypatch.setattr('app.media_v4.jobs.completeness.load_config', lambda: config)
    # Only the external HTTP boundary is replaced; download/write/digest/completeness stay real.
    image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=')
    def artwork_response(request):
        assert request.url.host == 'image.tmdb.org'
        return httpx.Response(200, content=image, headers={'content-type': 'image/png'})
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts._artwork_client',
                        lambda _config: httpx.Client(transport=httpx.MockTransport(artwork_response)))
    database = V4Database(tmp_path / 'lifecycle.db')
    database.initialize()
    source = tmp_path / 'source'
    source.mkdir()
    return database, source, tmp_path / 'mirror'


def write_video(source, relative, data=b'fixture-video'):
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def bindings(database, revision):
    with database.connect() as conn:
        return {r['relative_path']: dict(r) for r in conn.execute(
            'SELECT rb.*,se.relative_path FROM revision_bindings rb JOIN source_evidence se ON se.evidence_id=rb.evidence_id WHERE rb.revision_id=?', (revision,))}


def immutable_rows(database):
    with database.connect() as conn:
        return {table: {r[0]: hashlib.sha256(json.dumps(tuple(r), ensure_ascii=False).encode()).hexdigest()
                        for r in conn.execute(f'SELECT * FROM {table}')}
                for table in ('source_evidence', 'parsed_facts', 'revision_bindings')}


def test_scan_runner_rescan_append_failure_and_replacement(tmp_path, monkeypatch):
    """CHECK-010A: actual scanner, publisher, local images, progress and rescan."""
    database, source, mirror = setup(tmp_path, monkeypatch)
    names = ['Show/Show.S01E01.mkv', 'Show/Show.S01E02.1080p.mkv', 'Show/Show.S01E02.2160p.mkv',
             'Show/Specials/Show.S01E03.mkv', 'Happy Ending/Happy Ending.S01E01.mkv',
             'Unknown/未知甲.mkv', 'Unknown/未知乙.mkv', 'Show/Show.S02E13.mkv', 'Show/Show.S02E14.mkv',
             'Independent [OVA]/Independent.01.mkv']
    for name in names:
        write_video(source, name)
    scan(database, source, 'r1', monkeypatch)
    runner = V4JobRunner(database, metadata_provider=external_provider)
    runner.process_available(mirror_root=mirror)
    first = bindings(database, 'r1')
    assert 'Show/Specials/Show.S01E03.mkv' not in first
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_evidence WHERE relative_path='Show/Specials/Show.S01E03.mkv'").fetchone()[0] == 1
        assert {r[0] for r in conn.execute("SELECT artifact_type FROM artifacts WHERE status='published'")} >= {'poster', 'fanart', 'nfo'}
    main = first[names[0]]
    store = V4PlaybackStore(database)
    store.save_progress(main['work_id'], main['episode_id'], main['asset_id'], 123, 1500, False)
    immutable = immutable_rows(database)
    files = {p: p.read_bytes() for p in mirror.rglob('*') if p.is_file()}
    scan(database, source, 'r2', monkeypatch)
    requested = []
    def counting(target):
        requested.append(target['work_type'])
        return external_provider(target)
    V4JobRunner(database, metadata_provider=counting).process_available(mirror_root=mirror)
    second = bindings(database, 'r2')
    for name in names:
        if '/Specials/' in name:
            continue
        assert first[name]['asset_id'] == second[name]['asset_id']
        assert first[name]['episode_id'] == second[name]['episode_id']
        assert first[name]['evidence_id'] != second[name]['evidence_id']
    assert set(requested) <= {'unknown'}
    manager = V4PlaybackManager(database, session_dir=tmp_path / 'sessions')
    resolved = manager._resolve_asset(main['work_id'], main['episode_id'], main['asset_id'])
    assert resolved['playback_locator'] == str(source / names[0])
    assert store.get_progress(main['episode_id'], main['asset_id'])['position'] == 123
    write_video(source, 'Show/Show.S01E04.mkv')
    scan(database, source, 'r3', monkeypatch)
    V4JobRunner(database, metadata_provider=lambda target: {
        'provider': 'tmdb', 'provider_id': '321', 'metadata_state': 'source_unavailable', 'reason_code': 'provider_unavailable',
    }).process_available(mirror_root=mirror)
    card = next(c for c in V4LibraryProjection(database).ensure_current().cards if c['work_id'] == main['work_id'])
    assert card['metadata']['metadata_source'] == 'retained'
    assert card['metadata']['refresh_status'] == 'failed'
    assert card['metadata']['episode_mapping_status'] == 'partial'
    assert all(p.is_file() and p.read_bytes() == data for p, data in files.items())
    write_video(source, names[0], b'replacement-longer-content')
    scan(database, source, 'r4', monkeypatch)
    replaced = bindings(database, 'r4')[names[0]]
    assert replaced['asset_id'] != main['asset_id']
    with pytest.raises(KeyError):
        store.get_progress(replaced['episode_id'], replaced['asset_id'])
    assert store.get_progress(main['episode_id'], main['asset_id'])['position'] == 123
    after = immutable_rows(database)
    for table, rows in immutable.items():
        assert all(after[table][key] == value for key, value in rows.items())


def test_late_provider_cannot_publish_after_new_confirmation(tmp_path, monkeypatch):
    database, source, mirror = setup(tmp_path, monkeypatch)
    write_video(source, 'Show/Show.S01E01.mkv')
    scan(database, source, 'r1', monkeypatch)
    ready, release = threading.Event(), threading.Event()
    errors = []
    def delayed(target):
        result = external_provider(target)
        ready.set()
        assert release.wait(10)
        return result
    def run():
        try:
            V4JobRunner(database, metadata_provider=delayed).process_available(mirror_root=mirror)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert ready.wait(10)
        scan(database, source, 'r2', monkeypatch)
    finally:
        release.set()
        worker.join(timeout=15)
    assert not worker.is_alive() and not errors
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metadata_snapshots WHERE created_by_revision_id='r1'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM revision_metadata_refs WHERE revision_id='r1'").fetchone()[0] == 0


def test_upgraded_v21_accepts_new_scan_without_changing_old_facts(tmp_path, monkeypatch):
    from tests.test_v4_import_contract_schema import old_database, snapshot
    database = old_database(tmp_path)
    with database.connect() as conn:
        columns, old = snapshot(conn)
    database.initialize()
    source = tmp_path / 'new-source'
    source.mkdir()
    write_video(source, 'New/New.S01E01.mkv')
    scan(database, source, 'after-upgrade', monkeypatch)
    with database.connect() as conn:
        for table in ('source_evidence', 'parsed_facts', 'revision_bindings', 'work_overrides', 'playback_progress'):
            rows = [tuple(r) for r in conn.execute(f'SELECT {",".join(columns[table])} FROM {table}')]
            assert all(row in rows for row in old[table]), table
        assert conn.execute("SELECT status FROM import_revisions WHERE revision_id='after-upgrade'").fetchone()[0] == 'confirmed'


def test_duplicate_confirmation_is_idempotent_under_barrier(tmp_path, monkeypatch):
    database, source, _mirror = setup(tmp_path, monkeypatch)
    write_video(source, 'Show/Show.S01E01.mkv')
    scan(database, source, 'r1', monkeypatch, confirm_revision=False)
    gate = threading.Barrier(3)
    errors = []
    def confirm_again():
        gate.wait(timeout=5)
        try:
            V4RevisionService(database).confirm('r1')
        except Exception as exc:
            errors.append(exc)
    workers = [threading.Thread(target=confirm_again) for _ in range(2)]
    for worker in workers:
        worker.start()
    gate.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=5)
    assert not errors and not any(w.is_alive() for w in workers)
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_revisions WHERE status='confirmed'").fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(DISTINCT idempotency_key) FROM jobs').fetchone()[0] == 3


@pytest.mark.parametrize('cancelled', [False, True])
def test_partial_scan_does_not_replace_confirmed_baseline(tmp_path, monkeypatch, cancelled):
    from app.media_v4.sources.scanner import SourceScanCancelled
    database, source, _mirror = setup(tmp_path, monkeypatch)
    write_video(source, 'Show/Show.S01E01.mkv')
    scan(database, source, 'r1', monkeypatch)
    before = immutable_rows(database)
    root_id = _root_id(source)
    workers = []
    def factory(**kwargs):
        worker = threading.Thread(**kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(durable_scan, 'threading', SimpleNamespace(Thread=factory, Event=threading.Event))
    def partial(scan_id, on_evidence_batch, **kwargs):
        _, _, entries = scan_local_directory(source, scan_id=scan_id)
        on_evidence_batch(entries[:1])
        if cancelled:
            raise SourceScanCancelled()
        raise OSError('fixture second page failed')
    durable_scan.create_durable_scan(database, root_id=root_id, scan_id='partial', kind='full', scan_fn=partial,
        finalize_fn=draft_finalizer(database, revision_id='partial', root_id=root_id, scan_id='partial'))
    workers[0].join(timeout=10)
    assert not workers[0].is_alive()
    assert durable_scan.get_durable_scan(database, 'partial')['status'] == ('cancelled' if cancelled else 'failed')
    with database.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT revision_id FROM import_revisions WHERE status='confirmed'")] == ['r1']
        assert conn.execute("SELECT COUNT(*) FROM revision_bindings WHERE revision_id='partial'").fetchone()[0] == 0
    after = immutable_rows(database)
    assert all(after['revision_bindings'][key] == value for key, value in before['revision_bindings'].items())
