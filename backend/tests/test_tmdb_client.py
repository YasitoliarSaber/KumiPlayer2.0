# -*- coding: utf-8 -*-
"""TMDB 客户端测试（mock，不真实请求 TMDB）"""

import pytest
from unittest.mock import MagicMock

from app.scrape.tmdb_client import (
    TMDBAuthError,
    TMDBClient,
    TMDBClientError,
    TMDBRateLimitError,
)


@pytest.fixture
def mock_http():
    """创建 mock httpx 客户端"""
    return MagicMock()


@pytest.fixture
def client(mock_http):
    """创建使用 mock 的 TMDBClient"""
    return TMDBClient(
        bearer_token="test-token",
        language="zh-CN",
        rate_limit=0,  # 测试不限速
        max_retries=2,
        timeout=5,
        _http_client=mock_http,
    )


def _mock_response(status_code=200, json_data=None, headers=None):
    """创建 mock 响应"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    resp.text = str(json_data) if json_data else ""
    return resp


# ============================================================
# 认证测试
# ============================================================

class TestAuthentication:
    """测试认证"""

    def test_auth_success(self, client, mock_http):
        """认证成功"""
        mock_http.request.return_value = _mock_response(200, {"success": True})
        ok, msg = client.test_authentication()
        assert ok is True
        assert "成功" in msg

    def test_auth_invalid_token(self, client, mock_http):
        """token 无效"""
        mock_http.request.return_value = _mock_response(401, {"status_code": 7})
        ok, msg = client.test_authentication()
        assert ok is False
        assert "认证失败" in msg

    def test_no_token_returns_failure(self, mock_http):
        """未配置 token 时应返回失败"""
        c = TMDBClient(bearer_token="", _http_client=mock_http)
        ok, msg = c.test_authentication()
        assert ok is False
        assert "未配置" in msg


# ============================================================
# Configuration 测试
# ============================================================

class TestConfiguration:
    """测试配置获取"""

    def test_get_configuration(self, client, mock_http):
        """获取配置"""
        config_data = {
            "images": {
                "base_url": "http://image.tmdb.org/t/p/",
                "secure_base_url": "https://image.tmdb.org/t/p/",
                "poster_sizes": ["w92", "w154", "w185", "w342", "w500", "w780", "original"],
                "backdrop_sizes": ["w300", "w780", "w1280", "original"],
                "logo_sizes": ["w45", "w92", "w154", "w185", "w300", "w500", "original"],
                "still_sizes": ["w92", "w185", "w300", "original"],
            }
        }
        mock_http.request.return_value = _mock_response(200, config_data)

        result = client.get_configuration()
        assert result["images"]["secure_base_url"] == "https://image.tmdb.org/t/p/"

    def test_configuration_cached(self, client, mock_http):
        """配置应被缓存"""
        config_data = {"images": {"secure_base_url": "https://image.tmdb.org/t/p/"}}
        mock_http.request.return_value = _mock_response(200, config_data)

        # 第一次请求
        client.get_configuration()
        # 第二次请求应使用缓存
        client.get_configuration()

        # 只应请求一次 configuration
        assert mock_http.request.call_count == 1

    def test_build_image_url(self, client, mock_http):
        """构建图片 URL"""
        config_data = {"images": {"secure_base_url": "https://image.tmdb.org/t/p/"}}
        mock_http.request.return_value = _mock_response(200, config_data)

        url = client.build_image_url("/abc123.jpg", "w780")
        assert url == "https://image.tmdb.org/t/p/w780/abc123.jpg"

    def test_build_image_url_empty_path(self, client):
        """空路径返回空字符串"""
        assert client.build_image_url("", "w780") == ""
        assert client.build_image_url(None, "w780") == ""


# ============================================================
# 搜索测试
# ============================================================

class TestSearch:
    """测试搜索"""

    def test_search_tv(self, client, mock_http):
        """搜索 TV"""
        mock_http.request.return_value = _mock_response(200, {
            "results": [{"id": 1, "name": "Test Show"}]
        })
        results = client.search_tv("test", year=2024)
        assert len(results) == 1
        assert results[0]["name"] == "Test Show"

    def test_search_movie(self, client, mock_http):
        """搜索电影"""
        mock_http.request.return_value = _mock_response(200, {
            "results": [{"id": 2, "title": "Test Movie"}]
        })
        results = client.search_movie("test", year=2024)
        assert len(results) == 1
        assert results[0]["title"] == "Test Movie"

    def test_search_no_results(self, client, mock_http):
        """搜索无结果"""
        mock_http.request.return_value = _mock_response(200, {"results": []})
        results = client.search_tv("nonexistent")
        assert results == []

    def test_rate_limit_is_shared_across_clients(self, monkeypatch):
        """不同 TMDBClient 实例也应共享节流，避免批量刮削突发请求。"""
        import app.scrape.tmdb_client as tmdb_module

        first_http = MagicMock()
        second_http = MagicMock()
        first_http.request.return_value = _mock_response(200, {"results": []})
        second_http.request.return_value = _mock_response(200, {"results": []})

        sleeps = []
        times = iter([100.0, 100.0, 100.2, 101.0])
        monkeypatch.setattr(tmdb_module, "_GLOBAL_LAST_REQUEST_TIME", 0.0)
        monkeypatch.setattr(tmdb_module.time, "time", lambda: next(times))
        monkeypatch.setattr(tmdb_module.time, "sleep", lambda seconds: sleeps.append(seconds))

        first = TMDBClient(bearer_token="t", rate_limit=1, _http_client=first_http)
        second = TMDBClient(bearer_token="t", rate_limit=1, _http_client=second_http)

        first.search_tv("a")
        second.search_tv("b")

        assert sleeps == [pytest.approx(0.8)]


# ============================================================
# Details 测试（append_to_response）
# ============================================================

class TestDetails:
    """测试详情获取"""

    def test_tv_detail_with_append(self, client, mock_http):
        """TV 详情应使用 append_to_response"""
        mock_http.request.return_value = _mock_response(200, {
            "id": 123,
            "name": "Test Show",
            "images": {"posters": [], "backdrops": []},
            "credits": {"cast": []},
            "external_ids": {"imdb_id": "tt123"},
        })

        result = client.get_tv_detail(123)
        assert result["id"] == 123
        assert "images" in result
        assert "credits" in result

        # 验证请求参数包含 append_to_response
        call_args = mock_http.request.call_args
        params = call_args[1].get("params", {})
        assert "append_to_response" in params
        assert "images" in params["append_to_response"]

    def test_movie_detail_with_append(self, client, mock_http):
        """电影详情应使用 append_to_response"""
        mock_http.request.return_value = _mock_response(200, {
            "id": 456,
            "title": "Test Movie",
        })

        result = client.get_movie_detail(456)
        assert result["id"] == 456

    def test_season_detail(self, client, mock_http):
        """获取季详情"""
        mock_http.request.return_value = _mock_response(200, {
            "season_number": 1,
            "episodes": [{"episode_number": 1}],
        })

        result = client.get_tv_season_detail(123, 1)
        assert result["season_number"] == 1

    def test_lightweight_season_episodes_does_not_append_large_payloads(self, client, mock_http):
        """批量取分集只请求基础季详情，避免图片和演职员放大网络失败。"""
        mock_http.request.return_value = _mock_response(200, {
            "season_number": 1,
            "episodes": [{"episode_number": 1}],
        })

        result = client.get_tv_season_episodes(123, 1)

        assert result["episodes"][0]["episode_number"] == 1
        params = mock_http.request.call_args[1].get("params", {})
        assert "append_to_response" not in params

    def test_episode_detail(self, client, mock_http):
        """获取单集详情"""
        mock_http.request.return_value = _mock_response(200, {
            "episode_number": 1,
            "name": "Pilot",
        })

        result = client.get_tv_episode_detail(123, 1, 1)
        assert result["episode_number"] == 1
        assert result["name"] == "Pilot"


# ============================================================
# 错误处理测试
# ============================================================

class TestErrorHandling:
    """测试错误处理"""

    def test_401_raises_auth_error(self, client, mock_http):
        """401 应抛认证异常"""
        mock_http.request.return_value = _mock_response(401)
        with pytest.raises(TMDBAuthError, match="认证失败"):
            client.search_tv("test")

    def test_403_raises_auth_error(self, client, mock_http):
        """403 应抛认证异常"""
        mock_http.request.return_value = _mock_response(403)
        with pytest.raises(TMDBAuthError, match="认证失败"):
            client.search_tv("test")

    def test_404_raises_client_error(self, client, mock_http):
        """404 应抛资源不存在异常"""
        mock_http.request.return_value = _mock_response(404)
        with pytest.raises(TMDBClientError, match="不存在"):
            client.get_tv_detail(999)

    def test_429_raises_rate_limit_error(self, client, mock_http):
        """429 应抛速率限制异常"""
        mock_http.request.return_value = _mock_response(
            429, headers={"Retry-After": "5"}
        )
        with pytest.raises(TMDBRateLimitError, match="速率限制"):
            client.search_tv("test")

    def test_429_no_retry_after(self, client, mock_http):
        """429 无 Retry-After 时使用默认等待"""
        mock_http.request.return_value = _mock_response(429)
        with pytest.raises(TMDBRateLimitError):
            client.search_tv("test")

    def test_500_retries_then_raises(self, client, mock_http):
        """500 应重试后抛异常"""
        mock_http.request.return_value = _mock_response(500)
        with pytest.raises(TMDBClientError, match="服务端错误"):
            client.search_tv("test")
        # 应重试 max_retries 次
        assert mock_http.request.call_count == 2  # max_retries=2

    def test_timeout_retries(self, client, mock_http):
        """超时应重试"""
        mock_http.request.side_effect = Exception("timeout")
        with pytest.raises(TMDBClientError, match="异常"):
            client.search_tv("test")
        assert mock_http.request.call_count == 2

    def test_ssl_certificate_error_is_user_friendly(self, client, mock_http):
        """SSL 证书错误应提示代理/DNS/网络拦截"""
        import httpx

        mock_http.request.side_effect = httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "Hostname mismatch, certificate is not valid for 'api.themoviedb.org'."
        )
        with pytest.raises(TMDBClientError, match="SSL 证书校验失败"):
            client.search_tv("test")


# ============================================================
# 图片选择算法测试
# ============================================================

class TestImageSelection:
    """测试图片选择算法"""

    def test_select_best_poster_chinese(self, client):
        """中文海报优先"""
        images = {
            "posters": [
                {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 8, "width": 500},
                {"file_path": "/zh.jpg", "iso_639_1": "zh", "vote_average": 7, "width": 500},
                {"file_path": "/null.jpg", "iso_639_1": None, "vote_average": 9, "width": 500},
            ]
        }
        best = client.select_best_poster(images)
        assert best == "/zh.jpg"

    def test_select_best_poster_no_chinese(self, client):
        """无中文时选 null 语言"""
        images = {
            "posters": [
                {"file_path": "/en.jpg", "iso_639_1": "en", "vote_average": 8, "width": 500},
                {"file_path": "/null.jpg", "iso_639_1": None, "vote_average": 7, "width": 500},
            ]
        }
        best = client.select_best_poster(images)
        assert best == "/null.jpg"

    def test_select_best_backdrop_no_language(self, client):
        """背景图优先无语言"""
        images = {
            "backdrops": [
                {"file_path": "/zh.jpg", "iso_639_1": "zh", "vote_average": 8, "width": 1920},
                {"file_path": "/null.jpg", "iso_639_1": None, "vote_average": 7, "width": 1920},
            ]
        }
        best = client.select_best_backdrop(images)
        assert best == "/null.jpg"

    def test_select_best_logo_chinese(self, client):
        """Logo 优先中文"""
        images = {
            "logos": [
                {"file_path": "/en.png", "iso_639_1": "en", "vote_average": 8, "file_type": ".png"},
                {"file_path": "/zh.png", "iso_639_1": "zh", "vote_average": 7, "file_type": ".png"},
            ]
        }
        best = client.select_best_logo(images)
        assert best == "/zh.png"

    def test_select_best_logo_svg(self, client):
        """SVG logo 优先"""
        images = {
            "logos": [
                {"file_path": "/logo.png", "iso_639_1": "en", "vote_average": 8, "file_type": ".png"},
                {"file_path": "/logo.svg", "iso_639_1": "en", "vote_average": 7, "file_type": ".svg"},
            ]
        }
        best = client.select_best_logo(images)
        assert best == "/logo.svg"

    def test_select_empty_images(self, client):
        """空图片列表返回 None"""
        assert client.select_best_poster({}) is None
        assert client.select_best_backdrop({}) is None
        assert client.select_best_logo({}) is None
        assert client.select_best_still({}) is None

    def test_select_best_still(self, client):
        """选择最佳剧照"""
        images = {
            "stills": [
                {"file_path": "/still1.jpg", "iso_639_1": "en", "vote_average": 5, "width": 300},
                {"file_path": "/still2.jpg", "iso_639_1": None, "vote_average": 8, "width": 500},
            ]
        }
        best = client.select_best_still(images)
        assert best == "/still2.jpg"


# ============================================================
# 图片下载测试
# ============================================================

class TestImageDownload:
    """测试图片下载"""

    def test_download_poster(self, client, mock_http, tmp_path):
        """下载海报"""
        # mock configuration
        config_resp = _mock_response(200, {
            "images": {"secure_base_url": "https://image.tmdb.org/t/p/"}
        })
        # mock 图片下载
        img_resp = _mock_response(200)
        img_resp.content = b"fake-image-data"

        mock_http.request.return_value = config_resp
        mock_http.get.return_value = img_resp

        dest = tmp_path / "poster.jpg"
        result = client.download_image("/abc123.jpg", dest)
        assert result is True
        assert dest.exists()
        assert dest.read_bytes() == b"fake-image-data"

    def test_download_auto_size_poster(self, client, mock_http, tmp_path):
        """自动选择海报尺寸"""
        config_resp = _mock_response(200, {
            "images": {"secure_base_url": "https://image.tmdb.org/t/p/"}
        })
        img_resp = _mock_response(200)
        img_resp.content = b"fake"

        mock_http.request.return_value = config_resp
        mock_http.get.return_value = img_resp

        dest = tmp_path / "poster.jpg"
        client.download_image("/abc.jpg", dest)

        # 验证 URL 包含 w780
        call_args = mock_http.get.call_args
        url = call_args[0][0]
        assert "w780" in url

    def test_download_auto_size_fanart(self, client, mock_http, tmp_path):
        """自动选择背景图尺寸"""
        config_resp = _mock_response(200, {
            "images": {"secure_base_url": "https://image.tmdb.org/t/p/"}
        })
        img_resp = _mock_response(200)
        img_resp.content = b"fake"

        mock_http.request.return_value = config_resp
        mock_http.get.return_value = img_resp

        dest = tmp_path / "fanart.jpg"
        client.download_image("/abc.jpg", dest)

        call_args = mock_http.get.call_args
        url = call_args[0][0]
        assert "/original/" in url

    def test_download_empty_path(self, client, tmp_path):
        """空路径返回失败"""
        dest = tmp_path / "test.jpg"
        result = client.download_image("", dest)
        assert result is False

    def test_download_failure(self, client, mock_http, tmp_path):
        """下载失败返回 False"""
        config_resp = _mock_response(200, {
            "images": {"secure_base_url": "https://image.tmdb.org/t/p/"}
        })
        mock_http.request.return_value = config_resp
        mock_http.get.side_effect = Exception("network error")

        dest = tmp_path / "test.jpg"
        result = client.download_image("/abc.jpg", dest)
        assert result is False

    def test_download_failure_preserves_existing_image(self, client, mock_http, tmp_path):
        """刷新图片失败时保留旧文件，不能留下半写入的临时文件。"""
        config_resp = _mock_response(200, {
            "images": {"secure_base_url": "https://image.tmdb.org/t/p/"}
        })
        mock_http.request.return_value = config_resp
        mock_http.get.side_effect = Exception("network error")

        dest = tmp_path / "poster.jpg"
        dest.write_bytes(b"existing-image")

        result = client.download_image("/replacement.jpg", dest)

        assert result is False
        assert dest.read_bytes() == b"existing-image"
        assert list(tmp_path.glob(".*.tmp")) == []
