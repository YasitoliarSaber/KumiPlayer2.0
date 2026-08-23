"""V4 外部追踪状态不能覆盖本地集号。"""

from __future__ import annotations

import pytest


def test_tracking_state_is_separate_from_episode_number(tmp_path):
    from backend.tests.test_v4_revision_confirmation import _entry

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService
    from app.media_v4.tracking.store import V4TrackingStore

    database = V4Database(tmp_path / "tracking.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-track", [_entry(evidence_id="track")])
    revisions.confirm("rev-track")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()[0]
    store = V4TrackingStore(database)

    store.save_state(work_id, "bangumi", provider_id="123", last_watched_episode=99)
    state = store.get_state(work_id, "bangumi")

    assert state["provider_id"] == "123"
    assert state["last_watched_episode"] == 99
    with database.connect() as conn:
        assert conn.execute("SELECT local_episode_number FROM episodes").fetchone()[0] == 1
    with pytest.raises(KeyError):
        store.save_state("missing", "bangumi", provider_id="x", last_watched_episode=1)
