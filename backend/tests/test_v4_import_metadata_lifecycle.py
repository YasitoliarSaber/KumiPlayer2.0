"""CHECK-008: actual publication and rescan; only external provider is stubbed."""
from __future__ import annotations

import json

import pytest
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.persistence.database import V4Database
from app.media_v4.projection.library import V4LibraryProjection
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


def confirm(database, revision, numbers=(1,), *, provider_hint=False):
    entries = []
    for number in numbers:
        evidence = to_source_evidence(SourceEntry(
            root_id='root-life', scan_id=revision, provider='local',
            ingest_method='directory_tree', relative_path=f'Show/Show.S01E{number:02d}.mkv',
            playback_locator=f'Z:/fixture/Show.S01E{number:02d}.mkv',
            tmdb_hint_id='123' if provider_hint else '', tmdb_hint_type='tv' if provider_hint else '',
        ))
        entries.append((evidence, V4Parser().parse(evidence)))
    service = V4RevisionService(database)
    service.create_draft(revision, entries)
    service.confirm(revision)


def provider(target):
    return {
        'provider': 'tmdb', 'provider_id': '123', 'title': 'Online title',
        'metadata_state': 'ready', 'plot': 'fixture plot',
        'poster_url': 'https://image.tmdb.org/t/p/original/poster.jpg',
        'fanart_url': 'https://image.tmdb.org/t/p/original/fanart.jpg',
        'episode_mappings': [dict(e, title='Episode', provider_season_number=1,
                                  provider_episode_id=str(1000 + e['local_episode_number']),
                                  still_url='https://image.tmdb.org/t/p/original/episode.jpg',
                                  provider_episode_number=e['local_episode_number'])
                             for e in target['episodes']],
    }


def prepare(tmp_path, monkeypatch, *, provider_hint=False):
    from app.core.config import AppConfig
    config = AppConfig(artwork_storage_mode='remote')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts.load_config', lambda: config)
    monkeypatch.setattr('app.media_v4.jobs.completeness.load_config', lambda: config)
    database = V4Database(tmp_path / 'life.db')
    database.initialize()
    mirror = tmp_path / 'mirror'
    confirm(database, 'r1', provider_hint=provider_hint)
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    return database, mirror


def test_rescan_retains_success_and_unchanged_import_makes_no_request(tmp_path, monkeypatch):
    """CHECK-008A: superseded creation revision does not invalidate a success."""
    database, mirror = prepare(tmp_path, monkeypatch)
    assert V4LibraryProjection(database).current().cards[0]['title'] == 'Online title'
    files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in mirror.rglob('*') if p.is_file()}
    confirm(database, 'r2')
    card = V4LibraryProjection(database).ensure_current().cards[0]
    assert card['title'] == 'Online title'
    assert card['metadata']['metadata_source'] == 'retained'

    def no_request(_target):
        raise AssertionError('unchanged import must reuse successful snapshot')

    V4JobRunner(database, metadata_provider=no_request).process_available(mirror_root=mirror)
    assert all(p.exists() and (p.read_bytes(), p.stat().st_mtime_ns) == old for p, old in files.items())
    with database.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM metadata_snapshots').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM revision_metadata_refs').fetchone()[0] == 2
        outcome = json.loads(conn.execute("SELECT result_json FROM jobs WHERE revision_id='r2' AND job_type='scrape_work'").fetchone()[0])
        assert outcome['outcome'] == 'reused'


