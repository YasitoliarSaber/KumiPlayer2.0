# -*- coding: utf-8 -*-
"""Bangumi integration and safe folder-opening API tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _prepare_library(tmp_path, monkeypatch):
    from app.core import paths as core_paths
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import invalidate_library_index_cache, save_library_index

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: cache_dir)
    invalidate_library_index_cache()

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "S01E01.mkv"
    media_file.write_bytes(b"demo")
    strm_path = tmp_path / "mirror" / "Summer" / "S01E01.strm"
    strm_path.parent.mkdir(parents=True)
    strm_path.write_text(str(media_file), encoding="utf-8")

    episode = EpisodeIndex(
        episode_id="ep-1",
        work_id="work-summer",
        season_number=1,
        episode_number=1,
        title="春风与你",
        strm_path=str(strm_path),
    )
    work = WorkIndex(
        work_id="work-summer",
        title="夏日回声",
        seasons=[],
        episodes=[episode],
    )
    save_library_index(LibraryIndex(works=[work]))
    return media_dir, media_file


def test_bangumi_match_and_episode_watched_sync(tmp_path, monkeypatch):
    """A confirmed season match can sync a local episode as watched."""
    _prepare_library(tmp_path, monkeypatch)

    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.main import app

    class FakeBangumiClient:
        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            assert subject_id == 12345
            return [{"id": 9001, "ep": 1, "name": "Episode 1"}]

        def set_episode_collection(self, episode_id, collection_type=2):
            assert episode_id == 9001
            assert collection_type == 2
            return {"ok": True}

    monkeypatch.setattr(bangumi_api, "BangumiClient", lambda: FakeBangumiClient())

    client = TestClient(app)
    response = client.post(
        "/api/integrations/bangumi/matches/work-summer",
        json={"season_number": 1, "subject_id": 12345, "subject_name_cn": "夏日回声"},
    )
    assert response.status_code == 200
    assert response.json()["subject_id"] == 12345

    response = client.put(
        "/api/integrations/bangumi/episodes/ep-1/watched",
        json={"work_id": "work-summer"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["bangumi_episode_id"] == 9001
    assert data["type"] == 2

    response = client.get("/api/integrations/bangumi/episodes/work-summer?season_number=1")
    assert response.status_code == 200
    episode = response.json()["episodes"][0]
    assert episode["bangumi_episode_id"] == 9001
    assert episode["synced"] is True


def test_confirming_bangumi_match_backfills_completed_episodes(tmp_path, monkeypatch):
    """Episodes completed before match confirmation are synchronized afterwards."""
    _prepare_library(tmp_path, monkeypatch)

    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.main import app

    calls = []
    monkeypatch.setattr(
        bangumi_api,
        "sync_bidirectional_progress",
        lambda work_id, season_number, **kwargs: calls.append((work_id, season_number)) or {
            "ok": True,
            "status": "synced",
            "work_id": work_id,
            "season_number": season_number,
            "subject_id": 12345,
            "remote_done_before": 0,
            "local_done_before": 1,
            "pulled": 0,
            "pushed": 1,
            "pending": 0,
        },
        raising=False,
    )

    client = TestClient(app)
    response = client.post(
        "/api/integrations/bangumi/matches/work-summer",
        json={"season_number": 1, "subject_id": 12345, "subject_name_cn": "夏日回声"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "sync" in data
    assert data["sync"]["ok"] is True
    assert calls == [("work-summer", 1)]


def test_bangumi_global_match_does_not_apply_to_another_season(tmp_path, monkeypatch):
    """Legacy whole-work bindings must never mark a different season as matched."""
    _prepare_library(tmp_path, monkeypatch)

    from app.integrations import bangumi

    monkeypatch.setattr(bangumi, "get_state_path", lambda: tmp_path / "bangumi_state.json")

    bangumi.upsert_match(bangumi.BangumiMatch(work_id="work-summer", subject_id=12345))

    assert bangumi.get_match("work-summer", 1) is None
    assert bangumi.get_match("work-summer", None).subject_id == 12345


def test_bangumi_client_explains_unavailable_configured_proxy(monkeypatch):
    """A refused configured proxy must not surface as an opaque WinError."""
    import httpx
    from app.integrations.bangumi import BangumiClient, BangumiError

    class FakeConfig:
        bangumi_access_token = "token"
        bangumi_user_agent = "KumiPlayer test"
        proxy_url = "http://127.0.0.1:7890"

    class RefusingClient:
        def __init__(self, **kwargs):
            assert kwargs["proxy"] == "http://127.0.0.1:7890"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            raise httpx.ConnectError("[WinError 10061] Connection refused")

    monkeypatch.setattr("app.integrations.bangumi.load_config", lambda: FakeConfig())
    monkeypatch.setattr(httpx, "Client", RefusingClient)

    try:
        BangumiClient().get_me()
    except BangumiError as error:
        assert str(error) == "Bangumi 代理不可连接（http://127.0.0.1:7890），请启动 Clash 或检查代理端口。"
    else:
        raise AssertionError("Expected a BangumiError")


def test_bangumi_client_never_uses_a_personal_user_agent(monkeypatch):
    """分发版请求只能携带应用标识，不能继承旧配置中的姓名或昵称。"""
    from app.integrations.bangumi import BangumiClient
    from app.core.config import DEFAULT_BANGUMI_USER_AGENT

    class LegacyConfig:
        bangumi_access_token = ""
        bangumi_user_agent = "PersonalName/1.0"
        proxy_url = ""

    monkeypatch.setattr("app.integrations.bangumi.load_config", lambda: LegacyConfig())

    assert BangumiClient().user_agent == DEFAULT_BANGUMI_USER_AGENT


def test_bangumi_special_uses_special_episode_type(tmp_path, monkeypatch):
    """Specials must not be resolved against the main-story episode list."""
    _prepare_library(tmp_path, monkeypatch)

    from app.integrations.bangumi import BangumiMatch, resolve_bangumi_episode_id
    from app.library.models import EpisodeIndex

    calls = []

    class FakeBangumiClient:
        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            calls.append((subject_id, episode_type))
            return [{"id": 9101, "ep": 1, "name": "Special 1"}]

    episode = EpisodeIndex(
        episode_id="sp-1",
        work_id="work-summer",
        season_number=0,
        episode_number=1,
        group_type="special",
    )
    match = BangumiMatch(work_id="work-summer", season_number=0, subject_id=12345)

    assert resolve_bangumi_episode_id(FakeBangumiClient(), match, episode) == 9101
    assert calls == [(12345, 1)]


def test_open_folder_resolves_real_media_folder_without_opening(tmp_path, monkeypatch):
    """The system API resolves a known episode's real media folder from .strm."""
    media_dir, media_file = _prepare_library(tmp_path, monkeypatch)

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/system/open-folder",
        json={"work_id": "work-summer", "episode_id": "ep-1", "open": False},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["opened"] is False
    assert data["exists"] is True
    assert data["folder_path"] == str(media_dir)
    assert data["source_path"] == str(media_file)


