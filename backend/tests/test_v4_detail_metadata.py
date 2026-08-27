"""V4 detail-page metadata contract regressions."""

from __future__ import annotations

from types import SimpleNamespace


def test_default_metadata_provider_emits_logo_and_episode_stills(monkeypatch):
    from app.media_v4.jobs import metadata as metadata_module

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, provider_id):
            assert provider_id == 42
            return {
                "name": "Show",
                "original_name": "Original Show",
                "first_air_date": "2024-01-01",
                "images": {
                    "posters": [{"file_path": "/poster.jpg"}],
                    "backdrops": [{"file_path": "/fanart.jpg"}],
                    "logos": [{"file_path": "/logo.png"}],
                },
                "episode_run_time": [24],
            }

        def get_tv_season_episodes(self, provider_id, season_number):
            assert (provider_id, season_number) == (42, 1)
            return {
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "Online Episode",
                        "overview": "Episode plot",
                        "runtime": 24,
                        "still_path": "/still.jpg",
                        "id": 9001,
                    }
                ]
            }

        @staticmethod
        def select_best_poster(images):
            return images["posters"][0]["file_path"]

        @staticmethod
        def select_best_backdrop(images):
            return images["backdrops"][0]["file_path"]

        @staticmethod
        def select_best_logo(images):
            return images["logos"][0]["file_path"]

        @staticmethod
        def build_image_url(path, size):
            return f"https://image.tmdb.org/t/p/{size}{path}"

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="token"))

    result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "Show",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "42", "media_type": "tv"}],
        "episodes": [{
            "episode_id": "episode-1",
            "season_id": "season-1",
            "local_season_number": 1,
            "local_episode_number": 1,
            "season_kind": "regular",
            "episode_kind": "regular",
        }],
    })

    assert result["clearlogo_url"] == "https://image.tmdb.org/t/p/original/logo.png"
    assert result["episode_mappings"] == [{
        "episode_id": "episode-1",
        "provider_season_number": 1,
        "provider_episode_number": 1,
        "provider_episode_id": "9001",
        "title": "Online Episode",
        "plot": "Episode plot",
        "runtime": 24,
        "still_url": "https://image.tmdb.org/t/p/w500/still.jpg",
    }]


def test_downloaded_clearlogo_keeps_safe_source_extension():
    from app.media_v4.jobs.metadata_artifacts import _artwork_filename

    assert _artwork_filename("clearlogo", "/transparent-logo.svg") == "clearlogo.svg"
    assert _artwork_filename("clearlogo", "/transparent-logo.png") == "clearlogo.png"
    assert _artwork_filename("clearlogo", "/transparent-logo.unknown") == "clearlogo.png"


def test_default_metadata_provider_maps_specials_with_special_number(monkeypatch):
    from app.media_v4.jobs import metadata as metadata_module

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, provider_id):
            assert provider_id == 42
            return {"name": "Show", "images": {}, "episode_run_time": [24]}

        def get_tv_season_episodes(self, provider_id, season_number):
            assert (provider_id, season_number) == (42, 0)
            return {
                "episodes": [{
                    "episode_number": 2,
                    "name": "远端特别篇标题",
                    "overview": "特别篇简介",
                    "runtime": 12,
                    "still_path": "/special.jpg",
                    "id": 9002,
                }]
            }

        @staticmethod
        def select_best_poster(_images):
            return ""

        @staticmethod
        def select_best_backdrop(_images):
            return ""

        @staticmethod
        def select_best_logo(_images):
            return ""

        @staticmethod
        def build_image_url(path, size):
            return f"https://image.tmdb.org/t/p/{size}{path}"

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(
        metadata_module,
        "load_config",
        lambda: SimpleNamespace(tmdb_bearer_token="token"),
    )

    result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "Show",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "42", "media_type": "tv"}],
        "episodes": [{
            "episode_id": "episode-special-2",
            "season_id": "season-special",
            "local_season_number": 0,
            "local_episode_number": None,
            "special_number": 2,
            "season_kind": "special",
            "episode_kind": "special",
        }],
    })

    assert result["episode_mappings"] == [{
        "episode_id": "episode-special-2",
        "provider_season_number": 0,
        "provider_episode_number": 2,
        "provider_episode_id": "9002",
        "title": "远端特别篇标题",
        "plot": "特别篇简介",
        "runtime": 12,
        "still_url": "https://image.tmdb.org/t/p/w500/special.jpg",
    }]