def test_historical_ready_with_zero_mappings_is_not_reused_or_counted_complete(tmp_path, monkeypatch):
    """历史伪成功快照不能再复用，实际补全后才重新就绪。"""
    from app.media_v4.persistence import metadata_lifecycle
    from app.media_v4.persistence.metadata_lifecycle import select_applicable_snapshot
    publish = metadata_lifecycle.publish_snapshot
    bad = {}

    def publish_historical(conn, **kwargs):
        bad.update(kwargs['metadata'])
        bad.update(episode_mappings=[], reason_code='episode_mapping_incomplete',
                   episode_mapping_status='unmapped', mapped_count=0, total_count=1)
        return publish(conn, **{**kwargs, 'metadata': bad})

    monkeypatch.setattr(metadata_lifecycle, 'publish_snapshot', publish_historical)
    database, mirror = prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(metadata_lifecycle, 'publish_snapshot', publish)
    with database.connect() as conn:
        snapshot = conn.execute('SELECT * FROM metadata_snapshots').fetchone()
        conn.execute('UPDATE scrape_bindings SET metadata_json=?', (json.dumps(bad),))
        # 模拟升级前已发布的投影缓存，不能等下一次导入才纠正假就绪。
        cached = dict(bad)
        cached.pop('episode_metadata_status', None)
        conn.execute('UPDATE library_cards SET metadata_json=?', (json.dumps(cached),))
        conn.execute("DELETE FROM v4_meta WHERE key='library_projection_dirty'")
    assert V4LibraryProjection(database).ensure_current().cards[0]['metadata']['metadata_state'] == 'failed'
    from app.api import library_v4
    monkeypatch.setattr(library_v4, 'get_database', lambda: database)
    assert library_v4.get_library()['works'] == []
    assert library_v4.get_library(include_all=True)['works'][0]['metadata_state'] == 'failed'
    service = V4RevisionService(database)
    progress = service.get_execution_progress('r1')
    assert progress['work_units'][0]['metadata_state'] == 'failed'
    assert progress['overall_status'] == 'needs_attention'
    from app.media_v4.projection.source_libraries import _metadata_ready_count
    assert _metadata_ready_count(progress) == 0
    detail = service.get_work_execution_detail('r1', snapshot['work_id'])
    assert detail['work']['metadata_reason_code'] == 'episode_mapping_incomplete'
    confirm(database, 'r2')
    with database.connect() as conn:
        assert select_applicable_snapshot(conn, 'r2', snapshot['work_id']) is None
    card = V4LibraryProjection(database).ensure_current().cards[0]
    assert card['metadata']['metadata_state'] != 'ready'
    calls = []

    def fixed_provider(target):
        calls.append(target['work_id'])
        return provider(target)

    V4JobRunner(database, metadata_provider=fixed_provider).process_available(mirror_root=mirror)
    assert calls == [snapshot['work_id']]
    assert V4LibraryProjection(database).ensure_current().cards[0]['metadata']['metadata_state'] == 'ready'


def test_append_failed_refresh_keeps_success_files_and_current_members(tmp_path, monkeypatch):
    """CHECK-008A/B: new episode + failed provider cannot erase old NFO."""
    database, mirror = prepare(tmp_path, monkeypatch)
    files = {p: p.read_bytes() for p in mirror.rglob('*') if p.is_file()}
    confirm(database, 'r2', (1, 2))
    V4JobRunner(database, metadata_provider=lambda target: {
        'provider': 'tmdb', 'provider_id': '123', 'metadata_state': 'source_unavailable',
        'reason_code': 'provider_unavailable', 'reason': 'fixture timeout', 'retryable': True,
    }).process_available(mirror_root=mirror)
    card = V4LibraryProjection(database).ensure_current().cards[0]
    assert card['episode_count'] == 2
    assert card['title'] == 'Online title'
    assert card['metadata']['refresh_status'] == 'failed'
    assert card['metadata']['episode_mapping_status'] == 'partial'
    assert all(p.exists() and p.read_bytes() == old for p, old in files.items())
    with database.connect() as conn:
        results = [json.loads(r[0]) for r in conn.execute("SELECT result_json FROM jobs WHERE job_type='cleanup_superseded_artifacts'")]
        assert results and all(r['outcome'] == 'deferred_cleanup' for r in results)


def test_artifact_preview_preserves_source_and_personal_state(tmp_path, monkeypatch):
    """CHECK-008B: cleanup operation is separate from source retirement."""
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview
    database, mirror = prepare(tmp_path, monkeypatch)
    confirm(database, 'r2')
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM revision_bindings WHERE revision_id='r2'").fetchone()[0]
    V4ScrapeService(database).requeue_work('r2', work_id)
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    preview = compute_delete_preview(database, provider='all', mirror_root=mirror,
                                     operation_kind='superseded_artifacts', revision_id='r2')
    assert preview['artifact_count'] > 0
    confirm_delete_preview(database, preview_id=preview['preview_id'], scope='all', digest=preview['digest'], mirror_root=mirror)
    with database.connect() as conn:
        assert conn.execute("SELECT status FROM import_revisions WHERE revision_id='r2'").fetchone()[0] == 'confirmed'
        assert conn.execute('SELECT retired_at FROM source_roots').fetchone()[0] == ''
    assert V4LibraryProjection(database).ensure_current().cards[0]['title'] == 'Online title'


