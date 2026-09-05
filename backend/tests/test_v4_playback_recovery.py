"""V4 播放闭环：受控连播、刮削标题与历史语义。"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace


def _confirmed_three_episode_work(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "playback-recovery.db")
    database.initialize()
    entries = []
    for number in (1, 2, 3):
        media = tmp_path / f"Yuru.Camp.S01E{number:02d}.mkv"
        media.write_bytes(b"fixture")
        evidence = SourceEvidence(
            evidence_id=f"ev-{number}", scan_id="scan-playback", root_id="root-playback",
            source_key=media.name, relative_path=media.name, entry_kind="video", provider="local",
            source_locator=str(media), playback_locator=str(media),
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{number}", evidence_id=evidence.evidence_id,
            parser_version="fixture", work_title="Yuru Camp", title_candidates=("Yuru Camp",),
            media_type="tv", group_type="season", season_candidate=1, episode_candidate=number,
        )
        entries.append((evidence, facts))
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-playback", entries)
    revisions.confirm("rev-playback")
    return database


def test_regular_episode_queue_contains_only_following_regular_episodes(tmp_path, monkeypatch):
    from app.media_v4.playback import session as session_module
    from app.media_v4.playback.session import V4PlaybackManager

    database = _confirmed_three_episode_work(tmp_path)
    with database.connect() as conn:
        first = conn.execute(
            """
            SELECT rb.work_id, rb.episode_id, rb.asset_id
            FROM revision_bindings rb JOIN episodes e ON e.episode_id = rb.episode_id
            ORDER BY e.local_episode_number LIMIT 1
            """
        ).fetchone()
    monkeypatch.setattr(session_module, "load_config", lambda: SimpleNamespace(auto_play_next_episode=True))
    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")
    current = manager._resolve_asset(first["work_id"], first["episode_id"], first["asset_id"])

    queue = manager._build_playlist(first["work_id"], current)

    assert [item["local_episode_number"] for item in queue] == [1, 2, 3]
    assert [item["episode_kind"] for item in queue] == ["regular", "regular", "regular"]


def test_scraped_episode_title_overrides_local_filename_title(tmp_path):
    from app.media_v4.playback.session import V4PlaybackManager

    database = _confirmed_three_episode_work(tmp_path)
    with database.connect() as conn:
        first = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings ORDER BY episode_id LIMIT 1"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, status, created_at, updated_at
            ) VALUES (?, 'rev-playback', ?, 'tmdb', '76075', ?, 'confirmed', 'now', 'now')
            """,
            (
                "scrape-playback-title", first["work_id"], json.dumps({
                    "title": "摇曳露营△",
                    "episode_mappings": [{
                        "episode_id": first["episode_id"], "title": "野外烹饪与湖畔的夜晚",
                    }],
                }, ensure_ascii=False),
            ),
        )
    asset = V4PlaybackManager(database, session_dir=tmp_path / "sessions")._resolve_asset(
        first["work_id"], first["episode_id"], first["asset_id"]
    )

    assert V4PlaybackManager._display_title(asset) == (
        f"摇曳露营△ - S01E{int(asset['local_episode_number']):02d} 野外烹饪与湖畔的夜晚"
    )


def test_history_is_written_once_per_activation_not_per_progress_heartbeat(tmp_path):
    from app.media_v4.playback.store import V4PlaybackStore

    database = _confirmed_three_episode_work(tmp_path)
    with database.connect() as conn:
        first = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings ORDER BY episode_id LIMIT 1"
        ).fetchone()
    store = V4PlaybackStore(database)
    store.record_activation(first["work_id"], first["episode_id"], first["asset_id"])
    for position in (10, 20, 30):
        store.save_progress(first["work_id"], first["episode_id"], first["asset_id"], position, 100, False)
    with database.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM playback_history").fetchone()[0]
    assert count == 1


