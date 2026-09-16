"""B5-2b 续扫与暂停取消的 API 合同（回归：此前续扫 100% 500、暂停态无法取消）。

背景：paused 续扫原先复用 scan_id 之后**又走了一遍** `register_source_scan`，而它对
已存在的 scan_id 直接抛 `ValueError("扫描任务已存在")`；API 未捕获 → 500，用户点
「继续扫描」必失败；同时 `cancel` 端点只接受 running/queued，paused 既不能续、也不能
取消，来源卡永久停在"可继续扫描"。

这里锁住正确的行为：续扫**原地回置**同一行（不重复登记、保留 generation 与 frontier
断点），暂停态可被直接取消并清理断点。
"""

from __future__ import annotations

from types import SimpleNamespace


def _setup(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4, openlist_v4
    from app.integrations.openlist.providers import OpenListRouteConfig
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "resume.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(openlist_v4, "_client", lambda _config: object())
    monkeypatch.setattr(media_v4, "scan_openlist_directory", lambda _client, **kwargs: (kwargs["scan_id"], []))
    # 只验证登记与状态语义：不让后台执行器真的跑起来。
    monkeypatch.setattr(
        "app.media_v4.sources.source_scan_runner.get_source_scan_runner",
        lambda _database: SimpleNamespace(wake=lambda: None),
    )
    application = FastAPI()
    application.include_router(media_v4.router)
    return TestClient(application), database, media_v4


def _paused_scan(database, media_v4, *, scan_id: str = "scan-paused") -> tuple[str, str]:
    from app.api.media_v4 import openlist_root_id
    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.source_scan_runner import register_source_scan

    root_id = openlist_root_id("https://openlist.example.test", "kumi", "/Anime")
    with database.connect() as conn:
        # source_scans.root_id 有外键约束：登记扫描前来源必须已存在。
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, "
            "created_at, updated_at) VALUES (?, 'pan115', 'openlist_scan', '/Anime', 'X:\\OpenList\\Anime', 'now', 'now')",
            (root_id,),
        )
    register_source_scan(
        database,
        scan_id=scan_id,
        root_id=root_id,
        scan_kind="openlist_full",
        source_mode="openlist_full",
        request={"remote_root": "/Anime"},
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_scans SET status = 'paused', stage = 'paused', error = '本次巡检已达请求预算' "
            "WHERE scan_id = ?",
            (scan_id,),
        )
    scan_frontier.ensure_directories(database, scan_id=scan_id, remote_paths=["/Anime", "/Anime/Show"], depth=0)
    scan_frontier.mark_directory(database, scan_id=scan_id, remote_path="/Anime", status="completed")
    return scan_id, root_id


def _resume(client, scan_id: str, *, root_path: str = "/Anime"):
    return client.post("/api/v4/sources/scans", json={
        "source": "openlist",
        "root_path": root_path,
        "provider": "pan115",
        "scan_mode": "full",
        "resume_scan_id": scan_id,
    })


def test_resuming_a_paused_scan_requeues_the_same_row_and_keeps_the_frontier(tmp_path, monkeypatch):
    from app.media_v4.sources import scan_frontier

    client, database, media_v4 = _setup(tmp_path, monkeypatch)
    scan_id, root_id = _paused_scan(database, media_v4)

    response = _resume(client, scan_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan_id"] == scan_id, "续扫必须复用同一 scan_id，否则 frontier 断点无效"
    assert body["status"] == "queued"

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT status, stage, error, generation FROM source_scans WHERE scan_id = ?", (scan_id,)
        ).fetchall()
    assert len(rows) == 1, "续扫不能重复登记同一 scan_id（这正是此前 500 的原因）"
    assert str(rows[0]["status"]) == "queued"
    assert str(rows[0]["stage"]) == "queued"
    assert str(rows[0]["error"]) == ""
    assert int(rows[0]["generation"]) == 1, "续扫不是新代次"

    # 断点必须原样保留：已完成的根目录不会被重列。
    assert scan_frontier.directory_counts(database, scan_id=scan_id) == {
        "total": 2, "completed": 1, "pending": 1,
    }


def test_resume_rejects_a_scan_that_is_not_paused(tmp_path, monkeypatch):
    client, database, media_v4 = _setup(tmp_path, monkeypatch)
    scan_id, _root_id = _paused_scan(database, media_v4)
    with database.connect() as conn:
        conn.execute("UPDATE source_scans SET status = 'completed' WHERE scan_id = ?", (scan_id,))

    response = _resume(client, scan_id)

    assert response.status_code == 409
    assert "不可继续" in response.json()["detail"]


def test_paused_scan_can_be_cancelled_and_clears_the_frontier(tmp_path, monkeypatch):
    from app.media_v4.sources import scan_frontier

    client, database, media_v4 = _setup(tmp_path, monkeypatch)
    scan_id, _root_id = _paused_scan(database, media_v4)

    response = client.post(f"/api/v4/sources/scans/{scan_id}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    with database.connect() as conn:
        row = conn.execute("SELECT status, error FROM source_scans WHERE scan_id = ?", (scan_id,)).fetchone()
    assert str(row["status"]) == "cancelled"
    assert "取消" in str(row["error"])
    # 用户明确放弃巡检：断点行必须清掉，否则来源卡会一直显示"可继续扫描"。
    assert scan_frontier.directory_counts(database, scan_id=scan_id)["total"] == 0
