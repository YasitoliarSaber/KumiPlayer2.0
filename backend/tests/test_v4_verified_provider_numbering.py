"""真实失败的六种编号布局；远端条目使用合成 ID/图片，不访问媒体或网盘。"""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.media_v4.jobs import metadata

CASES = [
    (123249, (12, 12), 1),
    (241811, (12, 12), 1),
    (116727, (13,), 2),
    (274810, (13,), 2),
    (95479, (24, 23, 12), 1),
    (65942, (25, 25, 16), 1),
]
TITLES = {
    116727: ('奇怪的司机', '度过长夜的方法', '小心虚张声势', '田中革命',
             '别叫我偶像', '我才想问是在搞什么', '不给糖就捣蛋', '祝你保重',
             '英雄的忧郁', '我们没有明天', '如果能回到那天', '不足的两人', '请问要去哪里？'),
    274810: ('渴望赴死的她在等待大海', '斜阳之兽与祭典音乐', '希望之海', '泡沫的结点',
             '亲爱之兽', '亲爱之形', '温柔的人', '裂痕之始', '烙下的祈愿',
             '倾注祈愿', '冰冷的清晨', '深爱的孩子', '温暖的海底'),
}


def target_for(provider_id, counts, first_season=1):
    episodes = []
    for season, count in enumerate(counts, first_season):
        for number in range(1, count + 1):
            episodes.append({
                'episode_id': f'{season}-{number}', 'season_id': f'season-{season}',
                'local_season_number': season, 'local_episode_number': number,
                'display_title': TITLES[provider_id][number - 1] if provider_id in TITLES else '',
            })
    return {'work_type': 'series', 'preferred_title': 'Fixture', 'episodes': episodes,
            'provider_bindings': [{'provider': 'tmdb', 'provider_id': str(provider_id), 'media_type': 'tv'}]}


def install_client(monkeypatch, total, *, seasons=(1,)):
    requested = []
    remote = [{'episode_number': n, 'season_number': 1, 'id': 10000 + n,
               'name': f'Online episode {n}', 'still_path': f'/fixture-{n}.jpg'}
              for n in range(1, total + 1)]

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_tv_detail(self, provider_id):
            return {'name': 'Fixture', 'seasons': [{'season_number': s} for s in seasons]}
        def get_tv_season_episodes(self, provider_id, season):
            requested.append(season)
            return {'episodes': remote if season == 1 else []}
        def select_best_poster(self, images): return '/poster.jpg'
        def select_best_backdrop(self, images): return '/fanart.jpg'
        def select_best_logo(self, images): return ''
        def build_image_url(self, path, size): return f'https://image.tmdb.org/t/p/{size}{path}'

    monkeypatch.setattr(metadata, 'TMDBClient', Client)
    monkeypatch.setattr(metadata, 'load_config', lambda: SimpleNamespace(tmdb_bearer_token='fixture'))
    return requested, remote


@pytest.mark.parametrize('provider_id,counts,first_season', CASES)
def test_six_reported_layouts_get_real_episode_fields_without_changing_local_facts(
    monkeypatch, provider_id, counts, first_season,
):
    requested, remote = install_client(monkeypatch, sum(counts))
    target = target_for(provider_id, counts, first_season)
    original = deepcopy(target)
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'ready'
    assert result['episode_mapping_status'] == 'complete'
    assert len(result['episode_mappings']) == sum(counts)
    for number, mapping in enumerate(result['episode_mappings'], 1):
        assert (mapping['provider_season_number'], mapping['provider_episode_number']) == (1, number)
        assert mapping['provider_episode_id'] == str(remote[number - 1]['id'])
        assert mapping['title'] == remote[number - 1]['name']
        assert mapping['still_url'].endswith(f'/fixture-{number}.jpg')
        assert mapping['mapping_evidence']['rule_version']
    assert target == original
    assert requested == [1]


def test_missing_local_files_and_only_second_season_do_not_change_verified_offset(monkeypatch):
    requested, _ = install_client(monkeypatch, 24)
    target = target_for(123249, (12, 12))
    target['episodes'] = [e for e in target['episodes'] if e['episode_id'] == '2-3']
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'ready'
    assert result['episode_mappings'][0]['provider_episode_number'] == 15
    assert requested == [1]


@pytest.mark.parametrize('provider_id', [116727, 274810])
def test_mislabeled_season_requires_each_episode_title_evidence(monkeypatch, provider_id):
    install_client(monkeypatch, 13)
    target = target_for(provider_id, (13,), 2)
    target['episodes'][1]['display_title'] = 'Unrelated sequel'
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'failed'
    assert len(result['episode_mappings']) == 12
    assert '2-2' not in {m['episode_id'] for m in result['episode_mappings']}


