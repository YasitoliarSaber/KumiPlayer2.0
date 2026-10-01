"""恢复缓存仅保存受限名称证据，不替代媒体事实或身份证据。"""

import pytest

from app.media_v4.jobs.alias_cache import RecoveryCache
from app.media_v4.jobs.alias_recovery import recover_metadata
from app.media_v4.persistence.database import V4Database
from app.scrape.alias_contract import AliasEvidence


@pytest.fixture
def cache(tmp_path):
    database = V4Database(tmp_path / 'cache.db')
    database.initialize()
    clock = [1000.0]
    return database, clock, RecoveryCache(database, now=lambda: clock[0])


def evidence():
    return [AliasEvidence('独立电影 副标题', 'ja', 'anilist', '1', 'https://anilist.co/anime/1', 'fixture')]


def test_cache_survives_restart_and_expires_positive_and_empty_separately(cache):
    database, clock, store = cache
    target = {'preferred_title': '本地名称', 'year': 2020, 'work_type': 'movie'}
    store.put(target, 'anilist', evidence())
    other = RecoveryCache(database, now=lambda: clock[0])
    assert other.get(target, 'anilist')['evidence'][0]['title'] == '独立电影 副标题'
    empty = {**target, 'year': 2021}
    store.put(empty, 'anilist', [])
    clock[0] += 3601
    assert other.get(empty, 'anilist') is None
    assert other.get(target, 'anilist') is not None
    clock[0] += 7 * 86400
    assert other.get(target, 'anilist') is None


def test_key_distinguishes_year_type_season_provider_and_confirmed_titles(cache):
    _, _, store = cache
    target = {'preferred_title': '同名', 'year': 2020, 'work_type': 'series',
              'identity_titles': ['原名'], 'episodes': [{'local_season_number': 1}]}
    store.put(target, 'anilist', evidence())
    for altered in ({**target, 'year': 2021}, {**target, 'work_type': 'movie'},
                    {**target, 'identity_titles': ['另一原名']},
                    {**target, 'episodes': [{'local_season_number': 2}]}):
        assert store.get(altered, 'anilist') is None
    assert store.get(target, 'bangumi') is None


def test_cache_hit_only_reuses_search_evidence_and_does_not_cache_tmdb_decision(cache):
    _, _, store = cache
    calls, targets = [], []
    def names(_target):
        calls.append(1)
        return evidence()
    def tmdb(target):
        targets.append(target)
        return {'metadata_state': 'waiting_review', 'reason_code': 'no_candidates'}
    target = {'preferred_title': '本地名称', 'identity_titles': ['本地名称']}
    for _ in range(2):
        result = recover_metadata(target, metadata_provider=tmdb, name_providers=[names], cache=store)
    assert len(calls) == 1
    assert len(targets) == 4
    assert targets[-1]['identity_titles'] == ['本地名称']
    assert result['alias_recovery_trace'][1]['cache_status'] == 'hit'


def test_network_failure_is_short_cooldown_not_negative_result(cache):
    _, clock, store = cache
    target = {'preferred_title': '本地名称'}
    store.put_error(target, 'anilist', 'provider_timeout', 30)
    row = store.get(target, 'anilist')
    assert row['status'] == 'cooldown' and row['reason_code'] == 'provider_timeout'
    clock[0] += 31
    assert store.get(target, 'anilist') is None


def test_cancellation_does_not_publish_positive_cache(cache):
    _, _, store = cache
    cancelled = [False]
    def names(_target):
        cancelled[0] = True
        return evidence()
    target = {'preferred_title': '本地名称'}
    recover_metadata(target, metadata_provider=lambda _: {'metadata_state': 'waiting_review', 'reason_code': 'no_candidates'},
                     name_providers=[names], cache=store, should_cancel=lambda: cancelled[0])
    assert store.get(target, 'names') is None


def test_cache_eviction_only_removes_cache_namespace(cache):
    database, _, store = cache
    with database.connect() as conn:
        conn.execute("INSERT INTO v4_meta(key,value) VALUES ('library_projection_dirty','keep')")
    store.put({'preferred_title': '本地名称'}, 'anilist', evidence())
    store.clear()
    assert store.get({'preferred_title': '本地名称'}, 'anilist') is None
    with database.connect() as conn:
        assert conn.execute("SELECT value FROM v4_meta WHERE key='library_projection_dirty'").fetchone()[0] == 'keep'


def test_anilist_name_query_has_no_artwork_people_or_description():
    import httpx

    from app.scrape.anilist_client import AniListClient
    queries = []
    def handler(request):
        queries.append(request.content.decode())
        return httpx.Response(200, json={'data': {'Page': {'media': []}}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        AniListClient(rate_limit=0, _http_client=client).search_names('本地名称')
    assert len(queries) == 1
    for forbidden in ('coverImage', 'bannerImage', 'description', 'characters', 'staff', 'popularity'):
        assert forbidden not in queries[0]


@pytest.mark.parametrize('status,reason', [(401, 'provider_unauthorized'), (503, 'provider_unavailable')])
def test_anilist_failure_is_classified_without_response_body(status, reason):
    import httpx

    from app.scrape.anilist_client import AniListClient, AniListClientError
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, text='fixture-private-response'))) as client:
        with pytest.raises(AniListClientError) as error:
            AniListClient(rate_limit=0, _http_client=client).search_names('本地名称')
    assert error.value.reason_code == reason
    assert 'fixture-private-response' not in str(error.value)


def test_bangumi_public_name_client_does_not_resolve_account_token(monkeypatch):
    from app.integrations.bangumi import BangumiClient
    def denied():
        raise AssertionError('public name recovery read account token')
    monkeypatch.setattr('app.integrations.bangumi.resolve_bangumi_access_token', denied)
    assert BangumiClient(public_only=True).access_token == ''


def test_bangumi_detail_429_sets_shared_cooldown(monkeypatch):
    from app.integrations.bangumi import BangumiError
    from app.media_v4.jobs import alias_recovery
    from app.scrape.provider_budget import ProviderDeferred
    cooled = []
    monkeypatch.setattr(alias_recovery, 'acquire', lambda *_: None)
    monkeypatch.setattr(alias_recovery, 'cool_down', lambda *args: cooled.append(args))
    def denied():
        raise BangumiError('fixture', status_code=429, retry_after='invalid')
    with pytest.raises(ProviderDeferred):
        alias_recovery._bangumi_request({}, denied)
    assert cooled == [('bangumi_alias', 60)]
