"""P-006 跨页面 read model 与详情命令迁移合同。"""

from __future__ import annotations

import base64
import json


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "p006.db")
    database.initialize()
    return database


def _seed_work(database, *, work_id, title, provider="pan115", season_count=2, episode_numbers=(1, 2, 3)):
    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, created_at, updated_at) "
            "VALUES ('root-p', ?, 'openlist_scan', '/Anime', 'K:\\\\Anime', 'now', 'now')",
            (provider,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO source_scans(scan_id, root_id, generation, status) VALUES ('scan-p', 'root-p', 1, 'completed')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at, confirmed_at) "
            "VALUES ('rev-p', 'root-p', 'scan-p', 'v4', 'confirmed', '', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO works(work_id, identity_key, work_type, preferred_title, show_type, card_type, created_at, updated_at) "
            "VALUES (?, ?, 'series', ?, 'anime_series', 'main_series', 'now', 'now')",
            (work_id, f"ik-{work_id}", title),
        )
        for season in range(1, season_count + 1):
            conn.execute(
                "INSERT INTO seasons(season_id, work_id, local_season_number, season_kind) VALUES (?, ?, ?, 'regular')",
                (f"season-{work_id}-{season}", work_id, season),
            )
            for episode in episode_numbers:
                conn.execute(
                    "INSERT INTO episodes(episode_id, work_id, season_id, local_episode_number, episode_kind) "
                    "VALUES (?, ?, ?, ?, 'regular')",
                    (f"ep-{work_id}-{season}-{episode}", work_id, f"season-{work_id}-{season}", episode),
                )
                evidence_id = f"ev-{work_id}-{season}-{episode}"
                conn.execute(
                    "INSERT INTO source_evidence(evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind, source_locator, playback_locator, ingest_method, observed_at) "
                    "VALUES (?, 'scan-p', 'root-p', ?, ?, ?, 'video', ?, ?, 'openlist_api', 'now')",
                    (evidence_id, provider, f"rel-{work_id}-{season}-{episode}", f"rel-{work_id}-{season}-{episode}", f"K:\\\\Anime\\\\{work_id}\\\\S{season:02d}E{episode:02d}.mkv", f"K:\\\\Anime\\\\{work_id}\\\\S{season:02d}E{episode:02d}.mkv"),
                )
                asset_id = f"asset-{work_id}-{season}-{episode}"
                conn.execute(
                    "INSERT INTO assets(asset_id, evidence_id, root_id, source_locator, playback_locator) "
                    "VALUES (?, ?, 'root-p', ?, ?)",
                    (asset_id, evidence_id, f"K:\Anime\{work_id}\S{season:02d}E{episode:02d}.mkv", f"K:\Anime\{work_id}\S{season:02d}E{episode:02d}.mkv"),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO parsed_facts(parsed_fact_id, evidence_id, parser_version, work_title, title_candidates_json, media_type, group_type) "
                    "VALUES (?, ?, 'fixture', 'x', '[]', 'tv', 'season')",
                    (f"facts-{evidence_id}", evidence_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO revision_evidence(revision_id, evidence_id, parsed_fact_id) "
                    "VALUES ('rev-p', ?, ?)",
                    (evidence_id, f"facts-{evidence_id}"),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO revision_bindings(binding_id, revision_id, evidence_id, work_id, episode_id, asset_id, confidence) "
                    "VALUES (?, 'rev-p', ?, ?, ?, ?, 'high')",
                    (f"binding-{work_id}-{season}-{episode}", evidence_id, work_id, f"ep-{work_id}-{season}-{episode}", asset_id),
                )


def test_library_list_cards_include_watch_status_and_latest_episode(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import library_v4, media_v4

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(library_v4, "get_database", lambda: database)
    _seed_work(database, work_id="w1", title="追更作品")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO tracking_states(work_id, provider, provider_id, last_watched_episode, metadata_json, updated_at) "
            "VALUES ('w1', 'local', '', 3, ?, 'now')",
            (json.dumps({"status": "watching", "favorite": True}, ensure_ascii=False),),
        )

    application = FastAPI()
    application.include_router(library_v4.router)
    application.include_router(media_v4.router)
    client = TestClient(application)
    response = client.get("/api/library?include_all=true")
    assert response.status_code == 200, response.text
    work = next(item for item in response.json()["works"] if item["work_id"] == "w1")
    assert work["watch_status"]["favorite"] is True
    assert work["watch_status"]["status"] == "watching"
    # 两季、每季到第 3 集 → latest_episode_number = 3
    assert work["latest_episode_number"] == 3
    assert work["season_count"] == 2


def test_playback_history_is_event_based_and_respects_limit(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4, playback_v4
    from app.media_v4.playback.store import V4PlaybackStore

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(playback_v4, "get_database", lambda: database)
    _seed_work(database, work_id="w1", title="历史作品")
    store = V4PlaybackStore(database)
    store.record_activation("w1", "ep-w1-1-1", "asset-w1-1-1")
    for index in range(5):
        store.save_progress("w1", "ep-w1-1-1", "asset-w1-1-1", position=10 + index, duration=100, completed=False)

    application = FastAPI()
    application.include_router(playback_v4.router)
    client = TestClient(application)
    limited = client.get("/api/playback/history?limit=3")
    assert limited.status_code == 200, limited.text
    items = limited.json()["items"]
    assert len(items) == 1  # 同一集的多个进度心跳不得重复写入历史
    assert items[0]["title_snapshot"] == "历史作品"  # 事件含标题快照
    # 进度仍是单条 upsert
    progress = client.get("/api/playback/progress").json()["items"]
    assert len(progress) == 1


def test_detail_title_and_artwork_and_folder_commands(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    _seed_work(database, work_id="w1", title="原始标题")

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    # 标题覆盖层
    title = client.patch("/api/v4/works/w1/title", json={"title": "用户标题"})
    assert title.status_code == 200, title.text
    restored = client.delete("/api/v4/works/w1/title")
    assert restored.status_code == 200

    # 图片覆盖层（受控镜像根内）
    png = base64.b64encode(b"fake-png-bytes").decode()
    artwork = client.post("/api/v4/works/w1/artwork", json={"kind": "poster", "data_base64": png})
    assert artwork.status_code == 200, artwork.text
    assert "artwork" in artwork.json()["path"]
    restore_art = client.delete("/api/v4/works/w1/artwork/poster")
    assert restore_art.status_code == 200

    # 打开目录（基于已确认 Asset 播放路径）
    folder = client.get("/api/v4/works/w1/folder")
    assert folder.status_code == 200, folder.text
    assert folder.json()["folder"].endswith("w1")

    # 重跑刮削 job（进入同一 V4 job 链）
    scrape = client.post("/api/v4/works/w1/scrape")
    assert scrape.status_code == 200, scrape.text
    assert scrape.json()["status"] == "queued"
    with database.connect() as conn:
        job = conn.execute("SELECT job_type FROM jobs WHERE work_id = 'w1' AND job_type = 'scrape_work'").fetchone()
    assert job is not None


def _seed_tracking_work(database, *, work_id, title, status="watching", last_watched=2, latest=3):
    _seed_work(database, work_id=work_id, title=title)
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO tracking_states(work_id, provider, provider_id, last_watched_episode, metadata_json, updated_at) "
            "VALUES (?, 'local', '', ?, ?, 'now')",
            (work_id, last_watched, json.dumps({"status": status, "favorite": False}, ensure_ascii=False)),
        )


def test_tracking_works_lists_seasonal_with_latest_episode(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4, tracking_v4

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(tracking_v4, "get_database", lambda: database)
    _seed_tracking_work(database, work_id="w1", title="追更作品", status="watching", last_watched=2, latest=3)
    _seed_tracking_work(database, work_id="w2", title="搁置作品", status="on_hold")
    _seed_work(database, work_id="w3", title="未追更")

    application = FastAPI()
    application.include_router(tracking_v4.router)
    client = TestClient(application)
    response = client.get("/api/v4/tracking/works")
    assert response.status_code == 200, response.text
    works = response.json()["works"]
    by_id = {item["work_id"]: item for item in works}
    assert set(by_id) == {"w1", "w2"}  # 未追更不出现
    assert by_id["w1"]["status"] == "watching"
    assert by_id["w1"]["latest_episode_number"] == 3
    assert by_id["w2"]["status"] == "on_hold"


def test_tracking_scan_all_enqueues_incremental_for_openlist_roots(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4, tracking_v4
    from app.integrations.openlist.providers import OpenListRouteConfig

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(tracking_v4, "get_database", lambda: database)
    _seed_work(database, work_id="w1", title="追更作品")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO tracking_states(work_id, provider, provider_id, last_watched_episode, metadata_json, updated_at) "
            "VALUES ('w1', 'local', '', 1, ?, 'now')",
            (json.dumps({"status": "watching"}, ensure_ascii=False),),
        )
        # 该作品来源根改为 OpenList 路径
        conn.execute(
            "UPDATE source_roots SET source_locator = '/Anime', provider = 'pan115' WHERE root_id = 'root-p'"
        )
    from types import SimpleNamespace

    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\OpenList",
        openlist_routes=[OpenListRouteConfig(route_id="route-anime", remote_prefix="/Anime", provider_id="pan115")],
    )
    monkeypatch.setattr(tracking_v4, "load_config", lambda: config)
    monkeypatch.setattr(tracking_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    from app.api import openlist_v4

    monkeypatch.setattr(openlist_v4, "_client", lambda _config: object())
    monkeypatch.setattr(media_v4, "_configured_mirror_root", lambda: None)

    application = FastAPI()
    application.include_router(tracking_v4.router)
    client = TestClient(application)
    response = client.post("/api/v4/tracking/scan-all", json={"include_scrape": True})
    assert response.status_code == 200, response.text
    tasks = response.json()["tasks"]
    assert tasks, response.text
    assert tasks[0]["status"] == "running"
    assert tasks[0]["remote_root"] == "/Anime"


def test_work_delete_preview_and_confirm_hides_work(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import library_v4, media_v4

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(media_v4, "_configured_mirror_root", lambda: None)
    monkeypatch.setattr(library_v4, "get_database", lambda: database)
    _seed_work(database, work_id="w1", title="将被删除")
    _seed_work(database, work_id="w2", title="保留作品")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO playback_progress(episode_id, asset_id, work_id, position, duration, updated_at) VALUES ('ep-x', 'as-x', 'w1', 1, 10, 'now')"
        )

    application = FastAPI()
    application.include_router(media_v4.router)
    application.include_router(library_v4.router)
    client = TestClient(application)

    preview = client.post("/api/v4/works/w1/delete-preview", json={})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["playback_count"] == 1

    confirm = client.post("/api/v4/works/w1/delete-confirm", json={"preview_id": body["preview_id"], "digest": body["digest"]})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "completed"

    # 作品从列表退出，但不可变快照保留
    works = client.get("/api/library?include_all=true").json()["works"]
    assert all(work["work_id"] != "w1" for work in works)
    assert any(work["work_id"] == "w2" for work in works)
    with database.connect() as conn:
        hidden = conn.execute("SELECT override_json FROM work_overrides WHERE work_id = 'w1'").fetchone()
        import json as _json
        assert _json.loads(hidden["override_json"]).get("hidden") is True
        playback = conn.execute("SELECT 1 FROM playback_progress WHERE work_id = 'w1'").fetchone()
    assert playback is None

    # digest 不一致拒绝
    stale = client.post("/api/v4/works/w1/delete-confirm", json={"preview_id": "x", "digest": "0" * 64})
    assert stale.status_code == 409
