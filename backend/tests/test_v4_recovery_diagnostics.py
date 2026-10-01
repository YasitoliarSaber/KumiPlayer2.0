"""可见恢复摘要与图片来源保留；不透传内部结果或改变质量门控。"""

import json
from types import SimpleNamespace

from app.media_v4.jobs.metadata import finalize_metadata_statuses


def test_artwork_provenance_uses_episode_object_not_work_background():
    result = finalize_metadata_statuses({'work_type': 'movie'}, {
        'provider': 'tmdb', 'provider_id': '123', 'metadata_state': 'ready',
        'poster_url': 'https://image.tmdb.org/t/p/w500/poster.jpg',
        'fanart_url': 'https://image.tmdb.org/t/p/w1280/background.jpg',
        'episode_mappings': [{'episode_id': 'local-e', 'provider_episode_id': '456',
                              'still_url': 'https://image.tmdb.org/t/p/w500/still.jpg'}],
    })
    provenance = result['artwork_provenance']
    assert provenance['version'] == 1
    assert provenance['roles']['poster']['provider_object_id'] == '123'
    assert provenance['episodes']['local-e']['provider_object_id'] == '456'
    assert provenance['episodes']['local-e']['source_url'].endswith('/still.jpg')


def test_failed_download_keeps_remote_provenance_and_reports_failure(tmp_path, monkeypatch):
    from app.media_v4.jobs import metadata_artifacts
    result = finalize_metadata_statuses({'work_type': 'movie'}, {
        'provider': 'tmdb', 'provider_id': '123', 'metadata_state': 'ready',
        'poster_url': 'https://image.tmdb.org/t/p/w500/poster.jpg',
    })
    monkeypatch.setattr(metadata_artifacts, '_download_artwork', lambda *_a, **_kw: '')
    metadata_artifacts._materialize_local_artwork(config=SimpleNamespace(proxy_url='', tmdb_timeout=10), work_id='work',
        mirror_root=tmp_path, work_dir=tmp_path / 'work', target={'work_type': 'movie'},
        metadata=result, episode_metadata={}, artifacts=[])
    assert result['artwork_provenance']['roles']['poster']['status'] == 'download_failed'
    assert result['artwork_provenance']['roles']['poster']['source_url'].endswith('/poster.jpg')
    assert not result.get('local_poster_path')


def test_execution_detail_exposes_bounded_name_summary_without_raw_result(tmp_path, monkeypatch):
    from backend.tests.test_v4_work_execution_detail import _client, _seed_confirmed_work
    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)
    trace = [{'provider': 'tmdb', 'phase': 'retry', 'reason_code': 'no_candidates'},
             {'provider': 'anilist_names', 'status': 'completed', 'cache_status': 'hit',
              'evidence': [{'title': '可信名称', 'language': 'ja', 'source_url': 'https://anilist.co/anime/1',
                            'raw_prompt': 'fixture-private-prompt'}]},
             {'provider': 'bangumi_names', 'status': 'unavailable', 'reason_code': 'provider_timeout',
              'raw_response': 'fixture-private-response'}]
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET job_type='recover_work_aliases', result_json=? "
                     "WHERE revision_id='rev-detail' AND work_id=? AND job_type='scrape_work'",
                     (json.dumps({'alias_recovery_trace': trace, 'token': 'fixture-private-token'}), work_id))
    response = client.get(f'/api/v4/revisions/rev-detail/works/{work_id}/execution-detail')
    assert response.status_code == 200
    summary = response.json()['name_recovery']
    assert summary['steps'][1]['aliases'] == ['可信名称']
    assert summary['steps'][2]['reason_code'] == 'provider_timeout'
    assert 'fixture-private' not in response.text
