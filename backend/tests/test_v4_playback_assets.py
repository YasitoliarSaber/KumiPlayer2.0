"""V4 播放状态只引用媒体图，不改变媒体身份。"""

from __future__ import annotations


def test_playback_progress_is_stored_per_episode_and_asset(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.store import V4PlaybackStore

    database = V4Database(tmp_path / "playback.db")
    database.initialize()
    store = V4PlaybackStore(database)

    store.save_progress("work-1", "episode-1", "asset-4k", 123.5, 900.0, False)
    progress = store.get_progress("episode-1", "asset-4k")

    assert progress["work_id"] == "work-1"
    assert progress["position"] == 123.5
    assert progress["completed"] == 0
