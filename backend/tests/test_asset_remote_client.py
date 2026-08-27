"""远程图片代理连接池回归。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_remote_asset_client_reuses_connection_pool_and_closes_on_proxy_change(monkeypatch):
    from app.api import assets as assets_module

    created = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.is_closed = False
            created.append(self)

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(assets_module.httpx, "AsyncClient", FakeAsyncClient)
    app = SimpleNamespace(state=SimpleNamespace())

    async def exercise():
        first = await assets_module._get_remote_asset_client(app, None)
        second = await assets_module._get_remote_asset_client(app, None)
        assert first is second
        assert len(created) == 1
        assert created[0].kwargs["http2"] is True

        third = await assets_module._get_remote_asset_client(app, "http://127.0.0.1:7890")
        assert third is not first
        assert first.is_closed is True
        assert len(created) == 2

        await assets_module.close_remote_asset_client(app)
        assert third.is_closed is True

    asyncio.run(exercise())