def test_append_only_requests_missing_episode_and_composes_snapshot(tmp_path, monkeypatch):
    database, mirror = prepare(tmp_path, monkeypatch)
    confirm(database, 'r2', (1, 2))
    requested = []

    def partial(target):
        requested.extend(e['local_episode_number'] for e in target['episodes'])
        return provider(target)

    V4JobRunner(database, metadata_provider=partial).process_available(mirror_root=mirror)
    assert requested == [2]
    metadata = V4LibraryProjection(database).ensure_current().cards[0]['metadata']
    assert metadata['mapped_count'] == metadata['total_count'] == 2
    assert metadata['composed_from_snapshot_id']


def test_same_revision_refresh_keeps_snapshot_audit_artifact_members(tmp_path, monkeypatch):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.metadata_lifecycle import snapshot_artifacts
    database, mirror = prepare(tmp_path, monkeypatch)
    with database.connect() as conn:
        snapshot = conn.execute('SELECT snapshot_id,work_id FROM metadata_snapshots').fetchone()
        original = {r['artifact_id'] for r in snapshot_artifacts(conn, snapshot['snapshot_id'])}
    V4ScrapeService(database).requeue_work('r1', snapshot['work_id'])
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    with database.connect() as conn:
        assert {r['artifact_id'] for r in snapshot_artifacts(conn, snapshot['snapshot_id'])} == original


@pytest.mark.parametrize('change', ['reference', 'digest', 'source_root', 'symlink'])
def test_cleanup_preview_becomes_stale_before_confirmation(tmp_path, monkeypatch, change):
    from pathlib import Path

    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview
    from app.media_v4.persistence.metadata_lifecycle import attach_snapshot_refs, snapshot_artifacts
    database, mirror = prepare(tmp_path, monkeypatch)
    with database.connect() as conn:
        old = dict(conn.execute('SELECT * FROM metadata_snapshots').fetchone())
        old['artifacts'] = snapshot_artifacts(conn, old['snapshot_id'])
    confirm(database, 'r2')
    V4ScrapeService(database).requeue_work('r2', old['work_id'])
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    preview = compute_delete_preview(database, provider='all', mirror_root=mirror,
                                     operation_kind='superseded_artifacts', revision_id='r2')
    assert preview['artifact_count'] > 0
    protected = Path(old['artifacts'][0]['target_path'])
    if change == 'reference':
        with database.connect() as conn:
            attach_snapshot_refs(conn, 'r2', old['work_id'], old)
    elif change == 'digest':
        protected.write_bytes(b'changed after preview')
    elif change == 'source_root':
        with database.connect() as conn:
            conn.execute('UPDATE source_roots SET source_locator=?', (str(protected.parent),))
    else:
        outside = tmp_path / 'source-protected.nfo'
        outside.write_bytes(protected.read_bytes())
        protected.unlink()
        try:
            protected.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, 'winerror', None) != 1314:
                raise
            # Windows without symlink privilege: exercise an actual directory reparse point.
            import subprocess
            outside_directory = tmp_path / 'outside-generation'
            outside_directory.mkdir()
            (outside_directory / protected.name).write_bytes(outside.read_bytes())
            saved = protected.parent.with_name(protected.parent.name + '-saved')
            assert tmp_path.resolve() in protected.resolve().parents
            assert tmp_path.resolve() in saved.resolve().parents
            protected.parent.rename(saved)
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(protected.parent), str(outside_directory)], check=True, capture_output=True)
    with pytest.raises(ValueError):
        confirm_delete_preview(database, preview_id=preview['preview_id'], scope='all', digest=preview['digest'], mirror_root=mirror)
    assert protected.is_file()
    if change == 'symlink':
        assert outside.is_file() and mirror.resolve() not in protected.resolve().parents


def test_failed_generation_cannot_borrow_previous_generation_artifacts(tmp_path, monkeypatch):
    from app.media_v4.jobs.scrape import V4ScrapeService
    database, mirror = prepare(tmp_path, monkeypatch)
    with database.connect() as conn:
        before = dict(conn.execute('SELECT * FROM revision_metadata_refs').fetchone())
    V4ScrapeService(database).requeue_work('r1', before['work_id'])
    def fail(*args, **kwargs):
        raise OSError('fixture write failed')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts._artifact_digest', fail)
    with pytest.raises(OSError, match='fixture write failed'):
        V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    with database.connect() as conn:
        assert dict(conn.execute('SELECT * FROM revision_metadata_refs').fetchone()) == before
    card = V4LibraryProjection(database).ensure_current().cards[0]
    assert card['title'] == 'Online title'
    assert card['metadata']['refresh_status'] == 'failed'


