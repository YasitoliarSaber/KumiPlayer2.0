from __future__ import annotations

import json

import pytest

from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.jobs.scrape import V4ScrapeService
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


def test_season_refresh_preserves_other_season_mappings(tmp_path, monkeypatch):
    from app.core.config import AppConfig

    config = AppConfig(artwork_storage_mode='remote')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts.load_config', lambda: config)
    monkeypatch.setattr('app.media_v4.jobs.completeness.load_config', lambda: config)
    database = V4Database(tmp_path / 'library.db')
    database.initialize()
    entries = []
    for season in (1, 2):
        evidence = to_source_evidence(SourceEntry(
            root_id='root-scoped', scan_id='revision-scoped', provider='local',
            ingest_method='directory_tree',
            relative_path=f'Show/Show.S{season:02d}E01.mkv',
            playback_locator=f'Z:/fixture/Show.S{season:02d}E01.mkv',
            tmdb_hint_id='123', tmdb_hint_type='tv',
        ))
        entries.append((evidence, V4Parser().parse(evidence)))
    revisions = V4RevisionService(database)
    revisions.create_draft('revision-scoped', entries)
    revisions.confirm('revision-scoped')
    seen = []

    def provider(target):
        seasons = [item['local_season_number'] for item in target['episodes']]
        seen.append(seasons)
        return {
            'provider': 'tmdb', 'provider_id': '123', 'metadata_state': 'ready',
            'title': 'Online show',
            'poster_url': 'https://image.tmdb.org/t/p/original/poster.jpg',
            'fanart_url': 'https://image.tmdb.org/t/p/original/fanart.jpg',
            'episode_mappings': [
                {'episode_id': item['episode_id'], 'title': f'Pass {len(seen)} season {item["local_season_number"]}',
                 'provider_episode_id': str(1000 + item['local_season_number']),
                 'still_url': 'https://image.tmdb.org/t/p/w500/still.jpg',
                 'provider_season_number': item['local_season_number'], 'provider_episode_number': 1}
                for item in target['episodes']
            ],
        }

    mirror = tmp_path / 'mirror'
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM revision_bindings WHERE revision_id='revision-scoped' LIMIT 1").fetchone()[0]
        before = json.loads(conn.execute('SELECT metadata_json FROM scrape_bindings WHERE work_id=?', (work_id,)).fetchone()[0])
        old_by_id = {item['episode_id']: item for item in before['episode_mappings']}
        season_by_id = {row['episode_id']: row['local_season_number'] for row in conn.execute(
            'SELECT e.episode_id,s.local_season_number FROM episodes e JOIN seasons s ON s.season_id=e.season_id WHERE e.work_id=?',
            (work_id,),
        )}
    job = V4ScrapeService(database).requeue_work('revision-scoped', work_id, season_number=2)
    V4JobRunner(database, metadata_provider=provider).process_available(mirror_root=mirror)
    with database.connect() as conn:
        after = json.loads(conn.execute('SELECT metadata_json FROM scrape_bindings WHERE work_id=?', (work_id,)).fetchone()[0])
        result = json.loads(conn.execute('SELECT result_json FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()[0])
    by_id = {item['episode_id']: item for item in after['episode_mappings']}
    assert seen == [[1, 2], [2]]
    assert set(by_id) == set(old_by_id)
    assert all(by_id[key] == old_by_id[key] for key in by_id if season_by_id[key] == 1)
    assert all(by_id[key]['title'].startswith('Pass 2') for key in by_id if season_by_id[key] == 2)
    assert after['mapped_count'] == after['total_count'] == 2
    assert result['scope'] == 'season'
    with pytest.raises(ValueError, match='该季度'):
        V4ScrapeService(database).requeue_work('revision-scoped', work_id, season_number=3)
    monkeypatch.setattr('app.api.media_v4.get_database', lambda: database)
    from app.api.media_v4 import enqueue_work_scrape
    response = enqueue_work_scrape(work_id)
    assert response == {'work_id': work_id, 'job_id': job['job_id'], 'status': 'queued'}
