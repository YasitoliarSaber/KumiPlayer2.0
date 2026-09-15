"""来源卡待处理数量必须来自当前可处理事实，不来自历史关系提示。"""
import json
from dataclasses import replace

import pytest

from app.media_v4.persistence.database import V4Database
from app.media_v4.projection.source_libraries import list_source_cards
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.revisions.service import V4RevisionService

from .test_v4_revision_confirmation import _entry


@pytest.mark.parametrize(('title', 'media_type'), [('龙猫', 'movie'), ('福音战士新剧场版:序', 'movie'), ('颂乐人偶', 'tv')])
def test_provider_identity_does_not_create_relation_to_own_title(title, media_type):
    evidence, facts = _entry(title)
    facts = replace(facts, series_group=title, media_type=media_type,
                    tmdb_hint_id='42', tmdb_hint_type=media_type)
    graph = MediaResolver().resolve([(evidence, facts)])
    assert len(graph.works) == 1
    assert graph.relations == ()


def _completed_source(tmp_path):
    database = V4Database(tmp_path / 'source.db')
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft('rev', [_entry('龙猫')])
    service.confirm('rev')
    with database.connect() as conn:
        wid = conn.execute('SELECT work_id FROM works').fetchone()[0]
        conn.execute("UPDATE jobs SET status='succeeded'")
        conn.execute(
            "INSERT INTO scrape_bindings(binding_id,revision_id,work_id,provider,provider_id,metadata_json,status,created_at,updated_at) "
            "VALUES ('sb','rev',?,'tmdb','42',?,'confirmed','now','now')",
            (wid, json.dumps({'metadata_state': 'ready'})),
        )
    return database, wid


@pytest.mark.parametrize(('parent', 'notices'), [('龙猫', 0), ('另外的系列', 1)])
def test_completed_source_separates_relation_notices_without_deleting_history(tmp_path, parent, notices):
    database, wid = _completed_source(tmp_path)
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO revision_issues VALUES ('rev','legacy','unresolved_parent_relation',?,?,0)",
            (wid, f'父系列 series:{parent}:tv 尚未导入，无法建立作品关系'),
        )
    card = list_source_cards(database)[0]
    assert card['overall_status'] == 'completed'
    assert card['attention_count'] == 0
    assert card['relation_pending_count'] == notices
    with database.connect() as conn:
        assert conn.execute('SELECT resolved FROM revision_issues').fetchone()[0] == 0


@pytest.mark.parametrize('failure', ['metadata', 'mirror', 'projection', 'cleanup'])
def test_real_failure_is_counted_even_when_work_is_in_library(tmp_path, failure):
    database, _wid = _completed_source(tmp_path)
    with database.connect() as conn:
        if failure == 'metadata':
            conn.execute("UPDATE scrape_bindings SET metadata_json=?", (json.dumps({'metadata_state': 'source_unavailable'}),))
        else:
            kind = 'materialize_mirror' if failure == 'mirror' else 'refresh_projection'
            conn.execute("UPDATE jobs SET status='failed' WHERE job_type=?", (kind,))
            if failure == 'cleanup':
                conn.execute("UPDATE jobs SET job_type='cleanup_superseded_artifacts' WHERE job_type=?", (kind,))
    card = list_source_cards(database)[0]
    assert card['work_count'] == 1
    assert card['overall_status'] == 'needs_attention'
    assert card['attention_count'] == 1


def test_generic_provider_name_uses_source_folder_not_truncated_provider():
    from app.media_v4.projection.source_libraries import _display_name
    assert _display_name('百度网盘', 'K:/百度网盘/01动画', 'baidu') == '01动画'
    assert _display_name('我的动画库', 'K:/百度网盘/01动画', 'baidu') == '我的动画库'


def test_same_title_movie_can_still_relate_to_tv_parent():
    from app.media_v4.resolution.resolver import _relation_work_key_from_row
    assert _relation_work_key_from_row({
        'title': '同名作品', 'series_group': '同名作品', 'media_type': 'movie', 'relation_media_type': 'tv',
    }) == 'series:同名作品:tv'