def test_cleanup_resume_rechecks_source_root_protection(tmp_path, monkeypatch):
    """CHECK-008B: a failed deletion retry must honor newly protected source paths."""
    from pathlib import Path
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview, resume_operation
    database, mirror = prepare(tmp_path, monkeypatch)
    with database.connect() as conn:
        work_id = conn.execute('SELECT work_id FROM metadata_snapshots').fetchone()[0]
        protected = Path(conn.execute("SELECT target_path FROM artifacts WHERE artifact_type='nfo'").fetchone()[0])
    confirm(database, 'r2')
    V4ScrapeService(database).requeue_work('r2', work_id)
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    preview = compute_delete_preview(database, provider='all', mirror_root=mirror,
        operation_kind='superseded_artifacts', revision_id='r2')
    original_unlink = Path.unlink
    def fail_once(path, *args, **kwargs):
        if path == protected:
            raise PermissionError('fixture busy file')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_once)
        result = confirm_delete_preview(database, preview_id=preview['preview_id'], scope='all', digest=preview['digest'], mirror_root=mirror)
    assert result['status'] == 'partial_failed' and protected.is_file()
    with database.connect() as conn:
        conn.execute('UPDATE source_roots SET source_locator=?', (str(protected.parent),))
    resume_operation(database, preview_id=preview['preview_id'], mirror_root=mirror)
    assert protected.is_file()


def test_retained_work_skips_remote_work_detail_request(monkeypatch):
    from types import SimpleNamespace
    from app.media_v4.jobs.metadata import default_metadata_provider
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_tv_detail(self, *_args):
            raise AssertionError('retained Work must not be fetched again')
        def get_tv_season_episodes(self, *_args):
            return {'episodes': [{'episode_number': 2, 'name': 'Second', 'id': 2}]}
        def build_image_url(self, *_args):
            return ''
    monkeypatch.setattr('app.media_v4.jobs.metadata.TMDBClient', Client)
    monkeypatch.setattr('app.media_v4.jobs.metadata.load_config', lambda: SimpleNamespace(tmdb_bearer_token='test'))
    result = default_metadata_provider({'work_id': 'w', 'work_type': 'series',
        'provider_bindings': [{'provider': 'tmdb', 'media_type': 'tv', 'provider_id': '123'}],
        'retained_work_metadata': {'provider': 'tmdb', 'provider_id': '123', 'media_type': 'tv', 'title': 'Old title', 'metadata_state': 'ready'},
        'episodes': [{'episode_id': 'e2', 'season_id': 's1', 'episode_kind': 'regular', 'local_season_number': 1, 'local_episode_number': 2}]})
    assert result['title'] == 'Old title'
    assert [m['episode_id'] for m in result['episode_mappings']] == ['e2']


def test_changed_provider_does_not_reuse_previous_snapshot(tmp_path, monkeypatch):
    database, mirror = prepare(tmp_path, monkeypatch)
    with database.connect() as conn:
        conn.execute("UPDATE provider_bindings SET provider_id='999' WHERE provider='tmdb'")
    confirm(database, 'r2')
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM revision_metadata_refs WHERE revision_id='r2'").fetchone()[0] == 0
    assert list(mirror.rglob('work.nfo'))


