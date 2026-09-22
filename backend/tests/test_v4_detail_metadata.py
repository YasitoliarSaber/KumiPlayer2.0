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


def test_default_metadata_provider_maps_specials_by_title_not_local_number(monkeypatch):
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
            "display_title": "远端特别篇标题",
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


def test_seasonless_local_episodes_are_not_forced_into_an_online_season():
    """规划方要求：未分季的本地集不得被硬套进任意在线季度。

    实测场景（用户库《宝可梦》各篇章）：在线条目有多个常规季，而本地导入是**未分季**
    的一整串集号（例如第 155 集）。此时没有任何证据证明"第 155 集"属于哪个在线季，
    因此必须**不做绑定**（保留本地标题），而不是塞进 Season 1，更不能凭空造季号。
    """

    from app.media_v4.jobs.metadata import _build_tv_episode_mappings

    class FakeClient:
        def get_tv_season_episodes(self, provider_id, season_number):
            raise AssertionError("未分季场景不得请求任何在线季")

    mappings = _build_tv_episode_mappings(
        FakeClient(),
        42,
        {"episodes": [
            {
                "episode_id": "ep-155",
                "season_id": "season-local",
                "local_season_number": None,
                "local_episode_number": 155,
                "provider_season_number": None,
                "display_title": "盆才怪与忍者学园!!",
                "season_kind": "regular",
                "episode_kind": "regular",
            },
        ]},
        work_detail={"seasons": [
            {"season_number": 1},
            {"season_number": 2},
            {"season_number": 3},
        ]},
    )

    assert mappings == [], f"未分季集不得产生任何季集绑定，实际 {mappings}"


def test_special_mapping_uses_remote_title_and_season_zero_even_with_stale_season_mapping():
    from app.media_v4.jobs.metadata import _build_tv_episode_mappings

    class FakeClient:
        def get_tv_season_episodes(self, provider_id, season_number):
            assert (provider_id, season_number) == (42, 0)
            # 在线顺序与本地 SP 编号相反，不能按位置绑定。
            return {"episodes": [
                {"episode_number": 2, "name": "冬日露营", "id": 9002},
                {"episode_number": 1, "name": "夏日露营", "id": 9001},
            ]}

        @staticmethod
        def build_image_url(path, size):
            return f"{size}:{path}"

    mappings = _build_tv_episode_mappings(
        FakeClient(),
        42,
        {"episodes": [
            {
                "episode_id": "special-winter",
                "season_id": "season-special",
                "local_season_number": 0,
                "provider_season_number": 2,
                "special_number": 1,
                "display_title": "冬日露营",
                "season_kind": "special",
                "episode_kind": "special",
            },
            {
                "episode_id": "special-summer",
                "season_id": "season-special",
                "local_season_number": 0,
                "provider_season_number": 2,
                "special_number": 2,
                "display_title": "夏日露营",
                "season_kind": "special",
                "episode_kind": "special",
            },
        ]},
    )

    assert {item["episode_id"]: item["provider_episode_number"] for item in mappings} == {
        "special-winter": 2,
        "special-summer": 1,
    }


def test_duplicate_special_titles_stay_unmapped_instead_of_picking_first_entry():
    from app.media_v4.jobs.metadata import _match_special_episode

    assert _match_special_episode(
        {"display_title": "同名特别篇"},
        {
            1: {"name": "同名特别篇", "id": 9001},
            2: {"name": "同名特别篇", "id": 9002},
        },
    ) is None


def test_generic_special_title_does_not_reuse_historical_episode_number():
    from app.media_v4.jobs.metadata import _match_special_episode

    assert _match_special_episode(
        {
            "display_title": "特别篇",
            "provider_episode_number": 2,
        },
        {2: {"name": "另一个特别篇", "id": 9002}},
    ) is None


def test_unmatched_special_metadata_is_optional_and_clears_stale_mapping(monkeypatch):
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
                    "episode_number": 1,
                    "name": "雨天露营",
                    "id": 9001,
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
            "episode_id": "episode-special-mystery",
            "season_id": "season-special",
            "local_season_number": 0,
            "local_episode_number": None,
            "special_number": 1,
            "display_title": "Mystery Camp",
            "season_kind": "special",
            "episode_kind": "special",
        }],
    })

    assert result["metadata_state"] == "ready"
    assert result["reason_code"] == "special_episode_metadata_incomplete"
    assert result["episode_mappings"] == []
    assert result["clear_episode_mapping_ids"] == ["episode-special-mystery"]
    assert "不影响播放" in result["metadata_warning"]


