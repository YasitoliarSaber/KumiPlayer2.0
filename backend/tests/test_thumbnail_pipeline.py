"""缩略图管线测试"""


import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.library import thumbnails as thumb
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mirror_dir(tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    monkeypatch.setattr("app.api.assets.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    return mirror


def _make_test_image(path, width=780, height=1170):
    """生成一张真实的 RGB 测试图片"""
    img = Image.new("RGB", (width, height), color=(60, 120, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)


class TestThumbnailGeneration:
    """缩略图生成核心逻辑"""

    def test_generates_webp_thumbnail(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source)

        result = thumb.get_or_create_thumbnail(source, 384)
        assert result is not None
        assert result.exists()
        assert result.suffix == ".webp"

        with Image.open(result) as img:
            assert img.width <= 384
            assert img.height <= 384 * 2

    def test_cache_hit_does_not_regenerate(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source)

        first = thumb.get_or_create_thumbnail(source, 384)
        assert first is not None
        first_mtime = first.stat().st_mtime_ns

        second = thumb.get_or_create_thumbnail(source, 384)
        assert second == first
        assert second.stat().st_mtime_ns == first_mtime

    def test_source_change_invalidates_cache(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source, 780, 1170)

        first = thumb.get_or_create_thumbnail(source, 384)
        assert first is not None

        # 改写源文件（mtime 变化）
        _make_test_image(source, 600, 900)

        second = thumb.get_or_create_thumbnail(source, 384)
        assert second is not None
        assert second != first

    def test_invalid_width_returns_none(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source)

        assert thumb.get_or_create_thumbnail(source, 999) is None
        assert thumb.get_or_create_thumbnail(source, 100) is None

    def test_corrupt_image_returns_none(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"not-an-image")

        assert thumb.get_or_create_thumbnail(source, 384) is None

    def test_does_not_modify_source(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source, 780, 1170)
        original_size = source.stat().st_size
        original_mtime = source.stat().st_mtime_ns

        thumb.get_or_create_thumbnail(source, 384)

        assert source.stat().st_size == original_size
        assert source.stat().st_mtime_ns == original_mtime

    def test_png_source_supported(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.png"
        img = Image.new("RGBA", (400, 600), (100, 200, 50, 255))
        source.parent.mkdir(parents=True, exist_ok=True)
        img.save(source, "PNG")

        result = thumb.get_or_create_thumbnail(source, 384)
        assert result is not None
        assert result.exists()

    def test_512_width(self, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source, 780, 1170)

        result = thumb.get_or_create_thumbnail(source, 512)
        assert result is not None
        with Image.open(result) as img:
            assert img.width <= 512


class TestThumbnailEndpoint:
    """缩略图 API 端点"""

    def test_returns_thumbnail(self, client, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source)

        resp = client.get("/api/assets/thumbnail?path=115/TestWork/poster.jpg&width=384")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/webp"
        assert "max-age=86400" in resp.headers["cache-control"]

    def test_falls_back_to_original_on_corrupt(self, client, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"corrupt-data")

        resp = client.get("/api/assets/thumbnail?path=115/TestWork/poster.jpg&width=384")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == b"corrupt-data"

    def test_404_for_missing_file(self, client, mirror_dir):
        resp = client.get("/api/assets/thumbnail?path=115/missing/poster.jpg&width=384")
        assert resp.status_code == 404

    def test_rejects_path_traversal(self, client, mirror_dir):
        resp = client.get("/api/assets/thumbnail?path=../../../etc/passwd&width=384")
        assert resp.status_code == 404

    def test_rejects_disallowed_extension(self, client, mirror_dir):
        video_dir = mirror_dir / "115" / "TestWork"
        video_dir.mkdir(parents=True)
        (video_dir / "video.mkv").write_bytes(b"fake")

        resp = client.get("/api/assets/thumbnail?path=115/TestWork/video.mkv&width=384")
        assert resp.status_code == 403

    def test_invalid_width_falls_back_to_original(self, client, mirror_dir, monkeypatch):
        cache_dir = mirror_dir.parent / "cache"
        monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: cache_dir)
        monkeypatch.setattr(thumb, "get_cache_dir", lambda: cache_dir)

        source = mirror_dir / "115" / "TestWork" / "poster.jpg"
        _make_test_image(source)

        resp = client.get("/api/assets/thumbnail?path=115/TestWork/poster.jpg&width=999")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
