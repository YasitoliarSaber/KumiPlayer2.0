"""Assets API 测试"""


import pytest
from app.api.assets import _validate_remote_asset_url
from app.core.config import invalidate_config_cache
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """创建测试客户端"""
    invalidate_config_cache()
    return TestClient(app)


@pytest.fixture
def mirror_dir(tmp_path, monkeypatch):
    """使用临时 mirror 目录"""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    monkeypatch.setattr("app.api.assets.get_mirror_root", lambda: mirror)
    return mirror


# ============================================================
# GET /api/assets
# ============================================================

class TestGetAsset:
    """测试获取资源文件"""

    def test_returns_image(self, client, mirror_dir):
        """应能返回图片文件"""
        # 创建测试图片
        poster = mirror_dir / "115" / "CLANNAD"
        poster.mkdir(parents=True)
        (poster / "poster.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpg")

        resp = client.get("/api/assets?path=115/CLANNAD/poster.jpg")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == b"\xff\xd8\xff\xe0fake-jpg"

    def test_returns_nfo(self, client, mirror_dir):
        """应能返回 NFO 文件"""
        nfo_dir = mirror_dir / "115" / "CLANNAD"
        nfo_dir.mkdir(parents=True)
        nfo_content = '<?xml version="1.0"?><tvshow><title>CLANNAD</title></tvshow>'
        (nfo_dir / "tvshow.nfo").write_text(nfo_content, encoding="utf-8")

        resp = client.get("/api/assets?path=115/CLANNAD/tvshow.nfo")
        assert resp.status_code == 200
        assert "xml" in resp.headers["content-type"]
        assert resp.text == nfo_content

    def test_returns_404_for_missing_file(self, client, mirror_dir):
        """文件不存在时应返回 404"""
        resp = client.get("/api/assets?path=115/CLANNAD/poster.jpg")
        assert resp.status_code == 404

    def test_rejects_path_traversal(self, client, mirror_dir):
        """应拒绝路径遍历"""
        resp = client.get("/api/assets?path=../etc/passwd")
        assert resp.status_code == 403

    def test_allows_filename_containing_dots(self, client, mirror_dir):
        """文件名中包含连续点号但不是 .. 路径组件时不应被误伤"""
        asset_dir = mirror_dir / "115" / "CLANNAD"
        asset_dir.mkdir(parents=True)
        (asset_dir / "poster...v1.jpg").write_bytes(b"fake-jpg")

        resp = client.get("/api/assets?path=115/CLANNAD/poster...v1.jpg")
        assert resp.status_code == 200

    def test_rejects_absolute_outside_mirror(self, client, mirror_dir):
        """应拒绝 mirror 外的绝对路径"""
        resp = client.get("/api/assets?path=C:/Windows/System32/config/SAM")
        assert resp.status_code == 403

    def test_rejects_disallowed_extension(self, client, mirror_dir):
        """应拒绝不允许的扩展名"""
        video_dir = mirror_dir / "115" / "CLANNAD"
        video_dir.mkdir(parents=True)
        (video_dir / "video.mkv").write_bytes(b"fake-video")

        resp = client.get("/api/assets?path=115/CLANNAD/video.mkv")
        assert resp.status_code == 403

    def test_rejects_empty_path(self, client, mirror_dir):
        """空路径应返回 400 或 422"""
        resp = client.get("/api/assets?path=")
        assert resp.status_code in (400, 422)  # FastAPI 参数校验

    def test_accepts_allowed_image_types(self, client, mirror_dir):
        """应接受所有允许的图片类型"""
        img_dir = mirror_dir / "test"
        img_dir.mkdir()

        for ext in [".jpg", ".jpeg", ".png", ".webp", ".svg"]:
            filename = f"image{ext}"
            (img_dir / filename).write_bytes(b"fake-image")
            resp = client.get(f"/api/assets?path=test/{filename}")
            assert resp.status_code == 200, f"应接受 {ext}"

    def test_absolute_path_under_mirror(self, client, mirror_dir):
        """mirror 下的绝对路径应能访问"""
        poster = mirror_dir / "115" / "CLANNAD"
        poster.mkdir(parents=True)
        (poster / "poster.jpg").write_bytes(b"fake-jpg")

        abs_path = str(poster / "poster.jpg")
        resp = client.get(f"/api/assets?path={abs_path}")
        assert resp.status_code == 200

    def test_cache_control_header(self, client, mirror_dir):
        """应设置 Cache-Control 头"""
        poster = mirror_dir / "test"
        poster.mkdir()
        (poster / "poster.jpg").write_bytes(b"fake-jpg")

        resp = client.get("/api/assets?path=test/poster.jpg")
        assert resp.status_code == 200
        assert "Cache-Control" in resp.headers


@pytest.mark.parametrize("url", [
    "https://image.tmdb.org/t/p/w500/poster.jpg",
    "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/example.jpg",
])
def test_remote_image_proxy_accepts_only_known_cdn_paths(url):
    _validate_remote_asset_url(url)


@pytest.mark.parametrize("url", [
    "http://image.tmdb.org/t/p/w500/poster.jpg",
    "https://image.tmdb.org:8443/t/p/w500/poster.jpg",
    "https://127.0.0.1/private.jpg",
    "https://s4.anilist.co/not-anilist-cdn/example.jpg",
])
def test_remote_image_proxy_rejects_untrusted_targets(url):
    with pytest.raises(ValueError):
        _validate_remote_asset_url(url)
