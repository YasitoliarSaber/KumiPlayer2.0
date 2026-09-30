"""最近观看只消费所播放 Episode 的展示编号与成功资料。"""

import json

import pytest

from .test_v4_p006 import _fresh_database, _seed_work


@pytest.fixture
def history_database(tmp_path, monkeypatch):
    from app.api import playback_v4
    from app.media_v4.playback.store import V4PlaybackStore

    database = _fresh_database(tmp_path)
    _seed_work(database, work_id="w1", title="历史作品")
    monkeypatch.setattr(playback_v4, "get_database", lambda: database)
    store = V4PlaybackStore(database)
    store.record_activation("w1", "ep-w1-1-3", "asset-w1-1-3")
    store.save_progress("w1", "ep-w1-1-3", "asset-w1-1-3", 694, 1422, False)
    return database


def _metadata(database, mappings):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO scrape_bindings(binding_id, revision_id, work_id, provider, provider_id, "
            "metadata_json, created_at, updated_at) VALUES ('sb-history', 'rev-p', 'w1', 'tmdb', '1', ?, 'now', 'now')",
            (json.dumps({"fanart_path": "/work-background.jpg", "episode_mappings": mappings}),),
        )


def test_history_returns_played_episode_number_and_its_local_thumbnail(history_database):
    from app.api.playback_v4 import history

    _metadata(history_database, [
        {"episode_id": "ep-w1-1-1", "still_url": "/wrong-episode.jpg"},
        {"episode_id": "ep-w1-1-3", "local_thumb_path": "/mirror/ep-three.jpg", "still_url": "/remote.jpg", "title": "第三集标题"},
    ])
    item = history(limit=1)["items"][0]
    assert item["season_number"] == 1
    assert item["episode_number"] == 3
    assert item["thumb_path"] == "/mirror/ep-three.jpg"
    assert item["episode_title"] == "第三集标题"
    assert item["position"] == 694
    assert item["season_snapshot"] == "第 1 季"


def test_unmapped_history_episode_never_uses_work_background(history_database):
    from app.api.playback_v4 import history

    _metadata(history_database, [{"episode_id": "ep-w1-1-1", "still_url": "/wrong-episode.jpg"}])
    item = history(limit=1)["items"][0]
    assert item["thumb_path"] == ""
    assert item["episode_number"] == 3


def test_history_uses_retained_successful_episode_artwork(history_database, monkeypatch):
    from app.api.playback_v4 import history

    _metadata(history_database, [{"episode_id": "ep-w1-1-3", "still_url": "/partial-refresh.jpg"}])
    monkeypatch.setattr(
        "app.media_v4.persistence.metadata_lifecycle.referenced_metadata",
        lambda _conn, _work_id: {"episode_mappings": [{"episode_id": "ep-w1-1-3", "local_thumb_path": "/mirror/retained.jpg"}]},
    )
    assert history(limit=1)["items"][0]["thumb_path"] == "/mirror/retained.jpg"


def test_history_preserves_orphan_event_snapshots_without_inventing_numbers(history_database):
    from app.api.playback_v4 import history

    with history_database.connect() as conn:
        conn.execute(
            "INSERT INTO playback_history(event_id, work_id, episode_id, asset_id, played_at, "
            "season_snapshot, episode_snapshot) VALUES ('old-event', 'w1', 'removed-episode', 'old-asset', "
            "'9999-01-01', '第 2 季', '第 6 集')"
        )
    item = history(limit=1)["items"][0]
    assert item["season_number"] is None
    assert item["episode_number"] is None
    assert item["episode_snapshot"] == "第 6 集"
    assert item["thumb_path"] == ""
    assert item["position"] == 0