def test_explicit_legacy_success_is_adopted_only_on_new_confirmation(tmp_path, monkeypatch):
    """C-008 legacy fixture uses real artifact publication, not a runner-success substitute."""
    from app.core.config import AppConfig
    from app.media_v4.jobs.metadata_artifacts import publish_metadata_artifacts
    config = AppConfig(artwork_storage_mode='remote')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts.load_config', lambda: config)
    monkeypatch.setattr('app.media_v4.jobs.completeness.load_config', lambda: config)
    database = V4Database(tmp_path / 'legacy.db')
    database.initialize()
    confirm(database, 'old')
    with database.connect() as conn:
        work = dict(conn.execute('SELECT * FROM works').fetchone())
        episodes = [dict(r) for r in conn.execute('SELECT e.*,s.local_season_number,s.season_kind FROM episodes e JOIN seasons s ON s.season_id=e.season_id')]
    target = {**work, 'episodes': episodes}
    metadata = {**provider(target), 'media_type': 'tv'}
    publish_metadata_artifacts(database, revision_id='old', work_id=work['work_id'], target=target,
                               metadata=metadata, mirror_root=tmp_path / 'mirror')
    with database.connect() as conn:
        conn.execute("INSERT INTO provider_bindings(work_id,provider,media_type,provider_id) VALUES (?,'tmdb','tv','123')", (work['work_id'],))
        conn.execute("INSERT INTO scrape_bindings(binding_id,revision_id,work_id,provider,provider_id,metadata_json,status,created_at,updated_at) VALUES ('legacy','old',?,'tmdb','123',?,'confirmed','now','now')",
                     (work['work_id'], json.dumps(metadata)))
        assert conn.execute('SELECT COUNT(*) FROM metadata_snapshots').fetchone()[0] == 0
    confirm(database, 'new')
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM revision_metadata_refs WHERE revision_id='new'").fetchone()[0] == 1
        assert json.loads(conn.execute('SELECT metadata_json FROM metadata_snapshots').fetchone()[0])['legacy_binding_id'] == 'legacy'


def test_shared_work_uses_current_members_and_successes_from_both_roots(tmp_path, monkeypatch):
    from dataclasses import replace
    from app.core.config import AppConfig
    from tests.test_v4_import_identity_roundtrip import _tree_entries
    config = AppConfig(artwork_storage_mode='remote')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts.load_config', lambda: config)
    monkeypatch.setattr('app.media_v4.jobs.completeness.load_config', lambda: config)
    database = V4Database(tmp_path / 'shared.db')
    database.initialize()
    mirror = tmp_path / 'mirror'
    service = V4RevisionService(database)
    def known_provider(target):
        return {**provider(target), 'provider_id': target['provider_bindings'][0]['provider_id']}
    for number, root in [(1, 'first'), (2, 'other')]:
        entries = _tree_entries(f'无职转生2/Season 1/无职转生2 S01E{number:02d}.mkv', root_id=root, scan_id=root)
        entries = [(replace(e, playback_locator=f'Z:/fixture/{root}/{e.relative_path}', source_locator=f'Z:/fixture/{root}/{e.relative_path}'), f) for e, f in entries]
        service.create_draft(root, entries)
        service.confirm(root)
        V4JobRunner(database, metadata_provider=known_provider).process_available(mirror_root=mirror)
    cards = V4LibraryProjection(database).ensure_current().cards
    assert len(cards) == 1
    assert cards[0]['episode_count'] == 2
    assert cards[0]['metadata']['mapped_count'] == cards[0]['metadata']['total_count'] == 2
    from app.media_v4.persistence.metadata_lifecycle import collect_cleanup_candidates
    with database.connect() as conn:
        assert collect_cleanup_candidates(conn, 'other', mirror) == []

    # CHECK-008C：两个活动根对同Work并发刷新，各自发布到自己的revision引用。
    import threading
    from app.media_v4.jobs.scrape import V4ScrapeService
    scraper = V4ScrapeService(database)
    jobs = [scraper.requeue_work(revision, cards[0]['work_id']) for revision in ('first', 'other')]
    prepared = threading.Barrier(3)
    releases = [threading.Event(), threading.Event()]
    errors = []
    def run(index):
        def delayed(target):
            result = {**known_provider(target), 'title': f'Refresh {index}'}
            prepared.wait(timeout=5)
            assert releases[index].wait(5)
            return result
        try:
            scraper.process(jobs[index]['job_id'], delayed, mirror_root=mirror)
        except Exception as exc:
            errors.append(exc)
    workers = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    try:
        prepared.wait(timeout=5)
        releases[1].set()
        workers[1].join(timeout=5)
    finally:
        for release in releases:
            release.set()
        for worker in workers:
            worker.join(timeout=5)
    assert not errors and not any(w.is_alive() for w in workers)
    with database.connect() as conn:
        for index, revision in enumerate(('first', 'other')):
            row = conn.execute('SELECT ms.* FROM revision_metadata_refs mr JOIN metadata_snapshots ms ON ms.snapshot_id=mr.snapshot_id WHERE mr.revision_id=?', (revision,)).fetchone()
            assert row['created_by_revision_id'] == revision
            assert json.loads(row['metadata_json'])['title'] == f'Refresh {index}'
