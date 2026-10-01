"""正片分集缺项不能发布为完整刮削；所有数据均为离线夹具。"""
from app.media_v4.jobs.metadata import finalize_metadata_statuses


def test_legacy_confirmed_binding_does_not_bypass_regular_gate():
    from app.media_v4.jobs.metadata_quality import require_regular_episode_metadata
    result = require_regular_episode_metadata([{'episode_id': 'e1'}], {'metadata_state': 'confirmed'})
    assert result['metadata_state'] == 'failed'
    assert result['reason_code'] == 'episode_mapping_incomplete'


def test_zero_of_24_regular_episodes_is_not_ready():
    target = {'work_type': 'series', 'episodes': [
        {'episode_id': f'e{i}', 'episode_kind': 'regular', 'season_kind': 'regular'}
        for i in range(24)
    ]}
    result = finalize_metadata_statuses(target, {
        'provider': 'tmdb', 'provider_id': '42', 'metadata_state': 'ready',
        'title': 'Show', 'episode_mappings': [],
    })
    assert result['metadata_state'] == 'failed'
    assert result['work_metadata_status'] == 'ready'
    assert result['identity_status'] == 'confirmed'
    assert result['reason_code'] == 'episode_mapping_incomplete'
    assert result['mapped_count'] == 0
    assert result['total_count'] == 24
    assert '服务不可用' not in result['reason']


def test_missing_optional_special_does_not_hide_complete_regular_metadata():
    target = {'work_type': 'series', 'episodes': [
        {'episode_id': 'e1', 'episode_kind': 'regular'},
        {'episode_id': 'sp', 'episode_kind': 'special'},
    ]}
    result = finalize_metadata_statuses(target, {
        'provider': 'tmdb', 'provider_id': '42', 'metadata_state': 'ready',
        'reason_code': 'special_episode_metadata_incomplete',
        'episode_mappings': [{'episode_id': 'e1', 'provider_episode_id': '11', 'title': 'Episode',
                              'still_url': 'https://image.tmdb.org/t/p/w500/still.jpg'}],
    })
    assert result['metadata_state'] == 'ready'


def test_mapped_regular_episode_without_title_and_still_is_not_complete():
    result = finalize_metadata_statuses({'episodes': [{'episode_id': 'e1', 'episode_kind': 'regular'}]}, {
        'provider': 'tmdb', 'provider_id': '42', 'metadata_state': 'ready',
        'episode_mappings': [{'episode_id': 'e1', 'provider_episode_id': '11', 'title': '', 'still_url': ''}],
    })
    assert result['episode_mapping_status'] == 'complete'
    assert result['metadata_state'] == 'failed'
    assert result['reason_code'] == 'episode_details_incomplete'
    assert result['missing_episode_title_count'] == result['missing_episode_still_count'] == 1
