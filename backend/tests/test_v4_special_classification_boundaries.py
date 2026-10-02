"""零集、小数集号、OVA/OAD 与编号 SP 准入；原名不是在线编号。"""
from pathlib import PurePosixPath

import pytest

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.resolution.resolver import MediaResolver


def pair(path):
    evidence = SourceEvidence(
        evidence_id=path, scan_id='special-boundaries', root_id='offline-special-boundaries',
        source_key=path, relative_path=path, entry_kind='video', provider='baidu',
        ingest_method='directory_tree', playback_locator='Z:/offline-fixture/' + path,
    )
    return evidence, V4Parser().parse(evidence)


@pytest.mark.parametrize('path', [
    '作品甲/作品甲 [OVA].mkv', '作品甲/作品甲 OVA01.mkv',
    '作品甲/作品甲 OAD02.mkv', '作品甲/OVA/夏日的小故事.mkv',
    '作品甲/Specials/作品甲 - S00E02 - OVA1：通向天堂的阶梯.mkv',
    '作品甲/Season 1/作品甲 第8.5集.mkv', '作品甲/作品甲 第0集.mkv',
    '作品甲/Specials/作品甲 SP01.mkv', '作品甲/作品甲 SP02.mkv',
    '作品甲/Season 1/作品甲 [SP01].mkv',
    '2.5次元的诱惑/Season 1/2.5次元的诱惑 [14.5].mkv',
])
def test_selected_special_markers_need_no_explicit_regular_season(path):
    _, facts = pair(path)
    assert facts.is_importable and not facts.is_auxiliary
    assert facts.content_class == 'playable_special'
    assert facts.group_type == 'special'
    assert facts.episode_title == PurePosixPath(path).stem
    graph = MediaResolver().resolve([pair(path)])
    assert len(graph.episodes) == 1
    episode = graph.episodes[0]
    assert episode.season_kind == episode.episode_kind == 'special'
    assert episode.provider_season_number is None
    assert episode.provider_episode_number is None


@pytest.mark.parametrize('path', [
    '作品甲/Season 1/作品甲 [OVA][NCOP01].mkv',
    '作品甲/Season 1/作品甲 OAD [PV01].mkv',
    '作品甲/Season 1/作品甲 第0集 花絮.mkv',
])
def test_production_material_has_priority_over_selected_special_markers(path):
    assert not pair(path)[1].is_importable
    assert not MediaResolver().resolve([pair(path)]).episodes


@pytest.mark.parametrize('path', [
    '2.5次元的诱惑/Season 1/2.5次元的诱惑.S01E01.mkv',
    '2.5 Dimensional Seduction/Season 1/2.5 Dimensional Seduction.S01E01.mkv',
    '作品 8.5/Season 1/作品 8.5 S01E01.mkv',
    '作品甲/Season 1/作品甲 S01E01 [AAC 5.1][23.976fps].mkv',
    '作品甲/Season 1/作品甲 S01E01 v1.5.mkv',
    '作品 [2.5]/Season 1/作品 [2.5] S01E01.mkv',
    'SPY x FAMILY/Season 1/SPY x FAMILY S01E01.mkv',
])
def test_title_and_technical_decimals_do_not_override_explicit_regular_episode(path):
    _, facts = pair(path)
    assert facts.group_type == 'season'
    assert facts.episode_candidate == 1


@pytest.mark.parametrize('path', [
    '作品甲/Season 0/作品甲.S00E03.mkv',
    '作品甲/Specials/通向天堂的阶梯.mkv',
])
def test_other_special_markers_keep_existing_scope(path):
    assert not pair(path)[1].is_importable


def test_selected_specials_do_not_claim_regular_episode_one():
    paths = ['作品甲/Season 1/作品甲.S01E01.mkv',
             '作品甲/Season 1/作品甲 OVA01.mkv',
             '作品甲/Season 1/作品甲 OAD01.mkv',
             '作品甲/Season 1/作品甲 SP01.mkv',
             '作品甲/Season 1/作品甲 SP02.mkv',
             '作品甲/Season 1/作品甲 第0集.mkv',
             '作品甲/Season 1/作品甲 第8.5集.mkv']
    graph = MediaResolver().resolve([pair(path) for path in paths])
    assert len(graph.works) == 1
    assert len(graph.episodes) == 7
    assert sum(episode.season_kind == 'regular' for episode in graph.episodes) == 1
    specials = [episode for episode in graph.episodes if episode.season_kind == 'special']
    assert len({episode.special_number for episode in specials}) == 6
    assert all(episode.provider_episode_number is None for episode in specials)


@pytest.mark.parametrize('name', ['作品甲 OVA.mkv', '作品甲 OAD01.mkv', '作品甲 SP01.mkv', '作品甲 第0集.mkv'])
def test_single_special_never_becomes_movie(name):
    graph = MediaResolver().resolve([pair('作品甲/' + name)])
    assert len(graph.works) == 1
    assert graph.works[0].media_type == 'tv'
    assert len(graph.episodes) == 1
    assert graph.episodes[0].season_kind == 'special'
    assert not graph.work_assets


def test_online_episode_mapping_never_requests_special_season():
    from app.media_v4.jobs.metadata import _build_tv_episode_mappings

    class Client:
        calls = []

        def get_tv_season_episodes(self, _work, season):
            self.calls.append(season)
            return {'episodes': [{'id': 101, 'episode_number': 1, 'name': 'Regular'}]}

    client = Client()
    target = {'episodes': [
        {'episode_id': 'regular', 'local_season_number': 1, 'local_episode_number': 1},
        {'episode_id': 'special', 'local_season_number': 0, 'special_number': 1,
         'episode_kind': 'special', 'season_kind': 'special', 'display_title': 'OVA01'},
    ]}
    mappings = _build_tv_episode_mappings(client, 42, target)
    assert client.calls == [1]
    assert {row['episode_id'] for row in mappings} == {'regular'}


