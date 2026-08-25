"""V4-backed Bangumi detail API regressions."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    from app.api import bangumi, media_v4
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "bangumi-v4.db")
    database.initialize()
    evidence = SourceEvidence(
        evidence_id="evidence-1", scan_id="scan-1", root_id="root-1", source_key="episode-1",
        relative_path="Show/Show.S01E01.mkv", entry_kind="video", provider="local",
        source_locator="local://show/01.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id="fact-1", evidence_id=evidence.evidence_id, parser_version="fixture",
        work_title="Show", title_candidates=("Show",), media_type="tv", group_type="season",
        season_candidate=1, episode_candidate=1,
    )
    revisions = V4RevisionService(database)
    revisions.create_draft("revision-1", [(evidence, facts)])
    revisions.confirm("revision-1")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works").fetchone()["work_id"]

    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(bangumi, "get_database", lambda: database, raising=False)
    application = FastAPI()
    application.include_router(bangumi.router)
    return TestClient(application), work_id, database


def test_v4_bangumi_match_is_bound_to_existing_work_and_season(tmp_path, monkeypatch):
    from app.api import bangumi

    class FakeBangumiClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_subject_episodes(self, subject_id):
            assert subject_id == 100
            return [{"id": 9001, "ep": 1, "type": 0}]

    monkeypatch.setattr(bangumi, "BangumiClient", FakeBangumiClient)
    client, work_id, _database = _client(tmp_path, monkeypatch)

    confirmed = client.post(
        f"/api/integrations/bangumi/matches/{work_id}",
        json={"subject_id": 100, "season_number": 1, "subject_name": "Show", "subject_name_cn": "作品"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["episode_map"]

    episodes = client.get(f"/api/integrations/bangumi/episodes/{work_id}?season_number=1")
    assert episodes.status_code == 200, episodes.text
    assert episodes.json()["episodes"][0]["bangumi_episode_id"] == 9001


def test_v4_bangumi_sync_pushes_completed_episode_without_legacy_state(tmp_path, monkeypatch):
    from app.api import bangumi

    class FakeBangumiClient:
        pushed: list[int] = []

        def __init__(self, *args, **kwargs):
            pass

        def list_subject_episodes(self, subject_id):
            return [{"id": 9001, "ep": 1, "type": 0}]

        def get_episode_collection(self, subject_id):
            return {"data": []}

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type):
            self.pushed.extend(episode_ids)
            return {"ok": True}

    monkeypatch.setattr(bangumi, "BangumiClient", FakeBangumiClient)
    client, work_id, database = _client(tmp_path, monkeypatch)
    assert client.post(
        f"/api/integrations/bangumi/matches/{work_id}",
        json={"subject_id": 100, "season_number": 1},
    ).status_code == 200
    with database.connect() as conn:
        row = conn.execute(
            "SELECT ea.episode_id, ea.asset_id FROM episode_assets ea LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO playback_progress(episode_id, asset_id, work_id, position, duration, completed, updated_at) "
            "VALUES (?, ?, ?, 1, 1, 1, '2026-08-25T00:00:00+08:00')",
            (row["episode_id"], row["asset_id"], work_id),
        )

    synced = client.post(f"/api/integrations/bangumi/progress/{work_id}/sync", json={"season_number": 1})
    assert synced.status_code == 200, synced.text
    assert synced.json()["pushed"] == 1
    assert FakeBangumiClient.pushed == [9001]
    assert client.get(f"/api/integrations/bangumi/episodes/{work_id}?season_number=1").json()["episodes"][0]["synced"] is True


def test_v4_bangumi_sync_marks_existing_asset_completed_from_remote(tmp_path, monkeypatch):
    """远端已看只能回写已确认的 V4 Episode/Asset，不能生成孤立进度。"""

    from app.api import bangumi

    class FakeBangumiClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_subject_episodes(self, subject_id):
            return [{"id": 9001, "ep": 1, "type": 0}]

        def get_episode_collection(self, subject_id):
            return {"data": [{"type": 2, "episode": {"id": 9001}}]}

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type):
            raise AssertionError("远端已看剧集不应重复推送")

    monkeypatch.setattr(bangumi, "BangumiClient", FakeBangumiClient)
    client, work_id, database = _client(tmp_path, monkeypatch)
    assert client.post(
        f"/api/integrations/bangumi/matches/{work_id}",
        json={"subject_id": 100, "season_number": 1},
    ).status_code == 200

    synced = client.post(f"/api/integrations/bangumi/progress/{work_id}/sync", json={"season_number": 1})
    assert synced.status_code == 200, synced.text
    assert synced.json()["pulled"] == 1
    with database.connect() as conn:
        progress = conn.execute(
            "SELECT completed FROM playback_progress WHERE work_id = ?",
            (work_id,),
        ).fetchone()
    assert progress is not None
    assert progress["completed"] == 1
