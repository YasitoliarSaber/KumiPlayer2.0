"""远程图片代理收口回归：single-flight、缓存命中、缓存头与原子落盘。

详情页首次打开会并发请求同一批远程图片；代理必须保证：
- 相同 URL 的并发请求只产生一次上游下载；
- 磁盘缓存命中不再访问上游；
- 响应带稳定 ETag/Cache-Control，浏览器二次进入可走 304；
- 上游失败或写盘失败时不得留下半截缓存文件。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_proxy_globals():
    """熔断表与锁表是模块级全局；每个用例前后统一清理，避免跨用例污染。"""

    from app.api import assets as assets_module

    assets_module._REMOTE_FAILURES.clear()
    assets_module._REMOTE_INFLIGHT_LOCKS.clear()
    yield
    assets_module._REMOTE_FAILURES.clear()
    assets_module._REMOTE_INFLIGHT_LOCKS.clear()

TMDB_URL = "https://image.tmdb.org/t/p/w500/backdrop.jpg"


def _client(tmp_path, monkeypatch) -> TestClient:
    from app.api import assets as assets_module
    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    # 代理缓存目录在 assets 模块内通过 get_cache_dir 动态解析。
    monkeypatch.setattr(assets_module, "get_cache_dir", lambda: tmp_path / "cache")
    return TestClient(app)


def _async_client(tmp_path, monkeypatch):
    """生产环境是单事件循环；并发合同用同一循环内的 ASGI 传输验证。"""

    import httpx

    from app.api import assets as assets_module
    from app.core import paths as core_paths
    from app.main import app

    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(assets_module, "get_cache_dir", lambda: tmp_path / "cache")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _install_upstream(monkeypatch, calls: list[str], *, payload: bytes = b"image-bytes", fail: bool = False):
    from app.api import assets as assets_module

    class FakeResponse:
        def __init__(self, content: bytes):
            self.content = content
            self.headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            return None

    async def fake_get(_client, url, headers=None):
        calls.append(url)
        if fail:
            import httpx

            raise httpx.ReadError("upstream broken")
        return FakeResponse(payload)

    class FakeClientWrapper:
        def __init__(self, state_client):
            self._state_client = state_client

        async def get(self, url, headers=None):
            return await fake_get(self._state_client, url, headers)

    async def fake_get_client(_app, _proxy):
        class _Inner:
            async def get(self, url, headers=None):
                return await fake_get(self, url, headers)

        return _Inner()

    monkeypatch.setattr(assets_module, "_get_remote_asset_client", fake_get_client)


def test_concurrent_same_url_requests_download_upstream_once(tmp_path, monkeypatch):
    """同 URL 并发代理只产生一次上游下载；全部请求都拿到完整内容。"""


    calls: list[str] = []

    class FakeResponse:
        def __init__(self, content: bytes):
            self.content = content
            self.headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            return None

    async def fake_get_client(_app, _proxy):
        class _Inner:
            async def get(self, url, headers=None):
                calls.append(url)
                await asyncio.sleep(0.05)
                return FakeResponse(b"image-bytes")

        return _Inner()

    from app.api import assets as assets_module

    monkeypatch.setattr(assets_module, "_get_remote_asset_client", fake_get_client)

    async def scenario():
        client = _async_client(tmp_path, monkeypatch)
        try:
            responses = await asyncio.gather(*[
                client.get("/api/assets/remote", params={"url": TMDB_URL})
                for _ in range(4)
            ])
        finally:
            await client.aclose()
        return responses

    responses = asyncio.run(scenario())

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert all(response.content == b"image-bytes" for response in responses)
    assert len(calls) == 1, f"同 URL 并发请求触发了 {len(calls)} 次上游下载"
    cache_files = [
        item for item in (tmp_path / "cache" / "remote_assets").glob("*")
        if not item.name.endswith(".etag")
    ]
    assert len(cache_files) == 1
    assert cache_files[0].read_bytes() == b"image-bytes"


def test_cache_hit_never_touches_upstream(tmp_path, monkeypatch):
    from app.api import assets as assets_module

    client = _client(tmp_path, monkeypatch)
    cache_dir = tmp_path / "cache" / "remote_assets"
    cache_dir.mkdir(parents=True)
    cache_path = assets_module._remote_asset_cache_path(TMDB_URL)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"cached-image")

    def forbidden_get_client(*_args, **_kwargs):
        raise AssertionError("缓存命中不得访问上游")

    monkeypatch.setattr(assets_module, "_get_remote_asset_client", forbidden_get_client)

    response = client.get("/api/assets/remote", params={"url": TMDB_URL})

    assert response.status_code == 200
    assert response.content == b"cached-image"


def test_remote_proxy_returns_stable_cache_headers_and_supports_304(tmp_path, monkeypatch):
    """响应必须带 ETag/Last-Modified/Cache-Control；条件请求返回 304。"""

    client = _client(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_upstream(monkeypatch, calls)

    first = client.get("/api/assets/remote", params={"url": TMDB_URL})
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "public, max-age=86400"
    etag = first.headers.get("ETag")
    last_modified = first.headers.get("Last-Modified")
    assert etag, "远程代理响应缺少 ETag"

    second = client.get(
        "/api/assets/remote",
        params={"url": TMDB_URL},
        headers={"If-None-Match": etag},
    )
    assert second.status_code == 304, "带 ETag 的条件请求应返回 304"
    if last_modified:
        third = client.get(
            "/api/assets/remote",
            params={"url": TMDB_URL},
            headers={"If-Modified-Since": last_modified},
        )
        assert third.status_code == 304


def test_remote_download_persists_digest_sidecar_before_the_next_cache_hit(tmp_path, monkeypatch):
    """下载成功后立即落盘摘要，后续缓存命中无需再整图计算 ETag。"""

    from app.api import assets as assets_module

    client = _client(tmp_path, monkeypatch)
    calls: list[str] = []
    payload = b"digest-sidecar-image"
    _install_upstream(monkeypatch, calls, payload=payload)

    response = client.get("/api/assets/remote", params={"url": TMDB_URL})

    assert response.status_code == 200
    cache_path = assets_module._remote_asset_cache_path(TMDB_URL)
    sidecar = cache_path.with_suffix(f"{cache_path.suffix}.etag")
    assert sidecar.read_text(encoding="ascii").strip() == hashlib.sha256(payload).hexdigest()

    original_read_bytes = Path.read_bytes

    def _forbid_reading_cached_image(path: Path, *args, **kwargs):
        if path == cache_path:
            raise AssertionError("缓存命中不得为计算 ETag 再次读取整张图片")
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _forbid_reading_cached_image)
    cached = client.get("/api/assets/remote", params={"url": TMDB_URL})
    assert cached.status_code == 200
    assert cached.content == payload
    assert calls == [TMDB_URL]


def test_upstream_failure_leaves_no_partial_cache_file(tmp_path, monkeypatch):
    """上游失败时不得产生缓存文件；后续重试必须再次访问上游。"""

    client = _client(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_upstream(monkeypatch, calls, fail=True)

    response = client.get("/api/assets/remote", params={"url": TMDB_URL})
    assert response.status_code == 502

    cache_dir = tmp_path / "cache" / "remote_assets"
    if cache_dir.exists():
        assert list(cache_dir.glob("*")) == [], "失败请求留下了缓存残留"

    # 短 TTL 内重试同样失败但不再放大上游；TTL 过后恢复访问上游。
    retry = client.get("/api/assets/remote", params={"url": TMDB_URL})
    assert retry.status_code == 502
    upstream_calls = len(calls)
    assert upstream_calls <= 1, "失败熔断窗口内的重试不应反复请求上游"


def test_local_asset_endpoints_return_304_for_conditional_requests(tmp_path):
    """本地图片端点带稳定 ETag；条件命中直接 304，不再整张回传。"""

    import pytest

    from app.api import assets as assets_module
    from app.core import paths as core_paths
    from app.main import app

    mirror = tmp_path / "mirror"
    image = mirror / "works" / "show" / "poster.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"local-image-bytes")

    test_monkey = pytest.MonkeyPatch()
    try:
        test_monkey.setattr(core_paths, "get_mirror_root", lambda: mirror)
        test_monkey.setattr(core_paths, "get_data_dir", lambda: tmp_path / "data")
        test_monkey.setattr(assets_module, "get_mirror_root", lambda: mirror)
        test_monkey.setattr(assets_module, "get_data_dir", lambda: tmp_path / "data")
        client = TestClient(app)

        first = client.get("/api/assets", params={"path": str(image)})
        assert first.status_code == 200
        etag = first.headers["ETag"]
        assert first.headers["Cache-Control"].startswith("public, max-age=")

        second = client.get(
            "/api/assets", params={"path": str(image)}, headers={"If-None-Match": etag}
        )
        assert second.status_code == 304

        thumb_first = client.get("/api/assets/thumbnail", params={"path": str(image), "width": 384})
        assert thumb_first.status_code == 200
        thumb_second = client.get(
            "/api/assets/thumbnail",
            params={"path": str(image), "width": 384},
            headers={"If-None-Match": thumb_first.headers["ETag"]},
        )
        assert thumb_second.status_code == 304
    finally:
        test_monkey.undo()


def test_inflight_entry_is_reclaimed_after_download(tmp_path, monkeypatch):
    """下载完成后对应 inflight 条目必须被回收，锁表不得无界增长。"""

    from app.api import assets as assets_module

    client = _client(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_upstream(monkeypatch, calls)

    first = client.get("/api/assets/remote", params={"url": TMDB_URL})
    assert first.status_code == 200
    second_url = "https://image.tmdb.org/t/p/w500/other.jpg"
    second = client.get("/api/assets/remote", params={"url": second_url})
    assert second.status_code == 200
    assert len(calls) == 2
    assert assets_module._REMOTE_INFLIGHT_LOCKS == {}, (
        f"锁表未回收: {list(assets_module._REMOTE_INFLIGHT_LOCKS)}"
    )


def test_failed_leader_gives_waiters_consistent_failure_without_upstream_retry(tmp_path, monkeypatch):
    """首个请求失败时，等待者拿到一致的 502，且不立即重复请求上游。"""

    import asyncio

    from app.api import assets as assets_module

    calls: list[str] = []

    async def failing_get_client(_app, _proxy):
        class _Inner:
            async def get(self, url, headers=None):
                calls.append(url)
                await asyncio.sleep(0.05)
                import httpx

                raise httpx.ReadError("upstream broken")

        return _Inner()

    monkeypatch.setattr(assets_module, "_get_remote_asset_client", failing_get_client)

    async def scenario():
        client = _async_client(tmp_path, monkeypatch)
        try:
            responses = await asyncio.gather(*[
                client.get("/api/assets/remote", params={"url": TMDB_URL})
                for _ in range(4)
            ])
        finally:
            await client.aclose()
        return responses

    responses = asyncio.run(scenario())

    assert [response.status_code for response in responses] == [502, 502, 502, 502]
    assert len(calls) == 1, f"失败合并失效，上游被请求了 {len(calls)} 次"


def test_failure_circuit_allows_retry_after_ttl(tmp_path, monkeypatch):
    """失败熔断到期后应恢复访问上游，而不是永久 502。"""

    from app.api import assets as assets_module

    # _REMOTE_FAILURES 是模块级全局，先清掉其他用例留下的熔断记录。
    assets_module._REMOTE_FAILURES.clear()
    client = _client(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_upstream(monkeypatch, calls, fail=True)

    assert client.get("/api/assets/remote", params={"url": TMDB_URL}).status_code == 502
    assert client.get("/api/assets/remote", params={"url": TMDB_URL}).status_code == 502
    assert len(calls) == 1, "熔断窗口内不应重复请求上游"

    # 人工推进时间越过 TTL 后，重试应再次访问上游。
    real_monotonic = assets_module.time.monotonic

    class FakeClock:
        def __init__(self):
            self.now = real_monotonic()

        def __call__(self):
            return self.now

    fake_clock = FakeClock()
    monkeypatch.setattr(assets_module.time, "monotonic", fake_clock)
    fake_clock.now += assets_module._REMOTE_FAILURE_TTL + 1
    fourth = client.get("/api/assets/remote", params={"url": TMDB_URL})
    assert fourth.status_code == 502
    assert len(calls) == 2, "熔断到期后必须重新访问上游"
