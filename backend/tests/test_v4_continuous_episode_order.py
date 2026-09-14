"""本地分季与线上连续编集的区别不能变成资料获取失败。"""
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.media_v4.jobs import metadata
from app.scrape.tmdb_client import TMDBClientError


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
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'ready'
    assert len(result['episode_mappings']) == sum(counts)
    assert requested == [1]
    assert target == original
    second = next(m for m in result['episode_mappings'] if m['episode_id'] == '2-1')
    assert (second['provider_season_number'], second['provider_episode_number']) == (1, counts[0] + 1)


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
    assert mapped[1]['provider_episode_number'] == 15
    assert mapped[2] == episodes[2]
    assert mapped[3] == episodes[3]


def test_incomplete_previous_season_does_not_guess_wrong_offset():
    episodes = [
        {'episode_id': 'first', 'local_season_number': 1, 'local_episode_number': 10},
        {'episode_id': 'second', 'local_season_number': 2, 'local_episode_number': 1},
    ]
    remote = {'episodes': [
        {'episode_number': 10, 'id': 10, 'air_date': '2022-03-13'},
        {'episode_number': 11, 'id': 11, 'air_date': '2022-03-20'},
    ]}
    assert metadata._map_continuous_season(episodes, remote) == episodes


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
    assert metadata._map_continuous_season(episodes, remote)[1]['provider_episode_number'] == 13
    # 没有绝对编号证据时，13 也可能是缺少前半部分的本季第 13 集，不能猜成 25。
    del episodes[1]['absolute_episode_number']
    assert metadata._map_continuous_season(episodes, remote) == episodes
