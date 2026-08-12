# -*- coding: utf-8 -*-
"""AniList client tests."""

from types import SimpleNamespace

from app.scrape.anilist_client import AniListClient


def test_anilist_client_uses_configured_proxy(monkeypatch):
    """AniList 应使用应用代理配置，而不是只依赖系统全局代理。"""
    captured = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "app.scrape.anilist_client.load_config",
        lambda: SimpleNamespace(
            anilist_rate_limit=0,
            anilist_timeout=10,
            proxy_url="socks5://127.0.0.1:7890",
        ),
    )
    monkeypatch.setattr("app.scrape.anilist_client.httpx.Client", FakeHttpxClient)

    client = AniListClient()
    client._get_client()

    assert captured["proxy"] == "socks5://127.0.0.1:7890"
