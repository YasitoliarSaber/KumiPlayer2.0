"""TMDB 产物下载安全边界；只使用模拟传输与临时文件。"""

import hashlib

import httpx
import pytest

from app.media_v4.jobs import metadata_artifacts as module

JPEG = b"\xff\xd8\xff\xe0fixture"
URL = "https://image.tmdb.org/t/p/original/poster.jpg"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("app.core.url_guard.socket.getaddrinfo", lambda *_a, **_k: [
        (None, None, None, None, ("93.184.216.34", 443)),
    ])


@pytest.mark.parametrize("url", [
    "http://image.tmdb.org/t/p/original/a.jpg",
    "https://user:secret@image.tmdb.org/t/p/original/a.jpg",
    "https://image.tmdb.org:444/t/p/original/a.jpg",
    "https://image.tmdb.org/other/a.jpg",
    "https://image.tmdb.org/t/p/%2e%2e/a.jpg",
    "https://s4.anilist.co/file/anilistcdn/a.jpg",
])
def test_invalid_url_never_reaches_transport(tmp_path, url):
    def denied(_request):
        raise AssertionError("invalid URL reached transport")
    with httpx.Client(transport=httpx.MockTransport(denied)) as client:
        assert module._download_artwork(url, tmp_path / "image.jpg", client=client) == ""


@pytest.mark.parametrize("status,mime,payload", [
    (302, "image/jpeg", JPEG),
    (200, "image/jpeg", b"<html>not an image</html>"),
    (200, "image/jpeg", b""),
    (200, "image/png", JPEG),
    (200, "text/html", JPEG),
    (200, "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
    (200, "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.test/a"/></svg>'),
])
def test_invalid_response_preserves_old_artifact(tmp_path, status, mime, payload):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"old")
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
        status, headers={"content-type": mime, "location": URL}, content=payload,
    ))) as client:
        assert module._download_artwork(URL, path, client=client) == ""
    assert path.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_valid_image_atomic_digest(tmp_path):
    path = tmp_path / "image.jpg"
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
        200, headers={"content-type": "image/jpeg"}, content=JPEG,
    ))) as client:
        assert module._download_artwork(URL, path, client=client) == hashlib.sha256(JPEG).hexdigest()
    assert path.read_bytes() == JPEG


def test_private_dns_prevents_request(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.url_guard.socket.getaddrinfo", lambda *_a, **_k: [
        (None, None, None, None, ("127.0.0.1", 443)),
    ])
    def denied(_request):
        raise AssertionError("private address reached transport")
    with httpx.Client(transport=httpx.MockTransport(denied)) as client:
        assert module._download_artwork(URL, tmp_path / "image.jpg", client=client) == ""


def test_size_limit_stops_stream_before_reading_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_MAX_ARTWORK_BYTES", 8, raising=False)
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield JPEG
            raise AssertionError("oversized stream was consumed")
    path = tmp_path / "image.jpg"
    path.write_bytes(b"old")
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
        200, headers={"content-type": "image/jpeg"}, stream=Stream(),
    ))) as client:
        assert module._download_artwork(URL, path, client=client) == ""
    assert path.read_bytes() == b"old"


def test_interrupted_stream_never_publishes(tmp_path):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield JPEG
            raise httpx.ReadError("fixture interruption")
    path = tmp_path / "image.jpg"
    path.write_bytes(b"old")
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(
        200, headers={"content-type": "image/jpeg"}, stream=Stream(),
    ))) as client:
        with pytest.raises(httpx.ReadError):
            module._download_artwork(URL, path, client=client)
    assert path.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.tmp")) == []
