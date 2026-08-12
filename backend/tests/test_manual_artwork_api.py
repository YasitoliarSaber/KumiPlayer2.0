from fastapi.testclient import TestClient

from app.main import app


def test_manual_artwork_upload_is_copied_and_served(tmp_path):
    with TestClient(app) as client:
        response = client.put(
            "/api/library/works/work-art/artwork/poster",
            files={"file": ("cover.png", b"\x89PNG\r\n\x1a\nmanual", "image/png")},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["provenance"] == "manual"
        assert data["path"].endswith("poster.png")

        served = client.get("/api/assets", params={"path": data["path"]})
        assert served.status_code == 200
        assert served.content.startswith(b"\x89PNG")


def test_restore_online_artwork_removes_manual_override():
    with TestClient(app) as client:
        uploaded = client.put(
            "/api/library/works/work-restore/artwork/fanart",
            files={"file": ("backdrop.jpg", b"\xff\xd8\xffmanual", "image/jpeg")},
        ).json()

        restored = client.delete("/api/library/works/work-restore/artwork/fanart")

    assert restored.status_code == 200
    assert restored.json()["restored"] is True
    assert not __import__("pathlib").Path(uploaded["path"]).exists()


def test_manual_poster_override_syncs_compact_local_field(tmp_path):
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    save_library_index(LibraryIndex(works=[
        WorkIndex(
            work_id="work-manual-poster",
            title="手动海报作品",
            source="local",
            poster_path="https://image.tmdb.org/t/p/w780/canonical-poster.jpg",
            fanart_path="https://image.tmdb.org/t/p/original/canonical-fanart.jpg",
        ),
    ]))

    with TestClient(app) as client:
        uploaded = client.put(
            "/api/library/works/work-manual-poster/artwork/poster",
            files={"file": ("cover.png", b"\x89PNG\r\n\x1a\nmanual", "image/png")},
        ).json()
        compact = client.get("/api/library?compact=true").json()["works"][0]

    assert uploaded["path"] == compact["poster_path"]
    assert uploaded["path"] == compact["local_poster_path"]
    assert compact["artwork_provenance"]["poster"] == "manual"


def test_manual_fanart_override_syncs_compact_local_field_and_keeps_poster(tmp_path):
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    canonical_poster = "https://image.tmdb.org/t/p/w780/canonical-poster.jpg"
    save_library_index(LibraryIndex(works=[
        WorkIndex(
            work_id="work-manual-fanart",
            title="手动背景作品",
            source="local",
            poster_path=canonical_poster,
            fanart_path="https://image.tmdb.org/t/p/original/canonical-fanart.jpg",
        ),
    ]))

    with TestClient(app) as client:
        uploaded = client.put(
            "/api/library/works/work-manual-fanart/artwork/fanart",
            files={"file": ("backdrop.jpg", b"\xff\xd8\xffmanual", "image/jpeg")},
        ).json()
        compact = client.get("/api/library?compact=true").json()["works"][0]

    assert uploaded["path"] == compact["fanart_path"]
    assert uploaded["path"] == compact["local_fanart_path"]
    assert compact["poster_path"] == canonical_poster
    assert compact["artwork_provenance"]["fanart"] == "manual"
    assert compact["artwork_provenance"]["poster"] == "online"


def test_restore_online_artwork_returns_compact_to_canonical_paths(tmp_path):
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    canonical_poster = "https://image.tmdb.org/t/p/w780/canonical-poster.jpg"
    canonical_fanart = "https://image.tmdb.org/t/p/original/canonical-fanart.jpg"
    save_library_index(LibraryIndex(works=[
        WorkIndex(
            work_id="work-manual-restore",
            title="恢复在线图片作品",
            source="local",
            poster_path=canonical_poster,
            fanart_path=canonical_fanart,
        ),
    ]))

    with TestClient(app) as client:
        uploaded = client.put(
            "/api/library/works/work-manual-restore/artwork/poster",
            files={"file": ("cover.png", b"\x89PNG\r\n\x1a\nmanual", "image/png")},
        ).json()
        overridden = client.get("/api/library?compact=true").json()["works"][0]
        restored = client.delete("/api/library/works/work-manual-restore/artwork/poster")
        compact = client.get("/api/library?compact=true").json()["works"][0]

    assert overridden["poster_path"] == uploaded["path"]
    assert restored.status_code == 200
    assert compact["poster_path"] == canonical_poster
    assert compact["local_poster_path"] == ""
    assert compact["fanart_path"] == canonical_fanart


def test_manual_title_override_applies_to_list_and_detail_and_can_be_restored(tmp_path):
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    save_library_index(LibraryIndex(works=[
        WorkIndex(work_id="work-title", title="Heya Camp", original_title="へやキャン△", source="local"),
    ]))

    with TestClient(app) as client:
        saved = client.patch("/api/library/works/work-title/title", json={"title": "房间露营△"})
        compact = client.get("/api/library?compact=true").json()["works"][0]
        detail = client.get("/api/library/works/work-title").json()
        restored = client.delete("/api/library/works/work-title/title")
        original = client.get("/api/library/works/work-title").json()

    assert saved.status_code == 200
    assert saved.json()["title"] == "房间露营△"
    assert compact["title"] == "房间露营△"
    assert detail["title"] == "房间露营△"
    assert detail["title_provenance"] == "manual"
    assert restored.status_code == 200
    assert original["title"] == "Heya Camp"


def test_localized_nfo_title_wins_over_english_recognition_title(tmp_path):
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    season_dir = tmp_path / "Heya Camp" / "Season 1"
    season_dir.mkdir(parents=True)
    (season_dir / "tvshow.nfo").write_text(
        "<tvshow><title>房间露营△</title><originaltitle>へやキャン△</originaltitle></tvshow>",
        encoding="utf-8",
    )
    save_library_index(LibraryIndex(works=[
        WorkIndex(
            work_id="work-localized-title",
            title="Heya Camp",
            original_title="へやキャン△",
            source="local",
            dir_path=str(season_dir),
        ),
    ]))

    with TestClient(app) as client:
        compact = client.get("/api/library?compact=true").json()["works"][0]
        detail = client.get("/api/library/works/work-localized-title").json()

    assert compact["title"] == "房间露营△"
    assert compact["title_provenance"] == "nfo"
    assert detail["title"] == "房间露营△"
    assert detail["title_provenance"] == "nfo"
