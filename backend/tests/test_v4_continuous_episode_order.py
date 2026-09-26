"""本地分季与线上连续编集的区别不能变成资料获取失败。

编号映射合同（C-004 / CHECK-007A）：本地分季与线上连续编号之间只有带版本的
已核验 local→Provider 偏移规则、明确 absolute 来源或已有逐条映射才允许映射；
前季文件数、季内最大集号、缺集与播出间隔都只是诊断，不构成偏移依据。证据不足
时返回原编号副本并记录唯一原因，不猜 S1E1、不补 0、不抛错，作品资料仍为 ready。
"""
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from app.media_v4.jobs import metadata
from app.scrape.tmdb_client import TMDBClientError

#: 只用于"编号未被改写"比较的记账字段；它们是本轮新增的映射来源说明。
_BOOKKEEPING_FIELDS = ("mapping_basis", "mapping_evidence", "unmapped_reason", "mapping_conflict")


def _numbers(row: dict) -> dict:
    """去掉映射记账字段后的副本，用于断言本地/已有编号没有被改写。"""

    return {key: value for key, value in row.items() if key not in _BOOKKEEPING_FIELDS}


@pytest.mark.parametrize('counts', [(12, 12), (25, 25, 16)])
def test_local_seasons_map_to_continuous_online_episodes(monkeypatch, counts):
    local, remote = [], []
    for season, count in enumerate(counts, 1):
        for number in range(1, count + 1):
            local.append({'episode_id': f'{season}-{number}', 'season_id': str(season),
                          'local_season_number': season, 'local_episode_number': number})
            remote.append({'episode_number': len(remote) + 1, 'id': len(remote) + 100,
                           'name': f'S{season}E{number}',
                           'air_date': str(date(2016 + season * 3, 1, 1) + timedelta(days=number * 7))})
    requested = []

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get_tv_detail(self, _id):
            return {'name': 'Show', 'seasons': [{'season_number': 1, 'episode_count': len(remote)}]}
        def get_tv_season_episodes(self, _id, season):
            requested.append(season)
            if season != 1:
                raise TMDBClientError('missing', status_code=404, reason_code='provider_resource_missing', retryable=False)
            return {'episodes': remote}
        def select_best_poster(self, _images): return ''
        def select_best_backdrop(self, _images): return ''
        def select_best_logo(self, _images): return ''

    monkeypatch.setattr(metadata, 'TMDBClient', Client)
    monkeypatch.setattr(metadata, 'load_config', lambda: SimpleNamespace(tmdb_bearer_token='fixture'))
    target = {'work_type': 'series', 'preferred_title': 'Show', 'episodes': local,
              'provider_bindings': [{'provider': 'tmdb', 'provider_id': '42', 'media_type': 'tv'}]}
    original = deepcopy(target)

    # C-004：本地分季 + 线上单季连续编号本身不是偏移依据；不猜、不崩、也不把
    # 整部作品降级成资料失败。
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'ready'
    assert result['episode_mappings'] == []
    assert result['episode_mapping_status'] == 'unmapped'
    assert result['unmapped_reason_codes'] == ['insufficient_numbering_evidence']
    assert requested == [1]
    assert target == original

    # 只有带版本的已核验 local→Provider 偏移规则才允许连续映射，且不改本地编号。
    offsets = {1: {'offset': 0, 'rule_version': 'fixture-v1'}}
    cumulative = counts[0]
    for season, count in enumerate(counts, 1):
        if season > 1:
            offsets[season] = {'offset': cumulative, 'rule_version': 'fixture-v1'}
            cumulative += count
    with_offsets = deepcopy(target)
    with_offsets['verified_season_offsets'] = offsets
    mapped = metadata.default_metadata_provider(with_offsets)
    assert mapped['metadata_state'] == 'ready'
    assert mapped['episode_mapping_status'] == 'complete'
    assert len(mapped['episode_mappings']) == sum(counts)
    second = next(m for m in mapped['episode_mappings'] if m['episode_id'] == '2-1')
    assert (second['provider_season_number'], second['provider_episode_number']) == (1, counts[0] + 1)
    local_rows = {item['episode_id']: item for item in with_offsets['episodes']}
    assert local_rows['2-1']['local_season_number'] == 2
    assert local_rows['2-1']['local_episode_number'] == 1
    assert 'provider_episode_number' not in local_rows['2-1']


