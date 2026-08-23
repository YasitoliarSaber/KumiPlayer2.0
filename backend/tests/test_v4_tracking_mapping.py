"""V4 外部追踪状态不能覆盖本地集号。"""

from __future__ import annotations


def test_tracking_state_is_separate_from_episode_number(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.tracking.store import V4TrackingStore

    database = V4Database(tmp_path / "tracking.db")
    database.initialize()
    store = V4TrackingStore(database)

    store.save_state("work-1", "bangumi", provider_id="123", last_watched_episode=99)
    state = store.get_state("work-1", "bangumi")

    assert state["provider_id"] == "123"
    assert state["last_watched_episode"] == 99
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