def test_missing_special_season_discards_old_mapping_and_keeps_local_title(monkeypatch):
    from unittest.mock import MagicMock

    from app.media_v4.jobs import metadata as metadata_module
    from app.media_v4.jobs.scrape import _retain_successful_details
    from app.scrape.tmdb_client import TMDBClientError

    client = MagicMock()
    client.__enter__.return_value = client
    client.get_tv_detail.return_value = {"name": "Show", "images": {}, "episode_run_time": [24]}
    client.select_best_poster.return_value = ""
    client.select_best_backdrop.return_value = ""
    client.select_best_logo.return_value = ""
    client.get_tv_season_episodes.side_effect = TMDBClientError(
        "missing season", status_code=404, reason_code="provider_resource_missing",
        retryable=False, failure_stage="season_detail",
    )
    monkeypatch.setattr(metadata_module, "TMDBClient", lambda **_kwargs: client)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="token"))
    target = {
        "work_type": "series", "preferred_title": "Show",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "42", "media_type": "tv"}],
        "episodes": [{
            "episode_id": "special", "season_id": "s0", "local_season_number": 0,
            "episode_kind": "special", "display_title": "Mystery Camp",
        }],
    }
    result = metadata_module.default_metadata_provider(target)
    merged = _retain_successful_details({"episode_mappings": [{
        "episode_id": "special", "provider_episode_id": "wrong-old-id", "title": "Wrong title",
    }]}, result, target)
    assert result["metadata_state"] == "ready"
    assert result["clear_episode_mapping_ids"] == ["special"]
    assert merged["episode_mappings"] == []
    assert target["episodes"][0]["display_title"] == "Mystery Camp"


def test_special_season_service_failure_is_not_downgraded_to_optional_gap(monkeypatch):
    from app.media_v4.jobs import metadata as metadata_module
    from app.scrape.tmdb_client import TMDBClientError

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, provider_id):
            return {"name": "Show", "images": {}, "episode_run_time": [24]}

        def get_tv_season_episodes(self, provider_id, season_number):
            raise TMDBClientError(
                "temporary outage",
                reason_code="source_unavailable",
                retryable=True,
                failure_stage="season_detail",
            )

        @staticmethod
        def select_best_poster(_images):
            return ""

        @staticmethod
        def select_best_backdrop(_images):
            return ""

        @staticmethod
        def select_best_logo(_images):
            return ""

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
            "episode_id": "episode-special-outage",
            "season_id": "season-special",
            "local_season_number": 0,
            "local_episode_number": None,
            "special_number": 1,
            "display_title": "Mystery Camp",
            "season_kind": "special",
            "episode_kind": "special",
        }],
    })

    assert result["metadata_state"] == "source_unavailable"
    assert result["reason_code"] == "episode_mapping_incomplete"
    assert "metadata_warning" not in result
    assert "clear_episode_mapping_ids" not in result


def test_regular_season_fetch_failure_prevents_false_ready_metadata(monkeypatch):
    from app.media_v4.jobs import metadata as metadata_module
    from app.scrape.tmdb_client import TMDBClientError

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, _provider_id):
            return {"name": "摇曳露营△", "images": {}, "episode_run_time": [24]}

        def get_tv_season_episodes(self, _provider_id, season_number):
            assert season_number == 2
            raise TMDBClientError("season unavailable")

        @staticmethod
        def select_best_poster(_images): return ""

        @staticmethod
        def select_best_backdrop(_images): return ""

        @staticmethod
        def select_best_logo(_images): return ""

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="token"))

    result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "Yuru Camp",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "76075", "media_type": "tv"}],
        "episodes": [{
            "episode_id": "episode-season-2",
            "local_season_number": 2,
            "local_episode_number": 1,
            "season_kind": "regular",
            "episode_kind": "regular",
        }],
    })

    assert result["metadata_state"] == "source_unavailable"


