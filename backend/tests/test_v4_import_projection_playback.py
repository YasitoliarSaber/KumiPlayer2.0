"""CHECK-009: unknown values survive detail/projection/playback consumption."""
import pytest
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


def test_unknown_detail_and_playback_title_do_not_claim_movie_or_special(tmp_path, monkeypatch):
    from app.api import library_v4
    from app.media_v4.playback.session import V4PlaybackManager
    database = V4Database(tmp_path / 'unknown.db')
    database.initialize()
    entries = []
    for stem in ('未知内容甲', '未知内容乙'):
        evidence = to_source_evidence(SourceEntry(root_id='u', scan_id='u1', provider='local',
            ingest_method='directory_tree', relative_path=f'Show/{stem}.mkv', playback_locator=f'Z:/fixture/{stem}.mkv'))
        entries.append((evidence, V4Parser().parse(evidence)))
    service = V4RevisionService(database)
    service.create_draft('u1', entries)
    service.confirm('u1')
    with database.connect() as conn:
        binding = dict(conn.execute('SELECT * FROM revision_bindings LIMIT 1').fetchone())
    monkeypatch.setattr(library_v4, 'get_database', lambda: database)
    detail = library_v4.get_work_detail(binding['work_id'])
    assert detail['media_type'] == 'unknown'
    assert all(s['season_number'] is None and s['label'] == '未分季' for s in detail['seasons'])
    assert len(detail['episodes']) == 2
    manager = V4PlaybackManager(database)
    asset = manager._resolve_asset(binding['work_id'], binding['episode_id'], binding['asset_id'])
    title = manager._display_title(asset)
    assert 'S00' not in title and '特别篇' not in title


def test_started_session_final_checkpoint_does_not_open_public_history_writes(tmp_path):
    import pytest
    from app.media_v4.playback.store import V4PlaybackStore
    database = V4Database(tmp_path / 'checkpoint.db')
    database.initialize()
    evidence = to_source_evidence(SourceEntry(root_id='session', scan_id='s1', provider='local',
        ingest_method='directory_tree', relative_path='Show/Show.S01E01.mkv', playback_locator='Z:/fixture/a.mkv'))
    service = V4RevisionService(database)
    service.create_draft('s1', [(evidence, V4Parser().parse(evidence))])
    service.confirm('s1')
    with database.connect() as conn:
        binding = dict(conn.execute('SELECT * FROM revision_bindings').fetchone())
    store = V4PlaybackStore(database)
    token = store.capture_session_binding(binding['work_id'], binding['episode_id'], binding['asset_id'])
    service.create_draft('s2', [], root_id='session', scan_id='s2')
    service.confirm('s2')
    with pytest.raises(KeyError):
        store.save_progress(binding['work_id'], binding['episode_id'], binding['asset_id'], 124, 1000, False)
    assert store.save_session_progress(token, 123, 1000, False) is False
    assert store.get_progress(binding['episode_id'], binding['asset_id'])['position'] == 123


def test_asset_generation_keys_are_independent_uuids(tmp_path):
    import uuid
    database = V4Database(tmp_path / 'generation.db')
    database.initialize()
    entries = []
    for number in (1, 2):
        evidence = to_source_evidence(SourceEntry(root_id='g', scan_id='g1', provider='local',
            ingest_method='directory_tree', relative_path=f'Show/Show.S01E{number:02d}.mkv', size=100,
            playback_locator=f'Z:/fixture/{number}.mkv'))
        entries.append((evidence, V4Parser().parse(evidence)))
    service = V4RevisionService(database)
    service.create_draft('g1', entries)
    service.confirm('g1')
    with database.connect() as conn:
        keys = [r[0] for r in conn.execute('SELECT content_version_key FROM assets')]
    assert len(keys) == len(set(keys)) == 2
    assert all(str(uuid.UUID(key)) == key for key in keys)


@pytest.mark.parametrize(('name', 'kind'), [
    ('Show/Show.S01E01.1080p.mkv', 'series'),
    ('Movie/Show 剧场版 [Movie].mkv', 'movie'),
    ('Unknown/未知内容.mkv', 'unknown'),
])
def test_all_media_kinds_keep_progress_without_replaying_history(tmp_path, monkeypatch, name, kind):
    """CHECK-009A: real scan/confirm and manager resolution, per-Asset progress."""
    from tests.test_v4_media_import_lifecycle_contract import setup, scan, write_video, bindings
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.playback.store import V4PlaybackStore
    database, source, _mirror = setup(tmp_path, monkeypatch)
    write_video(source, name)
    if kind == 'series':
        write_video(source, name.replace('1080p', '2160p'))
    scan(database, source, 'r1', monkeypatch)
    before = bindings(database, 'r1')[name]
    key = before['episode_id'] or f"movie:{before['work_id']}"
    store = V4PlaybackStore(database)
    store.save_progress(before['work_id'], key, before['asset_id'], 123, 1500, False)
    with database.connect() as conn:
        assert conn.execute('SELECT work_type FROM works WHERE work_id=?', (before['work_id'],)).fetchone()[0] == kind
        history_count = conn.execute('SELECT COUNT(*) FROM playback_history').fetchone()[0]
    scan(database, source, 'r2', monkeypatch)
    current = bindings(database, 'r2')[name]
    assert all(current[field] == before[field] for field in ('work_id', 'episode_id', 'asset_id'))
    manager = V4PlaybackManager(database, session_dir=tmp_path / 'sessions')
    assert manager._resolve_asset(current['work_id'], key, current['asset_id'])['playback_locator'] == str(source / name)
    assert store.get_progress(key, current['asset_id'])['position'] == 123
    if kind == 'series':
        other = bindings(database, 'r2')[name.replace('1080p', '2160p')]
        assert other['episode_id'] == key and other['asset_id'] != current['asset_id']
        with pytest.raises(KeyError):
            store.get_progress(key, other['asset_id'])
    with database.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM playback_history').fetchone()[0] == history_count
    write_video(source, name, b'replaced-longer-content')
    scan(database, source, 'r3', monkeypatch)
    replacement = bindings(database, 'r3')[name]
    assert replacement['asset_id'] != current['asset_id']
    manager._resolve_asset(replacement['work_id'], key, replacement['asset_id'])
    with pytest.raises(KeyError):
        store.get_progress(key, replacement['asset_id'])
    assert store.get_progress(key, current['asset_id'])['position'] == 123