def test_completion_sync_reuses_the_v4_bangumi_endpoint(tmp_path, monkeypatch):
    from app.api import bangumi
    from app.media_v4.playback.session import V4PlaybackManager

    database = _confirmed_three_episode_work(tmp_path)
    captured = {}

    def sync_watched(sync_database, episode_id, payload):
        captured["database"] = sync_database
        captured["episode_id"] = episode_id
        captured["work_id"] = payload.work_id
        captured["season_number"] = payload.season_number

    monkeypatch.setattr(bangumi, "sync_completed_episode", sync_watched)

    V4PlaybackManager(database, session_dir=tmp_path / "sessions")._sync_completed_episode("work-1", "episode-2", 2)

    assert captured == {
        "database": database,
        "episode_id": "episode-2",
        "work_id": "work-1",
        "season_number": 2,
    }


def test_completion_sync_uses_the_playback_database_not_the_api_global_database(tmp_path, monkeypatch):
    """播放会话可使用非全局 V4Database；Bangumi 成功记录必须写回同一库。"""

    from app.api import bangumi
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.session import V4PlaybackManager

    database = _confirmed_three_episode_work(tmp_path)
    unrelated_database = V4Database(tmp_path / "unrelated-global.db")
    unrelated_database.initialize()
    with database.connect() as conn:
        row = conn.execute(
            "SELECT work_id, episode_id FROM revision_bindings ORDER BY episode_id LIMIT 1"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO bangumi_matches(
                work_id, season_number, subject_id, subject_name, subject_name_cn,
                episode_map_json, created_at, updated_at
            ) VALUES (?, 1, 100, 'Yuru Camp', '摇曳露营△', ?, 'now', 'now')
            """,
            (row["work_id"], json.dumps({str(row["episode_id"]): 9001})),
        )

    class FakeBangumiClient:
        updated: list[tuple[int, int]] = []

        def __init__(self, *args, **kwargs):
            pass

        def set_episode_collection(self, episode_id, collection_type):
            self.updated.append((episode_id, collection_type))
            return {"ok": True}

    monkeypatch.setattr(bangumi, "BangumiClient", FakeBangumiClient)
    monkeypatch.setattr(bangumi, "get_database", lambda: unrelated_database)

    V4PlaybackManager(database, session_dir=tmp_path / "sessions")._sync_completed_episode(
        str(row["work_id"]), str(row["episode_id"]), 1,
    )

    assert FakeBangumiClient.updated == [(9001, 2)]
    with database.connect() as conn:
        synced = conn.execute("SELECT status FROM bangumi_episode_sync").fetchone()
    with unrelated_database.connect() as conn:
        unrelated_sync_count = conn.execute("SELECT COUNT(*) FROM bangumi_episode_sync").fetchone()[0]
    assert synced["status"] == "succeeded"
    assert unrelated_sync_count == 0


def test_completion_sync_retries_transient_remote_failures(tmp_path, monkeypatch):
    """瞬时网络失败不能让一次已完成剧集永久失去自动同步机会。"""

    from fastapi import HTTPException

    from app.api import bangumi
    from app.media_v4 import playback as playback_package
    from app.media_v4.playback.session import V4PlaybackManager

    database = _confirmed_three_episode_work(tmp_path)
    attempts: list[tuple[str, int | None]] = []
    delays: list[float] = []

    def sync_watched(_database, episode_id, payload):
        attempts.append((episode_id, payload.season_number))
        if len(attempts) < 3:
            raise HTTPException(status_code=503, detail="Bangumi temporarily unavailable")

    monkeypatch.setattr(bangumi, "sync_completed_episode", sync_watched)
    monkeypatch.setattr(playback_package.session.time, "sleep", delays.append)

    V4PlaybackManager(database, session_dir=tmp_path / "sessions")._sync_completed_episode(
        "work-1", "episode-2", 2,
    )

    assert attempts == [("episode-2", 2), ("episode-2", 2), ("episode-2", 2)]
    assert delays == [0.5, 1.0]


def test_completion_sync_can_retry_after_a_previous_transient_retry_cycle(tmp_path, monkeypatch):
    """一次临时失败耗尽即时重试后，仍须允许后续完成检查点重新投递。"""

    from fastapi import HTTPException

    from app.api import bangumi
    from app.media_v4 import playback as playback_package
    from app.media_v4.playback.session import V4PlaybackManager

    database = _confirmed_three_episode_work(tmp_path)
    attempts: list[str] = []

    def sync_watched(_database, episode_id, _payload):
        attempts.append(episode_id)
        if len(attempts) <= 3:
            raise HTTPException(status_code=503, detail="Bangumi temporarily unavailable")

    class SynchronousThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(bangumi, "sync_completed_episode", sync_watched)
    monkeypatch.setattr(playback_package.session.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(playback_package.session.threading, "Thread", SynchronousThread)
    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")
    session = {"work_id": "work-1"}
    asset = {"episode_id": "episode-2", "local_season_number": 2}

    manager._schedule_completion_sync(session, asset)
    manager._schedule_completion_sync(session, asset)

    assert attempts == ["episode-2", "episode-2", "episode-2", "episode-2"]


def test_exit_forces_the_last_completed_sample_and_sync(tmp_path, monkeypatch):
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.playback.store import V4PlaybackStore

    database = _confirmed_three_episode_work(tmp_path)
    with database.connect() as conn:
        first = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings ORDER BY episode_id LIMIT 1"
        ).fetchone()
    asset = V4PlaybackManager(database, session_dir=tmp_path / "sessions")._resolve_asset(
        first["work_id"], first["episode_id"], first["asset_id"]
    )
    saved = {}
    synced = []

    def save_progress(_self, work_id, episode_id, asset_id, position, duration, completed):
        saved.update({
            "work_id": work_id, "episode_id": episode_id, "asset_id": asset_id,
            "position": position, "duration": duration, "completed": completed,
        })

    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")
    monkeypatch.setattr(V4PlaybackStore, "save_progress", save_progress)
    monkeypatch.setattr(manager, "_schedule_completion_sync", lambda _session, item: synced.append(item["episode_id"]))

    manager._force_checkpoint({
        "work_id": first["work_id"], "episode_id": first["episode_id"],
        "playlist": [asset], "playlist_position": 0, "position": 96.0, "duration": 100.0,
    })

    assert saved == {
        "work_id": first["work_id"], "episode_id": first["episode_id"], "asset_id": first["asset_id"],
        "position": 96.0, "duration": 100.0, "completed": True,
    }
    assert synced == [first["episode_id"]]


def test_progress_event_path_must_match_a_unique_v4_playlist_asset():
    """MPV 事件不能只凭 playlist-pos 写入另一集的播放记录。"""

    from app.media_v4.playback.session import _resolve_event_playlist_position
    from app.playback.mpv_ipc import MpvProgressEvent

    playlist = [
        {"asset_id": "asset-1", "playback_locator": r"K:\Anime\S01E01.mkv"},
        {"asset_id": "asset-2", "playback_locator": r"K:\Anime\S01E02.mkv"},
    ]

    assert _resolve_event_playlist_position(
        MpvProgressEvent(12, 120, 1, media_path=r"k:/anime/S01E02.mkv"), playlist
    ) == 1
    # 有路径时它才是身份真相；即使携带了合法序号，也不能将未知文件计入第 2 集。
    assert _resolve_event_playlist_position(
        MpvProgressEvent(12, 120, 1, media_path=r"K:\Outside\other.mkv"), playlist
    ) is None
    # IPC 回退采样没有路径时，保留受控 playlist 序号的兼容行为。
    assert _resolve_event_playlist_position(MpvProgressEvent(12, 120, 1), playlist) == 1


def test_progress_monitor_retries_when_mpv_ipc_is_not_ready(tmp_path, monkeypatch):
    """MPV 首次创建管道较晚时，V4 仍必须接上后续进度事件。"""

    from app.media_v4.playback import session as session_module
    from app.media_v4.playback.session import V4PlaybackManager, _resolve_event_playlist_position
    from app.media_v4.playback.store import V4PlaybackStore
    from app.playback.mpv_ipc import MpvProgressEvent

    database = _confirmed_three_episode_work(tmp_path)
    with database.connect() as conn:
        first = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings ORDER BY episode_id LIMIT 1"
        ).fetchone()
    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")
    asset = manager._resolve_asset(first["work_id"], first["episode_id"], first["asset_id"])
    attempts = []
    saved = []

    def observe(_ipc_server):
        attempts.append("attempt")
        if len(attempts) == 1:
            raise OSError("mpv IPC is still starting")
        yield MpvProgressEvent(
            position=12.0,
            duration=120.0,
            playlist_position=0,
            media_path=asset["playback_locator"],
        )

    class Process:
        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls < 6 else 0

        def wait(self):
            return 0

    monkeypatch.setattr(session_module, "observe_mpv_progress", observe)
    monkeypatch.setattr(session_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        V4PlaybackStore,
        "save_progress",
        lambda _self, *args: saved.append(args),
    )
    session = {
        "session_id": "session-retry", "work_id": first["work_id"],
        "episode_id": first["episode_id"], "asset_id": first["asset_id"],
        "ipc_server": "ipc-retry", "position": 0.0, "duration": 0.0,
        "playlist": [asset], "playlist_position": 0,
    }
    assert _resolve_event_playlist_position(
        MpvProgressEvent(12.0, 120.0, 0, media_path=asset["playback_locator"]), [asset]
    ) == 0

    manager._monitor(Process(), session)

    assert attempts == ["attempt", "attempt"]
    assert any(call[3:5] == (12.0, 120.0) for call in saved), saved


def test_playlist_switch_forces_final_checkpoint_for_the_previous_episode(tmp_path, monkeypatch):
    """切集必须先结算旧集，不能等待可能永远不会到来的最后一个 IPC 事件。"""

    from app.media_v4.playback import session as session_module
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.playback.store import V4PlaybackStore
    from app.playback.mpv_ipc import MpvProgressEvent

    database = _confirmed_three_episode_work(tmp_path)
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings ORDER BY episode_id LIMIT 2"
        ).fetchall()
    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")
    playlist = [manager._resolve_asset(row["work_id"], row["episode_id"], row["asset_id"]) for row in rows]
    saved_episode_ids = []
    observed = 0

    def observe(_ipc_server):
        nonlocal observed
        observed += 1
        if observed == 1:
            yield MpvProgressEvent(
                position=1.0,
                duration=100.0,
                playlist_position=1,
                media_path=playlist[1]["playback_locator"],
            )

    class Process:
        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls < 4 else 0

        def wait(self):
            return 0

    monkeypatch.setattr(session_module, "observe_mpv_progress", observe)
    monkeypatch.setattr(session_module, "set_mpv_playback_title", lambda *_args: True)
    monkeypatch.setattr(session_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        V4PlaybackStore,
        "save_progress",
        lambda _self, _work_id, episode_id, *_args: saved_episode_ids.append(episode_id),
    )
    session = {
        "session_id": "session-switch", "work_id": rows[0]["work_id"],
        "episode_id": rows[0]["episode_id"], "asset_id": rows[0]["asset_id"],
        "ipc_server": "ipc-switch", "position": 95.0, "duration": 100.0,
        "playlist": playlist, "playlist_position": 0,
    }

    manager._monitor(Process(), session)

    assert saved_episode_ids[0] == rows[0]["episode_id"]


def test_progress_checkpoint_retries_a_transient_sqlite_write_failure(tmp_path, monkeypatch):
    """一次数据库锁冲突不能让仍在播放的会话丢失后续检查点。"""

    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.playback.store import V4PlaybackStore

    database = _confirmed_three_episode_work(tmp_path)
    with database.connect() as conn:
        first = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings ORDER BY episode_id LIMIT 1"
        ).fetchone()
    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")
    calls = []

    def save_progress(_self, *args):
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(V4PlaybackStore, "save_progress", save_progress)
    monkeypatch.setattr("app.media_v4.playback.session.time.sleep", lambda _seconds: None)

    assert manager._checkpoint_progress(
        {"work_id": first["work_id"]},
        first["episode_id"],
        first["asset_id"],
        12.0,
        120.0,
        False,
        attempts=2,
    )
    assert len(calls) == 2
