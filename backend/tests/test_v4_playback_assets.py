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


def test_playback_title_prefers_scraped_chinese_title_and_keeps_local_season_number(tmp_path):
    import json

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.revisions.service import V4RevisionService

    media = tmp_path / "Yuru.Camp.S02E01.mkv"
    media.write_bytes(b"fixture")
    evidence = SourceEvidence(
        evidence_id="ev-play-s2",
        scan_id="scan-play-s2",
        root_id="root-play-s2",
        source_key=media.name,
        relative_path=media.name,
        entry_kind="video",
        provider="local",
        source_locator=str(media),
        playback_locator=str(media),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-play-s2",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Yuru Camp",
        title_candidates=("Yuru Camp",),
        media_type="tv",
        group_type="season",
        season_candidate=2,
        episode_candidate=1,
    )
    database = V4Database(tmp_path / "play-title.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-play-s2", [(evidence, facts)])
    revisions.confirm("rev-play-s2")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT rb.work_id, rb.episode_id, rb.asset_id FROM revision_bindings rb LIMIT 1"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, status, created_at, updated_at
            ) VALUES (?, 'rev-play-s2', ?, 'tmdb', '76075', ?, 'confirmed', 'now', 'now')
            """,
            ("binding-play-s2", row["work_id"], json.dumps({"title": "摇曳露营△"}, ensure_ascii=False)),
        )

    asset = V4PlaybackManager(database, session_dir=tmp_path / "sessions")._resolve_asset(
        row["work_id"], row["episode_id"], row["asset_id"]
    )

    assert V4PlaybackManager._display_title(asset) == "摇曳露营△ - S02E01"


def test_playback_title_appends_local_episode_title_without_pseudo_names(tmp_path):
    """窗口标题 = 中文作品名 + 唯一季集代码 + 本地集标题；空标题不追加。"""

    import json

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.revisions.service import V4RevisionService

    media = tmp_path / "Yuru.Camp.S02E01.Winter.Water.mkv"
    media.write_bytes(b"fixture")
    evidence = SourceEvidence(
        evidence_id="ev-play-title",
        scan_id="scan-play-title",
        root_id="root-play-title",
        source_key=media.name,
        relative_path=media.name,
        entry_kind="video",
        provider="local",
        source_locator=str(media),
        playback_locator=str(media),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-play-title",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Yuru Camp",
        title_candidates=("Yuru Camp",),
        media_type="tv",
        group_type="season",
        season_candidate=2,
        episode_candidate=1,
        episode_title="冬日的泉水",
    )
    database = V4Database(tmp_path / "play-episode-title.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-play-title", [(evidence, facts)])
    revisions.confirm("rev-play-title")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT rb.work_id, rb.episode_id, rb.asset_id FROM revision_bindings rb LIMIT 1"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, status, created_at, updated_at
            ) VALUES (?, 'rev-play-title', ?, 'tmdb', '76075', ?, 'confirmed', 'now', 'now')
            """,
            ("binding-play-title", row["work_id"], json.dumps({"title": "摇曳露营△"}, ensure_ascii=False)),
        )

    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")
    asset = manager._resolve_asset(row["work_id"], row["episode_id"], row["asset_id"])

    assert V4PlaybackManager._display_title(asset) == "摇曳露营△ - S02E01 冬日的泉水"

    # 空集标题不追加伪标题（如 "第 1 集"/"特别篇"）。
    asset_without_title = dict(asset)
    asset_without_title["display_title"] = ""
    assert V4PlaybackManager._display_title(asset_without_title) == "摇曳露营△ - S02E01"
    generic = dict(asset)
    generic["display_title"] = "特别篇"
    assert V4PlaybackManager._display_title(generic) == "摇曳露营△ - S02E01"

    # 特别篇：内部结构码不进入用户可见的窗口标题，语义标题仍可区分。
    special = dict(asset)
    special.update({"episode_kind": "special", "special_number": 8, "local_season_number": 0})
    special["display_title"] = "Making Documentary"
    assert V4PlaybackManager._display_title(special) == "摇曳露营△ - Making Documentary"

    # 历史数据：display_title 仍带 SP 前缀时也必须剥离内部码。
    legacy = dict(special)
    legacy["display_title"] = "SP08 - Making Documentary"
    assert V4PlaybackManager._display_title(legacy) == "摇曳露营△ - Making Documentary"

    # 无法刮削语义标题时，使用可区分的中文兜底，不泄漏 SP08。
    special_without_title = dict(special)
    special_without_title["display_title"] = "特别篇"
    assert V4PlaybackManager._display_title(special_without_title) == "摇曳露营△ - 特别篇 8"


def test_playback_title_uses_a_confirmed_mapping_for_the_active_episode(tmp_path):
    """较新的部分刮削不能遮蔽当前剧集已有的完整标题。"""

    import json

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.revisions.service import V4RevisionService

    media = tmp_path / "Show.S01E04.mkv"
    media.write_bytes(b"fixture")
    evidence = SourceEvidence(
        evidence_id="ev-play-mapping-history",
        scan_id="scan-play-mapping-history",
        root_id="root-play-mapping-history",
        source_key=media.name,
        relative_path=media.name,
        entry_kind="video",
        provider="local",
        source_locator=str(media),
        playback_locator=str(media),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-play-mapping-history",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=4,
        episode_title="本地集标题",
    )
    database = V4Database(tmp_path / "play-mapping-history.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-play-mapping-history", [(evidence, facts)])
    revisions.confirm("rev-play-mapping-history")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT rb.work_id, rb.episode_id FROM revision_bindings rb LIMIT 1"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, status, created_at, updated_at
            ) VALUES (?, 'rev-play-mapping-history', ?, 'tmdb', '1', ?, 'confirmed', '2026-01-01', '2026-01-01')
            """,
            (
                "binding-play-mapping-history",
                row["work_id"],
                json.dumps({
                    "title": "Show",
                    "episode_mappings": [{
                        "episode_id": row["episode_id"],
                        "title": "刮削后的完整集标题",
                    }],
                }, ensure_ascii=False),
            ),
        )
        conn.execute(
            """
            INSERT INTO source_roots(
                root_id, provider, ingest_method, created_at, updated_at
            ) VALUES ('root-play-mapping-partial', 'local', 'local_scan', '2026-01-02', '2026-01-02')
            """
        )
        conn.execute(
            """
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan-play-mapping-partial', 'root-play-mapping-partial', 1, 'completed')
            """
        )
        conn.execute(
            """
            INSERT INTO import_revisions(
                revision_id, root_id, scan_id, resolver_version, status, created_at, confirmed_at
            ) VALUES ('rev-play-mapping-partial', ?, ?, 'fixture', 'confirmed', '2026-01-02', '2026-01-02')
            """,
            ("root-play-mapping-partial", "scan-play-mapping-partial"),
        )
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, status, created_at, updated_at
            ) VALUES (?, 'rev-play-mapping-partial', ?, 'tmdb', '1', ?, 'confirmed', '2026-01-02', '2026-01-02')
            """,
            (
                "binding-play-mapping-partial",
                row["work_id"],
                json.dumps({
                    "title": "Show",
                    "episode_mappings": [{"episode_id": "another-episode", "title": "其他集"}],
                }, ensure_ascii=False),
            ),
        )

    asset = V4PlaybackManager(database, session_dir=tmp_path / "sessions")._resolve_asset(
        row["work_id"], row["episode_id"], ""
    )

    assert V4PlaybackManager._display_title(asset) == "Show - S01E04 刮削后的完整集标题"


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