def test_open_folder_resolves_mirror_folder_without_opening(tmp_path, monkeypatch):
    """The system API resolves the generated mirror folder without exposing arbitrary paths."""
    _prepare_library(tmp_path, monkeypatch)

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/system/open-folder",
        json={"work_id": "work-summer", "folder_type": "mirror", "open": False},
    )

    assert response.status_code == 200
    data = response.json()
    mirror_file = tmp_path / "mirror" / "Summer" / "S01E01.strm"
    assert data["opened"] is False
    assert data["exists"] is True
    assert data["folder_path"] == str(mirror_file.parent)
    assert data["source_path"] == str(mirror_file)


def test_open_folder_resolves_hidden_source_location_by_episode_id(tmp_path, monkeypatch):
    media_dir, media_file = _prepare_library(tmp_path, monkeypatch)

    from app.library.store import load_library_index, save_library_index
    from app.main import app
    from fastapi.testclient import TestClient

    index = load_library_index()
    index.works[0].source_locations = {
        "local": {"episode_id": "local-copy", "strm_path": index.works[0].episodes[0].strm_path},
    }
    save_library_index(index)

    client = TestClient(app)
    video = client.post(
        "/api/system/open-folder",
        json={"work_id": "work-summer", "episode_id": "local-copy", "open": False},
    )
    mirror = client.post(
        "/api/system/open-folder",
        json={
            "work_id": "work-summer",
            "episode_id": "local-copy",
            "folder_type": "mirror",
            "open": False,
        },
    )

    assert video.status_code == 200
    assert video.json()["folder_path"] == str(media_dir)
    assert video.json()["source_path"] == str(media_file)
    assert mirror.status_code == 200
    assert mirror.json()["source_path"].endswith("S01E01.strm")


def test_bangumi_me_normalizes_avatar_object(monkeypatch):
    """Bangumi /me may return avatar as a size map; expose a cacheable image URL."""
    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.main import app

    class FakeBangumiClient:
        def get_me(self, purpose: str = ""):
            return {
                "id": 100,
                "username": "kumi",
                "nickname": "久美子",
                "avatar": {
                    "large": "https://lain.bgm.tv/pic/user/l/000/00/01.jpg",
                    "medium": "https://lain.bgm.tv/pic/user/m/000/00/01.jpg",
                },
            }

    monkeypatch.setattr(bangumi_api, "BangumiClient", lambda *args, **kwargs: FakeBangumiClient())

    client = TestClient(app)
    response = client.get("/api/integrations/bangumi/me")
    assert response.status_code == 200
    avatar = response.json()["avatar"]
    assert avatar.startswith("/api/integrations/bangumi/avatar?url=")
    assert "%7B" not in avatar