@pytest.mark.parametrize('provider_id,number', [(123249, 13), (65942, 17), (95479, 13)])
def test_verified_ranges_do_not_expand_to_future_episodes(monkeypatch, provider_id, number):
    install_client(monkeypatch, 100)
    season = 2 if provider_id == 123249 else 3
    target = target_for(provider_id, (1,), season)
    target['episodes'][0]['local_episode_number'] = number
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'failed'
    assert result['episode_mappings'] == []


def test_unknown_work_does_not_infer_offset_from_same_counts(monkeypatch):
    install_client(monkeypatch, 24)
    result = metadata.default_metadata_provider(target_for(42, (12, 12)))
    assert result['metadata_state'] == 'failed'
    assert len(result['episode_mappings']) == 12


@pytest.mark.parametrize('missing', ['id', 'name', 'still_path'])
def test_verified_rule_never_hides_missing_provider_fields(monkeypatch, missing):
    _, remote = install_client(monkeypatch, 24)
    remote[12][missing] = ''
    result = metadata.default_metadata_provider(target_for(123249, (12, 12)))
    assert result['metadata_state'] == 'failed'
    assert result['reason_code'] in {'episode_mapping_incomplete', 'episode_details_incomplete'}


def test_real_provider_multiseason_layout_bypasses_continuous_rules(monkeypatch):
    requested, _ = install_client(monkeypatch, 24, seasons=(1, 2))
    result = metadata.default_metadata_provider(target_for(123249, (12, 12)))
    assert result['metadata_state'] == 'failed'
    assert requested == [1, 2]
    assert all(m['episode_id'].startswith('1-') for m in result['episode_mappings'])


def test_explicit_per_episode_and_absolute_evidence_keep_priority(monkeypatch):
    install_client(monkeypatch, 24)
    target = target_for(123249, (12, 12))
    target['episodes'] = target['episodes'][12:14]
    target['episodes'][0].update(provider_season_number=1, provider_episode_number=20)
    target['episodes'][1].update(absolute_episode_number=18, absolute_origin='explicit_filename')
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'ready'
    assert [m['provider_episode_number'] for m in result['episode_mappings']] == [20, 18]


def test_unique_episode_title_maps_unlisted_work_without_guessing_file_order(monkeypatch):
    requested, remote = install_client(monkeypatch, 30)
    remote[16]['name'] = '旅程的终点'
    target = target_for(42, (1,), 3)
    target['episodes'][0]['display_title'] = '旅程的终点！'
    original = deepcopy(target)
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'ready'
    mapping = result['episode_mappings'][0]
    assert mapping['provider_episode_number'] == 17
    assert mapping['mapping_basis'] == 'verified_episode_title'
    assert target == original
    assert requested == [1]


@pytest.mark.parametrize('ambiguous', ['online_duplicate', 'local_duplicate', 'placeholder'])
def test_episode_title_matching_refuses_ambiguity_and_placeholders(monkeypatch, ambiguous):
    _, remote = install_client(monkeypatch, 30)
    remote[16]['name'] = '旅程的终点'
    target = target_for(42, (2,), 3)
    target['episodes'][0]['display_title'] = '旅程的终点'
    if ambiguous == 'online_duplicate':
        remote[17]['name'] = '旅程的终点'
    elif ambiguous == 'local_duplicate':
        target['episodes'][1]['display_title'] = '旅程的终点'
    else:
        remote[16]['name'] = target['episodes'][0]['display_title'] = '第1集'
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'failed'
    assert result['episode_mappings'] == []


def test_mislabeled_title_cannot_duplicate_an_already_numbered_first_season(monkeypatch):
    install_client(monkeypatch, 13)
    target = target_for(116727, (1,), 1)
    target['episodes'].extend(target_for(116727, (1,), 2)['episodes'])
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'failed'
    assert len(result['episode_mappings']) == 1
    assert result['episode_mappings'][0]['episode_id'] == '1-1'


def test_verified_offset_conflicting_with_unique_online_title_is_not_silently_used(monkeypatch):
    _, remote = install_client(monkeypatch, 24)
    remote[0]['name'] = '独一无二的第一话'
    target = target_for(123249, (1,), 2)
    target['episodes'][0]['display_title'] = remote[0]['name']
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'failed'
    assert result['episode_mappings'] == []
    assert 'numbering_conflict' in result['unmapped_reason_codes']


def test_title_mapping_does_not_reuse_episode_claimed_by_explicit_number(monkeypatch):
    _, remote = install_client(monkeypatch, 13)
    remote[0]['name'] = '独一无二的第一话'
    target = target_for(42, (1, 1))
    target['episodes'][1]['display_title'] = remote[0]['name']
    result = metadata.default_metadata_provider(target)
    assert result['metadata_state'] == 'failed'
    assert [m['episode_id'] for m in result['episode_mappings']] == ['1-1']


