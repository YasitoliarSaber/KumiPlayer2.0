"""V4 播放状态只引用媒体图，不改变媒体身份。"""

from __future__ import annotations

import pytest


def test_playback_progress_is_stored_per_episode_and_asset(tmp_path):
    from backend.tests.test_v4_revision_confirmation import _entry

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.store import V4PlaybackStore
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "playback.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-progress", [_entry(evidence_id="progress")])
    revisions.confirm("rev-progress")
    with database.connect() as conn:
        binding = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings LIMIT 1"
        ).fetchone()
    store = V4PlaybackStore(database)

    store.save_progress(
        binding["work_id"], binding["episode_id"], binding["asset_id"], 123.5, 900.0, False
    )
    progress = store.get_progress(binding["episode_id"], binding["asset_id"])

    assert progress["work_id"] == binding["work_id"]
    assert progress["position"] == 123.5
    assert progress["completed"] == 0

    with pytest.raises(KeyError):
        store.save_progress("missing", "missing", "missing", 1, 2, False)


def test_playback_session_launches_mpv_for_confirmed_asset(tmp_path, monkeypatch):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.revisions.service import V4RevisionService

    media = tmp_path / "Show.S01E01.mkv"
    media.write_bytes(b"fixture")
    evidence = SourceEvidence(
        evidence_id="ev-play",
        scan_id="scan-play",
        root_id="root-play",
        source_key=media.name,
        relative_path=media.name,
        entry_kind="video",
        provider="local",
        source_locator=str(media),
        playback_locator=str(media),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-play",
        evidence_id="ev-play",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    database = V4Database(tmp_path / "play.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-play", [(evidence, facts)])
    revisions.confirm("rev-play")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT rb.work_id, rb.episode_id, rb.asset_id FROM revision_bindings rb LIMIT 1"
        ).fetchone()

    class Process:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    captured = []
    monkeypatch.setattr(
        "app.media_v4.playback.session.start_mpv",
        lambda strm_path, **kwargs: captured.append((strm_path, kwargs)) or Process(),
    )
    monkeypatch.setattr("app.media_v4.playback.session.threading.Thread.start", lambda _self: None)
    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")

    session = manager.play(row["work_id"], row["episode_id"], row["asset_id"])

    assert session["status"] == "playing"
    assert session["pid"] == 123
    assert (tmp_path / "sessions" / f"{row['asset_id']}.strm").read_text(encoding="utf-8") == str(media)
    assert captured[0][1]["display_title"] == "Show - S01E01"


def test_default_asset_selection_prefers_available_local_then_higher_quality(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.revisions.service import V4RevisionService

    entries = []
    for evidence_id, quality in (("low", "1080p"), ("high", "2160p")):
        evidence = SourceEvidence(
            evidence_id=evidence_id,
            scan_id="scan-quality",
            root_id="root-quality",
            source_key=evidence_id,
            relative_path=f"Show/Show.S01E01.{quality}.mkv",
            entry_kind="video",
            provider="local",
            source_locator=str(tmp_path / f"{evidence_id}.mkv"),
            playback_locator=str(tmp_path / f"{evidence_id}.mkv"),
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{evidence_id}",
            evidence_id=evidence_id,
            parser_version="fixture",
            work_title="Show",
            title_candidates=("Show",),
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=1,
            quality_tags=(quality,),
        )
        entries.append((evidence, facts))
    database = V4Database(tmp_path / "quality.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-quality", entries)
    revisions.confirm("rev-quality")
    with database.connect() as conn:
        binding = conn.execute(
            "SELECT work_id, episode_id FROM revision_bindings LIMIT 1"
        ).fetchone()

    selected = V4PlaybackManager(database, session_dir=tmp_path / "sessions")._resolve_asset(
        binding["work_id"], binding["episode_id"], ""
    )

    assert selected["resolution"] == "2160p"