@pytest.mark.parametrize('with_regular', [False, True])
def test_confirmed_specials_are_displayed_without_any_scrape_target(tmp_path, monkeypatch, with_regular):
    from app.api import library_v4
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    paths = ['作品甲/作品甲 SP01.mkv', '作品甲/作品甲 OVA01.mkv']
    if with_regular:
        paths.append('作品甲/Season 1/作品甲 S01E01.mkv')
    database = V4Database(tmp_path / 'local-specials.db')
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft('local-specials', [pair(path) for path in paths])
    service.confirm('local-specials')
    received = []

    def provider(target):
        received.append(target)
        assert target['episodes']
        assert all(row['episode_kind'] == 'regular' for row in target['episodes'])
        return {'provider': 'local', 'metadata_state': 'waiting_metadata'}

    runner = V4JobRunner(database, metadata_provider=provider)
    runner.process_available(mirror_root=tmp_path / 'mirror')
    assert len(received) == int(with_regular)
    assert len(list((tmp_path / 'mirror').rglob('*.strm'))) == len(paths)
    assert not list((tmp_path / 'mirror').rglob('*.jpg'))
    assert not list((tmp_path / 'mirror').rglob('*.nfo'))
    with database.connect() as conn:
        work_id = conn.execute('SELECT work_id FROM works').fetchone()[0]
        assert conn.execute('SELECT COUNT(*) FROM episode_provider_mappings').fetchone()[0] == 0
        from app.media_v4.persistence.metadata_lifecycle import current_episode_signatures

        assert len(current_episode_signatures(conn, 'local-specials', work_id)) == int(with_regular)
        if not with_regular:
            assert not conn.execute("SELECT 1 FROM jobs WHERE job_type='recover_work_aliases'").fetchone()
    monkeypatch.setattr(library_v4, 'get_database', lambda: database)
    detail = library_v4.get_work_detail(work_id)
    assert len([row for row in detail['episodes'] if row['kind'] == 'special']) == 2
    if not with_regular:
        assert runner.projection.current().cards[0]['metadata']['metadata_state'] == 'not_required'
        assert len(library_v4.get_library()['works']) == 1
        assert service.get_execution_progress('local-specials')['overall_status'] == 'completed'


def test_special_and_explicit_movie_keep_separate_work_identities():
    graph = MediaResolver().resolve([
        pair('作品甲/作品甲 SP01.mkv'),
        pair('作品甲/作品甲 Movie.mkv'),
    ])
    assert len(graph.works) == 2
    assert {work.media_type for work in graph.works} == {'movie', 'tv'}
    assert len(graph.episodes) == 1
    assert graph.episodes[0].episode_kind == 'special'
    assert len(graph.work_assets) == 1


def test_regular_scrape_snapshot_does_not_require_special_metadata(tmp_path, monkeypatch):
    from app.core.config import AppConfig
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.metadata_lifecycle import referenced_metadata, select_applicable_snapshot
    from app.media_v4.revisions.service import V4RevisionService

    config = AppConfig(artwork_storage_mode='remote')
    monkeypatch.setattr('app.media_v4.jobs.metadata_artifacts.load_config', lambda: config)
    monkeypatch.setattr('app.media_v4.jobs.completeness.load_config', lambda: config)
    database = V4Database(tmp_path / 'regular-and-special.db')
    database.initialize()
    service = V4RevisionService(database)
    paths = ['作品甲/Season 1/作品甲 S01E01.mkv', '作品甲/作品甲 SP01.mkv']
    service.create_draft('mixed-ready', [pair(path) for path in paths])
    service.confirm('mixed-ready')

    def provider(target):
        assert len(target['episodes']) == 1
        return {
            'provider': 'tmdb', 'provider_id': '42', 'media_type': 'tv',
            'title': '作品甲', 'metadata_state': 'ready',
            'poster_url': 'https://image.tmdb.org/t/p/original/poster.jpg',
            'fanart_url': 'https://image.tmdb.org/t/p/original/fanart.jpg',
            'episode_mappings': [{
                'episode_id': target['episodes'][0]['episode_id'], 'provider_episode_id': '101',
                'provider_season_number': 1, 'provider_episode_number': 1, 'title': '正片',
                'still_url': 'https://image.tmdb.org/t/p/w500/still.jpg',
            }],
        }

    runner = V4JobRunner(database, metadata_provider=provider)
    runner.process_available(mirror_root=tmp_path / 'mirror')
    card = runner.projection.current().cards[0]
    assert card['metadata']['metadata_state'] == 'ready'
    assert card['metadata']['total_count'] == card['metadata']['mapped_count'] == 1
    assert len(list((tmp_path / 'mirror').rglob('*.strm'))) == 2
    assert len(list((tmp_path / 'mirror').rglob('*.nfo'))) == 2  # 作品与正片，特别篇没有 NFO。
    with database.connect() as conn:
        snapshot = select_applicable_snapshot(conn, 'mixed-ready', card['work_id'])
        assert snapshot['covers_members']
        metadata = referenced_metadata(conn, card['work_id'])
        assert metadata['total_count'] == metadata['mapped_count'] == 1
        assert metadata['unmapped_episode_ids'] == []