@pytest.mark.parametrize('provider_id,counts,first_season', CASES)
def test_confirmed_import_and_rescrape_publish_correct_fields_in_sqlite_and_detail(
    tmp_path, monkeypatch, provider_id, counts, first_season,
):
    """真实 V4 作业/SQLite/投影，只有 TMDB 客户端被替换；媒体路径只是词法夹具。"""
    from app.core.config import AppConfig
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.parsing.parser import V4Parser
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    requested, _ = install_client(monkeypatch, sum(counts))
    config = AppConfig(artwork_storage_mode='remote')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts.load_config', lambda: config)
    monkeypatch.setattr('app.media_v4.jobs.completeness.load_config', lambda: config)
    database = V4Database(tmp_path / 'fixture.db')
    database.initialize()
    target = target_for(provider_id, counts, first_season)
    entries = []
    for episode in target['episodes']:
        season = episode['local_season_number']
        number = episode['local_episode_number']
        name = f'Fixture.S{season:02d}E{number:02d}'
        if episode['display_title']:
            name += f" - {episode['display_title']}"
        evidence = to_source_evidence(SourceEntry(
            root_id='offline-fixture', scan_id='r1', provider='local', ingest_method='directory_tree',
            relative_path=f'Fixture/Season {season}/{name}.mkv',
            playback_locator=str(tmp_path / 'source-fixture' / f'{name}.mkv'),
            tmdb_hint_id=str(provider_id), tmdb_hint_type='tv',
        ))
        entries.append((evidence, V4Parser().parse(evidence)))
    service = V4RevisionService(database)
    service.create_draft('r1', entries)
    service.confirm('r1')
    with database.connect() as conn:
        original_revision = [tuple(r) for r in conn.execute(
            "SELECT evidence_id,resolved_json FROM revision_bindings WHERE revision_id='r1' ORDER BY evidence_id")]
        original_numbers = [tuple(r) for r in conn.execute(
            'SELECT episode_id,local_episode_number,display_title FROM episodes ORDER BY episode_id')]
    runner = V4JobRunner(database, metadata_provider=metadata.default_metadata_provider)
    mirror = tmp_path / 'mirror'
    runner.process_available(mirror_root=mirror)
    card = V4LibraryProjection(database).ensure_current().cards[0]
    assert card['episode_count'] == sum(counts)
    assert card['metadata']['metadata_state'] == 'ready'
    with database.connect() as conn:
        saved = json.loads(conn.execute('SELECT metadata_json FROM metadata_snapshots').fetchone()[0])
        assert len(saved['episode_mappings']) == sum(counts)
        assert all(m.get('mapping_evidence', {}).get('rule_version') for m in saved['episode_mappings'])
        assert conn.execute('SELECT COUNT(*) FROM episode_provider_mappings').fetchone()[0] == sum(counts)
        assert [tuple(r) for r in conn.execute(
            "SELECT evidence_id,resolved_json FROM revision_bindings WHERE revision_id='r1' ORDER BY evidence_id")] == original_revision
        assert [tuple(r) for r in conn.execute(
            'SELECT episode_id,local_episode_number,display_title FROM episodes ORDER BY episode_id')] == original_numbers
    assert requested == [1]
    # 从已入库作品触发重新刮削，沿用在线身份与每集对应，不需要再扫描来源。
    V4ScrapeService(database).requeue_work('r1', card['work_id'])
    runner.process_available(mirror_root=mirror)
    refreshed = V4LibraryProjection(database).ensure_current().cards[0]
    assert refreshed['metadata']['metadata_state'] == 'ready'
    assert requested == [1, 1]
    # 局部刷新按本地季选择成员，但在线请求仍在正确的 Provider 命名空间。
    V4ScrapeService(database).requeue_work('r1', card['work_id'], season_number=first_season + len(counts) - 1)
    runner.process_available(mirror_root=mirror)
    assert V4LibraryProjection(database).ensure_current().cards[0]['metadata']['metadata_state'] == 'ready'
    assert requested == [1, 1, 1]
    from app.api import library_v4
    monkeypatch.setattr(library_v4, 'get_database', lambda: database)
    detail = library_v4.get_work_detail(card['work_id'])
    assert len(detail['episodes']) == sum(counts)
    for number, episode in enumerate(detail['episodes'], 1):
        assert episode['title'] == f'Online episode {number}'
        assert episode['thumb_path'].endswith(f'/fixture-{number}.jpg')
    assert [(e['season_number'], e['episode_number']) for e in detail['episodes']] == [
        (e['local_season_number'], e['local_episode_number']) for e in target['episodes']]
