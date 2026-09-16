"""O14：一次候选搜索只创建一个 TMDB 客户端（连接与响应缓存可复用）。

逐个 query 新建客户端会让每次请求重新握手 TCP/TLS，并且 `_response_cache`、连接池
与限速状态全部作废；叠加全局串行限流后，8 个 query 的成本被放大成 8 份。
"""

from __future__ import annotations

from types import SimpleNamespace


def _fake_client(created: list) -> type:
    class FakeTMDBClient:
        def __init__(self, *args, **kwargs):
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def search_tv(self, _query, _year=None):
            return []

        def search_movie(self, _query, _year=None):
            return []

    return FakeTMDBClient


def _patch(monkeypatch, created: list) -> None:
    import app.core.config as core_config
    import app.media_v4.jobs.metadata as metadata_module
    import app.scrape.tmdb_client as tmdb_module

    fake = _fake_client(created)
    config = SimpleNamespace(tmdb_bearer_token="token")
    # metadata.py 在模块级 `from app.scrape.tmdb_client import TMDBClient`，因此既要
    # 替换客户端模块里的类，也要替换 metadata 里已绑定的名字——只改前者会让用例真的
    # 发起网络请求（第一次写这条用例时就踩到了：TMDB 超时）。
    monkeypatch.setattr(tmdb_module, "TMDBClient", fake)
    monkeypatch.setattr(metadata_module, "TMDBClient", fake)
    monkeypatch.setattr(core_config, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(metadata_module, "load_config", lambda *a, **k: config)


def test_default_candidate_search_reuses_one_client_across_all_queries(monkeypatch):
    from app.media_v4.resolution import candidates as candidates_module

    created: list = []
    _patch(monkeypatch, created)

    result = candidates_module.default_candidate_search(
        "work:1", ["阿一", "阿二", "阿三", "阿四", "阿五"], 2024, "tv",
    )

    assert result == []
    assert len(created) == 1, f"5 个 query 只应创建 1 个客户端，实际 {len(created)} 个"


def test_search_tmdb_candidates_owns_a_client_when_none_is_passed(monkeypatch):
    """默认路径不变：不传 client 时自己开一个（保持既有调用方语义）。"""

    from app.media_v4.jobs import metadata as metadata_module

    created: list = []
    _patch(monkeypatch, created)

    assert metadata_module.search_tmdb_candidates("阿一", "tv", 2024) == []
    assert len(created) == 1


def test_search_tmdb_candidates_reuses_the_injected_client(monkeypatch):
    from app.media_v4.jobs import metadata as metadata_module

    created: list = []
    _patch(monkeypatch, created)
    shared = _fake_client(created)()

    metadata_module.search_tmdb_candidates("阿一", "tv", 2024, client=shared)
    metadata_module.search_tmdb_candidates("阿二", "tv", 2024, client=shared)

    assert len(created) == 1, "注入 client 时不得再新建"


def test_candidate_search_skips_network_entirely_without_a_token(monkeypatch):
    import app.core.config as core_config
    import app.media_v4.jobs.metadata as metadata_module
    import app.scrape.tmdb_client as tmdb_module
    from app.media_v4.resolution import candidates as candidates_module

    created: list = []
    monkeypatch.setattr(tmdb_module, "TMDBClient", _fake_client(created))
    config = SimpleNamespace(tmdb_bearer_token="")
    monkeypatch.setattr(core_config, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(metadata_module, "load_config", lambda *a, **k: config)

    assert candidates_module.default_candidate_search("work:1", ["阿一"], 2024, "tv") == []
    assert created == [], "未配置 Token 时不应创建任何客户端"
