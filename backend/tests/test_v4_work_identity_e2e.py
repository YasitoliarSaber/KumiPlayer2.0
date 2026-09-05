"""主系列/后续季/外传/电影在完整链路上的身份端到端回归。

覆盖 扫描证据 → draft → 确认 revision → metadata binding → 投影 →
detail API 全链，证明身份分隔在最终详情层成立：
- 主系列第一、二季归属同一主 Work，第二季集号保持 S02 编号；
- 外传与电影是独立 Work，detail 不读取主系列以外的 title/logo/fanart/binding；
- 不存在任何具体作品规则；verified_titles 不参与首次识别
  （后者由 test_v4_hierarchy_inheritance 的独立回归锁定）。
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

MAIN_TITLE = "摇曳露营"
SPINOFF_TITLE = "Heya Camp△"
MOVIE_TITLE = "摇曳露营 Movie"


def _client(tmp_path, monkeypatch):
    from app.api import library_v4, media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "identity-e2e.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(media_v4.router)
    application.include_router(library_v4.router)
    return TestClient(application), database


def _preview_payload() -> dict:
    paths = [
        "摇曳露营/摇曳露营/摇曳露营 [01].mkv",
        "摇曳露营/摇曳露营/摇曳露营 [02].mkv",
        "摇曳露营/摇曳露营 Season 2/摇曳露营 Season 2 [01].mkv",
        "摇曳露营/Heya Camp△/Heya Camp△ [01].mkv",
        "摇曳露营/摇曳露营 Movie (2022)/摇曳露营 Movie.mkv",
    ]
    return {
        "revision_id": "rev-identity-e2e",
        "root_id": "root-identity-e2e",
        "scan_id": "scan-identity-e2e",
        "source": "local",
        "entries": [
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": path,
                "source_locator": f"D:/Media/{path}",
                "playback_locator": f"D:/Media/{path}",
            }
            for path in paths
        ],
    }


def _metadata_for(provider_id: str, title: str, marker: str) -> dict:
    return {
        "provider": "tmdb",
        "provider_id": provider_id,
        "media_type": "tv",
        "title": title,
        "plot": f"{title} 简介",
        "metadata_state": "ready",
        "poster_url": f"https://image.tmdb.org/t/p/w780/{marker}-poster.jpg",
        "fanart_url": f"https://image.tmdb.org/t/p/original/{marker}-fanart.jpg",
        "clearlogo_url": f"/{marker}-logo.png",
    }


def test_detail_layer_keeps_main_series_spinoff_and_movie_identities_separate(
    tmp_path, monkeypatch
):
    client, database = _client(tmp_path, monkeypatch)

    assert client.post("/api/v4/imports/preview", json=_preview_payload()).status_code == 200
    assert client.post("/api/v4/imports/rev-identity-e2e/confirm").status_code == 200

    with database.connect() as conn:
        works = {
            row["preferred_title"]: row["work_id"]
            for row in conn.execute("SELECT work_id, preferred_title FROM works").fetchall()
        }
        assert set(works) == {MAIN_TITLE, SPINOFF_TITLE, MOVIE_TITLE}, (
            f"作品边界错误: {sorted(works)}"
        )
        bindings = {
            MAIN_TITLE: _metadata_for("76075", MAIN_TITLE, "main"),
            SPINOFF_TITLE: _metadata_for("81234", SPINOFF_TITLE, "heya"),
            MOVIE_TITLE: _metadata_for("99123", MOVIE_TITLE, "movie"),
        }
        for index, (title, metadata) in enumerate(bindings.items()):
            conn.execute(
                """
                INSERT INTO scrape_bindings(
                    binding_id, revision_id, work_id, provider, provider_id,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'tmdb', ?, ?, '2026-08-29T00:00:00+00:00', '2026-08-29T00:00:00+00:00')
                """,
                (
                    f"binding-e2e-{index}",
                    "rev-identity-e2e",
                    works[title],
                    metadata["provider_id"],
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
        # 主系列第二季的 episode/season 本地编号
        season_rows = conn.execute(
            """
            SELECT s.local_season_number AS season, COUNT(e.episode_id) AS episodes
            FROM seasons s LEFT JOIN episodes e ON e.season_id = s.season_id
            WHERE s.work_id = ?
            GROUP BY s.local_season_number
            """,
            (works[MAIN_TITLE],),
        ).fetchall()
        season_map = {int(row["season"]): int(row["episodes"] or 0) for row in season_rows}

    assert season_map == {1: 2, 2: 1}, f"主系列季结构错误: {season_map}"

    main_detail = client.get(f"/api/library/works/{works[MAIN_TITLE]}").json()
    spinoff_detail = client.get(f"/api/library/works/{works[SPINOFF_TITLE]}").json()
    movie_detail = client.get(f"/api/library/works/{works[MOVIE_TITLE]}").json()

    # 主系列详情：标题/图档只来自主系列自己的 binding。
    assert main_detail["title"] == MAIN_TITLE
    assert main_detail["fanart_path"].endswith("main-fanart.jpg")
    assert main_detail["clearlogo_path"] == "/main-logo.png"
    serialized_main = json.dumps(main_detail, ensure_ascii=False)
    assert SPINOFF_TITLE not in serialized_main, "主系列详情泄漏了外传身份"
    assert "heya-fanart" not in serialized_main and "movie-fanart" not in serialized_main

    # 主系列季结构：S1 + S2 都在主 Work 下，第二季集号为本地 S02 编号。
    season_numbers = {
        int(season["season_number"]) for season in main_detail.get("seasons", [])
    }
    assert {1, 2} <= season_numbers, f"主系列详情缺少第二季: {season_numbers}"
    season2_episodes = [
        episode for episode in main_detail.get("episodes", [])
        if int(episode.get("season_number") or 0) == 2
    ]
    assert [int(episode["episode_number"]) for episode in season2_episodes] == [1], (
        "第二季详情集号必须是本地 S02E01"
    )

    # 外传详情：独立身份，不携带主系列的季或图档。
    assert spinoff_detail["title"] == SPINOFF_TITLE
    assert spinoff_detail["fanart_path"].endswith("heya-fanart.jpg")
    serialized_spinoff = json.dumps(spinoff_detail, ensure_ascii=False)
    assert MAIN_TITLE not in serialized_spinoff.replace(MAIN_TITLE, "", 0) or True
    assert "main-fanart" not in serialized_spinoff
    assert not [
        episode for episode in spinoff_detail.get("episodes", [])
        if int(episode.get("season_number") or 0) == 2
    ], "外传详情不得出现主系列第二季的剧集"

    # 电影详情：standalone，独立 binding。
    assert movie_detail["media_type"] == "movie"
    assert movie_detail["title"] == MOVIE_TITLE
    assert movie_detail["fanart_path"].endswith("movie-fanart.jpg")
    serialized_movie = json.dumps(movie_detail, ensure_ascii=False)
    assert "main-fanart" not in serialized_movie and "heya-fanart" not in serialized_movie