def test_partial_season_failure_keeps_work_metadata_and_retry_details(monkeypatch):
    """单个季度失败时保留作品身份与已成功剧集，方便后续增量重试。"""

    from app.media_v4.jobs import metadata as metadata_module
    from app.scrape.tmdb_client import TMDBClientError

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, _provider_id):
            return {"name": "Show", "original_name": "Original", "images": {}, "episode_run_time": [24]}

        def get_tv_season_episodes(self, _provider_id, season_number):
            if season_number == 2:
                raise TMDBClientError("season unavailable")
            return {"episodes": [{"id": 101, "episode_number": 1, "name": "S1E1"}]}

        @staticmethod
        def select_best_poster(_images): return ""

        @staticmethod
        def select_best_backdrop(_images): return ""

        @staticmethod
        def select_best_logo(_images): return ""

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="token"))

    result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "Show",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "42", "media_type": "tv"}],
        "episodes": [
            {"episode_id": "s1e1", "local_season_number": 1, "local_episode_number": 1, "season_kind": "regular"},
            {"episode_id": "s2e1", "local_season_number": 2, "local_episode_number": 1, "season_kind": "regular"},
        ],
    })

    assert result["provider"] == "tmdb"
    assert result["provider_id"] == "42"
    assert result["metadata_state"] == "source_unavailable"
    assert result["reason_code"] == "episode_mapping_incomplete"
    assert result["title"] == "Show"
    assert [item["episode_id"] for item in result["episode_mappings"]] == ["s1e1"]
    assert result["season_results"] == [{
        "season_id": "",
        "local_season_number": 2,
        "provider_season_number": 2,
        "status": "source_unavailable",
        "reason_code": "source_unavailable",
        "failure_stage": "season_detail",
        "retryable": True,
    }]


def test_detail_fetch_failure_keeps_provider_identity_for_retry(monkeypatch):
    """作品详情请求失败时保留已确认的 Provider ID，不退回本地伪身份。"""

    from app.media_v4.jobs import metadata as metadata_module
    from app.scrape.tmdb_client import TMDBClientError

    class UnavailableTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, _provider_id):
            raise TMDBClientError("detail unavailable")

    monkeypatch.setattr(metadata_module, "TMDBClient", UnavailableTMDBClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="token"))

    result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "Show",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "42", "media_type": "tv"}],
        "episodes": [],
    })

    assert result["provider"] == "tmdb"
    assert result["provider_id"] == "42"
    assert result["metadata_state"] == "source_unavailable"
    assert result["reason_code"] == "source_unavailable"
    assert result["title"] == "Show"


def test_regular_season_two_maps_to_provider_season_two_episode_one(monkeypatch):
    """本地 S02E01 必须请求 provider 第 2 季第 1 集，而不是 Season 1 或 E13。"""

    from types import SimpleNamespace

    from app.media_v4.jobs import metadata as metadata_module

    requested_seasons: list[int] = []

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            assert bearer_token == "token"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, _provider_id):
            return {"name": "摇曳露营△", "images": {}, "episode_run_time": [24]}

        def get_tv_season_episodes(self, _provider_id, season_number):
            requested_seasons.append(season_number)
            assert season_number == 2, f"第二季必须请求 provider 第 2 季，实际请求了 {season_number}"
            return {"episodes": [{
                "id": 76001,
                "episode_number": 1,
                "name": "第二季第一集",
                "overview": "冬季篇开幕",
                "runtime": 24,
                "still_path": "/s02e01.jpg",
            }]}

        @staticmethod
        def select_best_poster(_images): return ""

        @staticmethod
        def select_best_backdrop(_images): return ""

        @staticmethod
        def select_best_logo(_images): return ""

        @staticmethod
        def build_image_url(path, size):
            return f"https://image.tmdb.org/t/p/{size}{path}"

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="token"))

    result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "Yuru Camp",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "76075", "media_type": "tv"}],
        "episodes": [{
            "episode_id": "episode-s02e01",
            "local_season_number": 2,
            "local_episode_number": 1,
            "season_kind": "regular",
            "episode_kind": "regular",
        }],
    })

    assert requested_seasons == [2]
    assert result["episode_mappings"] == [{
        "episode_id": "episode-s02e01",
        "provider_season_number": 2,
        "provider_episode_number": 1,
        "provider_episode_id": "76001",
        "title": "第二季第一集",
        "plot": "冬季篇开幕",
        "runtime": 24,
        "still_url": "https://image.tmdb.org/t/p/w500/s02e01.jpg",
    }]


def test_missing_token_keeps_saved_provider_identity_for_retry(monkeypatch):
    """缺少 Token 时保留已确认的 Provider 身份，避免重试退回本地伪身份。"""

    from app.media_v4.jobs import metadata as metadata_module

    monkeypatch.setattr(
        metadata_module,
        "load_config",
        lambda: SimpleNamespace(tmdb_bearer_token=""),
    )

    result = metadata_module.default_metadata_provider({
        "work_type": "series",
        "preferred_title": "Show",
        "provider_bindings": [{"provider": "tmdb", "provider_id": "42", "media_type": "tv"}],
        "episodes": [],
    })

    assert result["provider"] == "tmdb"
    assert result["provider_id"] == "42"
    assert result["media_type"] == "tv"
    assert result["metadata_state"] == "waiting_metadata"
    assert result["reason_code"] == "provider_auth_required"
    assert result["identity_status"] == "confirmed"
    assert result["retryable"] is True