@pytest.mark.parametrize('provider_season', [None, 1, 2])
def test_missing_files_do_not_shrink_offset_and_explicit_mapping_is_preserved(provider_season):
    episodes = [
        {'episode_id': 'first-tail', 'local_season_number': 1, 'local_episode_number': 12},
        {'episode_id': 'second', 'local_season_number': 2, 'local_episode_number': 3,
         'provider_season_number': provider_season},
        {'episode_id': 'explicit', 'local_season_number': 2, 'local_episode_number': 4,
         'provider_season_number': 1, 'provider_episode_number': 20},
        {'episode_id': 'special', 'local_season_number': 0, 'special_number': 1, 'episode_kind': 'special'},
    ]
    remote = {'episodes': [
        {'episode_number': 12, 'id': 12, 'air_date': '2022-03-27'},
        {'episode_number': 13, 'id': 13, 'air_date': '2025-07-06'},
        {'episode_number': 15, 'id': 15},
    ]}
    mapped = metadata._map_continuous_season(episodes, remote)
    # 前季只剩 E12：文件数与季内最大集号都不能推断偏移，如实记录缺编号依据。
    assert mapped[1]['unmapped_reason'] == 'insufficient_numbering_evidence'
    assert _numbers(mapped[1]) == episodes[1]
    assert mapped[1].get('provider_episode_number') in (None, episodes[1].get('provider_episode_number'))
    # 已有逐条 Provider 映射不被改写或丢弃。
    assert mapped[2]['provider_episode_number'] == 20
    assert _numbers(mapped[2]) == episodes[2]
    # 明确特别篇原样返回，由标题匹配负责。
    assert mapped[3] == episodes[3]
    # 带版本的已核验偏移规则（前季完整 12 集）才允许 S2E3 → 线上 15。
    offset_mapped = metadata._map_continuous_season(
        episodes, remote, verified_offsets={2: {'offset': 12, 'rule_version': 'fixture-v1'}}
    )
    assert offset_mapped[1]['provider_episode_number'] == 15
    assert offset_mapped[1]['mapping_basis'] == 'verified_season_offset'
    assert offset_mapped[1]['mapping_evidence']['rule_version'] == 'fixture-v1'


def test_incomplete_previous_season_does_not_guess_wrong_offset():
    episodes = [
        {'episode_id': 'first', 'local_season_number': 1, 'local_episode_number': 10},
        {'episode_id': 'second', 'local_season_number': 2, 'local_episode_number': 1},
    ]
    remote = {'episodes': [
        {'episode_number': 10, 'id': 10, 'air_date': '2022-03-13'},
        {'episode_number': 11, 'id': 11, 'air_date': '2022-03-20'},
    ]}
    mapped = metadata._map_continuous_season(episodes, remote)
    assert all(row['unmapped_reason'] == 'insufficient_numbering_evidence' for row in mapped)
    assert [_numbers(row) for row in mapped] == episodes
    assert all('provider_episode_number' not in row for row in mapped)


def test_absolute_numbers_are_not_offset_twice():
    episodes = [
        {'episode_id': 'first', 'local_season_number': 1, 'local_episode_number': 12},
        {'episode_id': 'second', 'local_season_number': 2, 'local_episode_number': 13,
         'absolute_episode_number': 13},
    ]
    remote = {'episodes': [
        {'episode_number': 12, 'id': 12, 'air_date': '2022-03-27'},
        {'episode_number': 13, 'id': 13, 'air_date': '2025-07-06'},
        {'episode_number': 25, 'id': 25},
    ]}
    mapped = metadata._map_continuous_season(episodes, remote)
    assert mapped[1]['provider_episode_number'] == 13
    assert mapped[1]['mapping_basis'] == 'explicit_absolute'
    # 没有绝对编号证据时，13 也可能是缺少前半部分的本季第 13 集，不能猜成 25。
    del episodes[1]['absolute_episode_number']
    guessed = metadata._map_continuous_season(episodes, remote)
    assert 'provider_episode_number' not in guessed[1]
    assert guessed[1]['unmapped_reason'] == 'insufficient_numbering_evidence'
    assert [_numbers(row) for row in guessed] == episodes
