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


def test_metadata_ranks_all_title_queries_instead_of_adopting_first_exact_hit(monkeypatch):
    """多个标题查询必须汇总后统一评分，不能被首个精确命中提前截断。"""

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
            assert year == 2020
            queries.append(query)
            if query == "本地化名称":
                return [{
                    "id": 100,
                    "name": "本地化名称",
                    "original_name": "Less Trusted Result",
                    "first_air_date": "2020-01-01",
                    "popularity": 1,
                    "genre_ids": [18],
                }]
            assert query == "原文作品名"
            return [{
                "id": 200,
                "name": "Correct Work",
                "original_name": "原文作品名",
                "first_air_date": "2020-01-01",
                "popularity": 99,
                "genre_ids": [16],
            }]

        def get_tv_detail(self, provider_id):
            assert provider_id == 200
            return {
                "name": "Correct Work",
                "original_name": "原文作品名",
                "overview": "正确候选",
                "first_air_date": "2020-01-01",
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
        "show_type": "anime_series",
        "preferred_title": "本地化名称",
        "original_title": "原文作品名",
        "year": 2020,
        "provider_bindings": [],
        "episodes": [],
    })

    assert queries == ["本地化名称", "原文作品名"]
    assert result["metadata_state"] == "ready"
    assert result["provider_id"] == "200"
    assert result["candidate_decision"]["decision"] == "auto_adopted"
    assert result["candidate_decision"]["selected_score"] >= 55
    assert result["candidate_decision"]["ranked_candidates"][0]["provider_id"] == "200"


def test_metadata_uses_anime_domain_to_resolve_same_title_live_action_result(monkeypatch):
    """真实同名动画/真人候选应依靠作品域自动选择动画，而非要求人工确认。"""

    from app.media_v4.jobs import metadata as metadata_module

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def search_tv(self, query, year):
            assert query == "Yuru Camp"
            assert year is None
            return [
                {
                    "id": 76075,
                    "name": "摇曳露营△",
                    "original_name": "ゆるキャン△",
                    "first_air_date": "2018-01-04",
                    "popularity": 22.5,
                    "genre_ids": [16, 35],
                },
                {
                    "id": 95623,
                    "name": "摇曳露营△",
                    "original_name": "ゆるキャン△",
                    "first_air_date": "2020-01-10",
                    "popularity": 3.5,
                    "genre_ids": [18],
                },
            ]

        def get_tv_detail(self, provider_id):
            if provider_id == 76075:
                return {
                    "id": 76075,
                    "name": "摇曳露营△",
                    "original_name": "ゆるキャン△",
                    "first_air_date": "2018-01-04",
                    "genres": [{"id": 16, "name": "Animation"}],
                    "alternative_titles": {"results": [{"title": "Yuru Camp"}]},
                    "translations": {"translations": []},
                    "images": {},
                    "episode_run_time": [24],
                }
            assert provider_id == 95623
            return {
                "id": 95623,
                "name": "摇曳露营△",
                "original_name": "ゆるキャン△",
                "first_air_date": "2020-01-10",
                "genres": [{"id": 18, "name": "Drama"}],
                "alternative_titles": {"results": [{"title": "Yuru Camp"}]},
                "translations": {"translations": []},
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
        "show_type": "anime_series",
        "preferred_title": "Yuru Camp",
        "provider_bindings": [],
        "episodes": [],
    })

    assert result["metadata_state"] == "ready"
    assert result["provider_id"] == "76075"


def test_search_retries_without_year_when_year_filtered_query_is_empty(monkeypatch):
    """年份来自目录时，TMDB 年份过滤无结果必须回退到纯标题搜索。"""

    from app.media_v4.jobs import metadata as metadata_module

    calls: list[tuple[str, int | None]] = []

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def search_tv(self, query, year):
            calls.append((query, year))
            if year is not None:
                return []
            return [{
                "id": 300,
                "name": "Correct Work",
                "original_name": "Correct Work Original",
                "first_air_date": "2020-01-01",
            }]

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(
        metadata_module,
        "load_config",
        lambda: SimpleNamespace(tmdb_bearer_token="token"),
    )

    candidates = metadata_module.search_tmdb_candidates("Correct Work", "tv", 2024)

    assert calls == [("Correct Work", 2024), ("Correct Work", None)]
    assert candidates == [{
        "provider": "tmdb",
        "provider_id": "300",
        "title": "Correct Work",
        "original_title": "Correct Work Original",
        "year": 2020,
        "media_type": "tv",
        "popularity": 0.0,
    }]


def test_metadata_distinguishes_zero_results_from_provider_unavailable(monkeypatch):
    """零结果和服务不可用必须保留不同的状态码，便于界面给出正确动作。"""

    from app.media_v4.jobs import metadata as metadata_module
    from app.scrape.tmdb_client import TMDBClientError

    class EmptyTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def search_tv(self, _query, _year):
            return []

    monkeypatch.setattr(metadata_module, "TMDBClient", EmptyTMDBClient)
    monkeypatch.setattr(
        metadata_module,
        "load_config",
        lambda: SimpleNamespace(tmdb_bearer_token="token"),
    )

    zero_result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "完全不存在的作品",
        "provider_bindings": [],
        "episodes": [],
    })

    assert zero_result["metadata_state"] == "waiting_review"
    assert zero_result["reason_code"] == "no_candidates"
    assert zero_result["candidate_decision"]["decision"] == "waiting_review"

    class UnavailableTMDBClient(EmptyTMDBClient):
        def search_tv(self, _query, _year):
            raise TMDBClientError("service unavailable")

    monkeypatch.setattr(metadata_module, "TMDBClient", UnavailableTMDBClient)

    unavailable = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "服务不可用作品",
        "provider_bindings": [],
        "episodes": [],
    })

    assert unavailable["metadata_state"] == "source_unavailable"
    assert unavailable["reason_code"] == "source_unavailable"
