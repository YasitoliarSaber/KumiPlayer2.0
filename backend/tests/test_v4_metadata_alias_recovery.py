"""V4 metadata 阶段的别名自动恢复回归。"""

from __future__ import annotations

from types import SimpleNamespace


def test_metadata_uses_unique_tmdb_alias_before_requesting_manual_review(monkeypatch):
    """本地标题仅命中 TMDB 别名时，必须自动刮削而不是落入人工处理。"""

    from app.media_v4.jobs import metadata as metadata_module

    created_clients = 0

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            nonlocal created_clients
            created_clients += 1
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def search_tv(self, query, year):
            assert query == "房间露营"
            assert year is None
            return [{
                "id": 81234,
                "name": "Heya Camp△",
                "original_name": "へやキャン△",
                "first_air_date": "2020-01-06",
            }]

        def get_tv_detail(self, provider_id):
            assert provider_id == 81234
            return {
                "name": "Heya Camp△",
                "original_name": "へやキャン△",
                "overview": "短篇外传",
                "first_air_date": "2020-01-06",
                "images": {},
                "episode_run_time": [4],
                "alternative_titles": {"results": [{"title": "房间露营△"}]},
                "translations": {"translations": []},
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
        "preferred_title": "房间露营",
        "provider_bindings": [],
        "episodes": [],
    })

    assert result["metadata_state"] == "ready"
    assert result["provider_id"] == "81234"
    assert result["title"] == "Heya Camp△"
    assert created_clients == 1


def test_metadata_retries_the_saved_original_title_before_requesting_manual_review(monkeypatch):
    """本地中文名未命中时，已解析的原文标题仍是安全的等值身份事实。"""

    from app.media_v4.jobs import metadata as metadata_module

    queries: list[str] = []

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def search_tv(self, query, year):
            assert year is None
            queries.append(query)
            if query == "本地化名称":
                return []
            assert query == "原文作品名"
            return [{
                "id": 91234,
                "name": "Original Work",
                "original_name": "原文作品名",
                "first_air_date": "2022-01-01",
            }]

        def get_tv_detail(self, provider_id):
            assert provider_id == 91234
            return {
                "name": "Original Work",
                "original_name": "原文作品名",
                "overview": "",
                "first_air_date": "2022-01-01",
                "images": {},
                "episode_run_time": [24],
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
        "preferred_title": "本地化名称",
        "original_title": "原文作品名",
        "provider_bindings": [],
        "episodes": [],
    })

    assert result["metadata_state"] == "ready"
    assert result["provider_id"] == "91234"
    assert queries == ["本地化名称", "原文作品名"]