def test_bangumi_session_keeps_saved_credential_when_service_is_unavailable(monkeypatch):
    """临时网络故障不能被前端误判为 Access Token 丢失。

    新语义：/session 只读本地（0 远程请求），服务不可用时依然恢复凭据状态
    （status=available、credential_saved=true），远程故障交给 /session/verify
    分类，绝不触发 BangumiClient。
    """
    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.integrations.bangumi import BangumiError
    from app.main import app

    class FakeConfig:
        bangumi_access_token = "saved-token"

    class UnavailableBangumiClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("/session 不得发起远程请求（Bangumi 服务不可用也不得调用）")

        def get_me(self, purpose: str = ""):
            raise BangumiError("Bangumi 请求超时")

    monkeypatch.setattr(bangumi_api, "load_config", lambda: FakeConfig())
    monkeypatch.setattr(bangumi_api, "BangumiClient", UnavailableBangumiClient)

    response = TestClient(app).get("/api/integrations/bangumi/session")

    assert response.status_code == 200
    payload = response.json()
    assert payload["credential_saved"] is True
    assert payload["credential_state"] == "found"
    assert payload["status"] == "available"
    assert payload["auth_status"] == "unknown"
    assert payload["user"] is None


def test_bangumi_session_reports_signed_out_without_saved_credential(monkeypatch):
    """没有保存凭据时才显示真正的登录入口。"""
    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.main import app

    class FakeConfig:
        bangumi_access_token = ""

    monkeypatch.setattr(bangumi_api, "load_config", lambda: FakeConfig())

    response = TestClient(app).get("/api/integrations/bangumi/session")

    assert response.status_code == 200
    assert response.json()["credential_saved"] is False
    assert response.json()["status"] == "signed_out"


def test_bangumi_search_normalizes_subject_cover(monkeypatch):
    """Subject search exposes a cacheable cover URL from Bangumi images."""
    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.main import app

    class FakeBangumiClient:
        def search_subjects(self, keyword, limit=10, offset=0, subject_types=None):
            assert keyword == "摇曳露营△"
            return {
                "data": [
                    {
                        "id": 123,
                        "name": "ゆるキャン△",
                        "name_cn": "摇曳露营△",
                        "images": {
                            "grid": "https://lain.bgm.tv/pic/cover/g/1.jpg",
                            "common": "https://lain.bgm.tv/pic/cover/c/1.jpg",
                        },
                    }
                ]
            }

    monkeypatch.setattr(bangumi_api, "BangumiClient", lambda *args, **kwargs: FakeBangumiClient())

    client = TestClient(app)
    response = client.post(
        "/api/integrations/bangumi/search",
        json={"keyword": "摇曳露营△", "limit": 8, "subject_types": [2]},
    )
    assert response.status_code == 200
    subject = response.json()["data"][0]
    assert subject["cover"].startswith("/api/integrations/bangumi/subject-image?url=")
    assert "images" in subject


def test_config_persists_poster_size(tmp_path, monkeypatch):
    """The frontend card-size slider can persist through the config API."""
    from fastapi.testclient import TestClient
    from app.core import config as core_config
    from app.main import app

    monkeypatch.setattr(core_config, "CONFIG_FILE", tmp_path / "config.json")
    core_config.invalidate_config_cache()

    client = TestClient(app)
    response = client.patch("/api/config", json={"poster_size": 220})
    assert response.status_code == 200
    assert response.json()["poster_size"] == 220

    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["poster_size"] == 220


def test_bangumi_image_proxy_rejects_non_whitelisted_host():
    """SSRF 防护：图片代理仅放行 Bangumi 官方图片域，拒绝内网/任意 URL。"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    for raw in (
        "https://127.0.0.1/secret.jpg",
        "https://192.168.1.1/pic/x.jpg",
        "https://evil.example.com/pic/user/x.jpg",
        "http://lain.bgm.tv/pic/user/x.jpg",  # 非 HTTPS 也不放行
    ):
        response = client.get(
            "/api/integrations/bangumi/avatar", params={"url": raw}
        )
        assert response.status_code == 400, f"{raw} 应被拒绝"


def test_bangumi_subject_image_proxy_rejects_non_whitelisted_host():
    """条目图片代理同样拒绝非 Bangumi 官方域（含重定向入口被拒）。"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    response = client.get(
        "/api/integrations/bangumi/subject-image",
        params={"url": "https://evil.example.com/pic/cover/x.jpg"},
    )
    assert response.status_code == 400
