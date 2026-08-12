# -*- coding: utf-8 -*-
"""TMDB API 客户端（按 TMDB 规范重构）

规范来源：KumiPlayer/docx/technical/TMDB规范.txt

主要改进：
1. get_configuration + 图片 URL 不再硬编码 w780
2. append_to_response 合并请求
3. include_image_language 图片语言 fallback
4. 401/403/429/5xx 错误处理
5. 图片选择算法：poster/fanart/clearlogo/still
6. TV season / episode detail 能力
7. 保持 mock 测试可用
"""

import logging
import os
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import load_config

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.themoviedb.org/3"

# 默认图片尺寸
_DEFAULT_POSTER_SIZE = "w780"
_DEFAULT_BACKDROP_SIZE = "original"
_DEFAULT_LOGO_SIZE = "original"
_DEFAULT_STILL_SIZE = "w300"

# 图片语言优先级
_IMAGE_LANGUAGES = "zh-CN,zh,null,en,ja"

_GLOBAL_RATE_LIMIT_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0


class TMDBClientError(Exception):
    """TMDB 客户端基础异常"""


class TMDBAuthError(TMDBClientError):
    """认证失败（401/403）"""


class TMDBRateLimitError(TMDBClientError):
    """速率限制（429）"""


class TMDBClient:
    """TMDB API 客户端

    支持 mock：通过构造函数注入 _http_client。
    """

    def __init__(
        self,
        bearer_token: Optional[str] = None,
        language: Optional[str] = None,
        rate_limit: Optional[float] = None,
        max_retries: Optional[int] = None,
        timeout: Optional[int] = None,
        _http_client: Optional[Any] = None,
    ):
        config = load_config()
        self._token = bearer_token if bearer_token is not None else config.tmdb_bearer_token
        self._language = language or config.tmdb_language or "zh-CN"
        self._rate_limit = rate_limit if rate_limit is not None else config.tmdb_rate_limit
        raw_max_retries = max_retries if max_retries is not None else config.tmdb_max_retries
        self._max_retries = max(1, min(int(raw_max_retries or 2), 2))
        raw_timeout = timeout if timeout is not None else config.tmdb_timeout
        self._timeout = max(3, min(int(raw_timeout or 10), 12))
        self._client = _http_client
        self._owned_client: Optional[httpx.Client] = None
        self._last_request_time = 0.0
        self._response_cache: Dict[Tuple[str, str, Tuple[Tuple[str, str], ...]], dict] = {}

        # configuration 缓存
        self._config_cache: Optional[dict] = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._owned_client is None:
            config = load_config()
            proxy = config.proxy_url or None
            if proxy:
                self._owned_client = httpx.Client(
                    timeout=self._timeout,
                    proxy=proxy,
                )
            else:
                self._owned_client = httpx.Client(timeout=self._timeout)
        return self._owned_client

    def close(self) -> None:
        if self._owned_client is not None:
            self._owned_client.close()
            self._owned_client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "accept": "application/json",
        }

    def _rate_limit_wait(self):
        """进程级 TMDB 节流，避免批量刮削时多个 client 同时打爆外部服务。"""
        global _GLOBAL_LAST_REQUEST_TIME
        interval = max(0.0, float(self._rate_limit or 0.0))
        if interval <= 0:
            return
        with _GLOBAL_RATE_LIMIT_LOCK:
            elapsed = time.time() - _GLOBAL_LAST_REQUEST_TIME
            if elapsed < interval:
                time.sleep(interval - elapsed)
            _GLOBAL_LAST_REQUEST_TIME = time.time()
            self._last_request_time = _GLOBAL_LAST_REQUEST_TIME

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """发送请求，支持重试和错误处理

        错误处理策略（按 TMDB 规范）：
        - 401/403：立即失败，提示 token/权限问题
        - 404：记录候选不存在
        - 422：参数错误，记录 query 和参数
        - 429：读取 Retry-After；没有则等待 30 秒
        - 500/502/503/504：指数退避重试
        """
        if not self._token:
            raise TMDBAuthError("未配置 tmdb_bearer_token")

        url = f"{_BASE_URL}{path}"
        headers = self._headers()
        cache_key = self._cache_key(method, path, kwargs)
        if method.upper() == "GET" and cache_key in self._response_cache:
            return deepcopy(self._response_cache[cache_key])

        last_error = None
        for attempt in range(self._max_retries):
            self._rate_limit_wait()
            try:
                client = self._get_client()
                resp = client.request(method, url, headers=headers, **kwargs)

                # 成功
                if resp.status_code == 200:
                    data = resp.json()
                    if method.upper() == "GET":
                        self._response_cache[cache_key] = deepcopy(data)
                    return data

                # 401/403：立即失败
                if resp.status_code in (401, 403):
                    raise TMDBAuthError(
                        f"TMDB 认证失败 ({resp.status_code}): "
                        f"请检查 tmdb_bearer_token 是否有效"
                    )

                # 404：资源不存在
                if resp.status_code == 404:
                    raise TMDBClientError(f"TMDB 资源不存在: {path}")

                # 422：参数错误
                if resp.status_code == 422:
                    error_msg = resp.text[:200] if resp.text else "参数错误"
                    raise TMDBClientError(f"TMDB 参数错误 (422): {error_msg}")

                # 429：速率限制。无 Retry-After 时不再默认等待 30 秒，
                # 否则前端会长时间无新日志，看起来像刮削卡死。
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait_time = int(retry_after) if retry_after else 3
                    wait_time = max(1, min(wait_time, 5))
                    logger.warning(f"TMDB 429 速率限制，等待 {wait_time} 秒")
                    if attempt < self._max_retries - 1:
                        time.sleep(wait_time)
                        continue
                    raise TMDBRateLimitError(f"TMDB 速率限制，已重试 {self._max_retries} 次")

                # 5xx：指数退避重试
                if resp.status_code >= 500:
                    wait_time = 2 ** attempt
                    logger.warning(f"TMDB {resp.status_code}，{wait_time}秒后重试")
                    if attempt < self._max_retries - 1:
                        time.sleep(wait_time)
                        continue
                    raise TMDBClientError(f"TMDB 服务端错误 ({resp.status_code})")

                # 其他错误
                raise TMDBClientError(f"TMDB 请求失败 ({resp.status_code}): {resp.text[:200]}")

            except httpx.TimeoutException:
                last_error = TMDBClientError("TMDB 请求超时")
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_error
            except httpx.ConnectError as e:
                message = str(e)
                if "CERTIFICATE_VERIFY_FAILED" in message or "certificate verify failed" in message:
                    if attempt < self._max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise TMDBClientError(
                        "TMDB SSL 证书校验失败：请检查代理/VPN、DNS、杀毒软件 HTTPS 扫描或网络拦截。"
                        "当前连接拿到的证书与 api.themoviedb.org 不匹配。"
                    )
                last_error = TMDBClientError(f"TMDB 网络连接失败: {e}")
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_error
            except (TMDBAuthError, TMDBRateLimitError, TMDBClientError):
                raise
            except Exception as e:
                last_error = TMDBClientError(f"TMDB 请求异常: {e}")
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_error

        raise TMDBClientError(f"TMDB 请求失败: {path}")

    @staticmethod
    def _cache_key(method: str, path: str, kwargs: dict) -> Tuple[str, str, Tuple[Tuple[str, str], ...]]:
        params = kwargs.get("params") or {}
        normalized_params = tuple(sorted((str(k), str(v)) for k, v in params.items()))
        return (method.upper(), path, normalized_params)

    # ============================================================
    # 认证与配置
    # ============================================================

    def test_authentication(self) -> Tuple[bool, str]:
        """测试 TMDB token 是否有效

        返回: (success, message)
        """
        try:
            data = self._request("GET", "/authentication")
            if data.get("success"):
                return True, "TMDB 认证成功"
            return False, "TMDB 认证失败: token 无效"
        except TMDBAuthError as e:
            return False, str(e)
        except Exception as e:
            return False, f"TMDB 认证测试失败: {e}"

    def get_configuration(self) -> dict:
        """获取 TMDB 配置（图片 base_url 和尺寸）

        结果会缓存，避免重复请求。
        """
        if self._config_cache is not None:
            return self._config_cache

        data = self._request("GET", "/configuration")
        self._config_cache = data
        return data

    def get_image_base_url(self) -> str:
        """获取图片 secure_base_url

        默认返回 https://image.tmdb.org/t/p/
        """
        try:
            config = self.get_configuration()
            return config.get("images", {}).get("secure_base_url", "https://image.tmdb.org/t/p/")
        except Exception:
            return "https://image.tmdb.org/t/p/"

    def build_image_url(self, file_path: str, size: str = "w780") -> str:
        """构建图片完整 URL

        参数:
            file_path: 图片路径，如 /abc123.jpg
            size: 图片尺寸，如 w780, w1280, original

        返回:
            完整图片 URL
        """
        if not file_path:
            return ""
        base_url = self.get_image_base_url()
        return f"{base_url}{size}{file_path}"

    # ============================================================
    # 搜索
    # ============================================================

    def search_tv(self, query: str, year: Optional[int] = None) -> List[dict]:
        """搜索 TV 剧集"""
        params = {
            "query": query,
            "language": self._language,
            "include_adult": "false",
        }
        if year:
            params["first_air_date_year"] = year
        data = self._request("GET", "/search/tv", params=params)
        return data.get("results", [])

    def search_movie(self, query: str, year: Optional[int] = None) -> List[dict]:
        """搜索电影"""
        params = {
            "query": query,
            "language": self._language,
            "include_adult": "false",
        }
        if year:
            params["primary_release_year"] = year
        data = self._request("GET", "/search/movie", params=params)
        return data.get("results", [])

    # ============================================================
    # Details（使用 append_to_response）
    # ============================================================

    def get_tv_detail(self, tmdb_id: int) -> dict:
        """获取 TV 详情（含图片、演职员、外部ID、翻译）

        使用 append_to_response 合并请求。
        """
        params = {
            "language": self._language,
            "append_to_response": "images,credits,external_ids,alternative_titles,translations,content_ratings",
            "include_image_language": _IMAGE_LANGUAGES,
        }
        return self._request("GET", f"/tv/{tmdb_id}", params=params)

    def get_movie_detail(self, tmdb_id: int) -> dict:
        """获取电影详情（含图片、演职员、外部ID、翻译）

        使用 append_to_response 合并请求。
        """
        params = {
            "language": self._language,
            "append_to_response": "images,credits,external_ids,alternative_titles,translations,release_dates",
            "include_image_language": _IMAGE_LANGUAGES,
        }
        return self._request("GET", f"/movie/{tmdb_id}", params=params)

    def get_tv_season_detail(self, tmdb_id: int, season_number: int) -> dict:
        """获取 TV 季详情（含图片、演职员、翻译）"""
        params = {
            "language": self._language,
            "append_to_response": "images,credits,external_ids,translations",
            "include_image_language": _IMAGE_LANGUAGES,
        }
        return self._request("GET", f"/tv/{tmdb_id}/season/{season_number}", params=params)

    def get_tv_season_episodes(self, tmdb_id: int, season_number: int) -> dict:
        """轻量获取一季的分集列表，避免为批量分集刮削附加大体积资源。"""
        return self._request(
            "GET",
            f"/tv/{tmdb_id}/season/{season_number}",
            params={"language": self._language},
        )

    def get_tv_episode_detail(
        self,
        tmdb_id: int,
        season_number: int,
        episode_number: int,
    ) -> dict:
        """获取 TV 单集详情（含图片、演职员、翻译）"""
        params = {
            "language": self._language,
            "append_to_response": "images,credits,external_ids,translations",
            "include_image_language": _IMAGE_LANGUAGES,
        }
        return self._request(
            "GET",
            f"/tv/{tmdb_id}/season/{season_number}/episode/{episode_number}",
            params=params,
        )

    # ============================================================
    # 图片选择算法
    # ============================================================

    @staticmethod
    def _score_poster(img: dict) -> float:
        """海报评分

        优先级：
        1. 语言：zh/zh-CN > null > ja > en
        2. 宽高比接近 2:3（aspect_ratio ~ 0.666）
        3. vote_average
        4. vote_count
        5. 分辨率
        """
        score = 0.0
        lang = img.get("iso_639_1")

        # 语言加分
        if lang in ("zh", "zh-CN", "zh-TW"):
            score += 50
        elif lang is None:
            score += 35
        elif lang == "ja":
            score += 25
        elif lang == "en":
            score += 20

        # 宽高比（海报约 2:3）
        aspect = img.get("aspect_ratio") or 0
        if 0.62 <= aspect <= 0.72:
            score += 20

        # 投票评分
        score += min((img.get("vote_average") or 0) * 3, 30)
        score += min((img.get("vote_count") or 0), 20)

        # 分辨率
        score += min((img.get("width") or 0) / 100, 10)

        return score

    @staticmethod
    def _score_backdrop(img: dict) -> float:
        """背景图评分

        优先级：
        1. 无语言（backdrop 通常无语言）
        2. 宽高比 16:9（aspect_ratio ~ 1.78）
        3. vote_average
        4. vote_count
        5. 分辨率
        """
        score = 0.0
        lang = img.get("iso_639_1")

        # 语言加分（backdrop 优先无语言）
        if lang is None:
            score += 50
        elif lang in ("zh", "en", "ja"):
            score += 10

        # 宽高比（backdrop 约 16:9）
        aspect = img.get("aspect_ratio") or 0
        if 1.70 <= aspect <= 1.90:
            score += 20

        # 投票评分
        score += min((img.get("vote_average") or 0) * 3, 30)
        score += min((img.get("vote_count") or 0), 20)

        # 分辨率
        score += min((img.get("width") or 0) / 200, 10)

        return score

    @staticmethod
    def _score_logo(img: dict) -> float:
        """Logo 评分

        优先级：
        1. 语言：zh > ja > en > null
        2. SVG 格式加分
        3. vote_average
        """
        score = 0.0
        lang = img.get("iso_639_1")

        # 语言加分
        if lang in ("zh", "zh-CN", "zh-TW"):
            score += 50
        elif lang == "ja":
            score += 35
        elif lang == "en":
            score += 25
        elif lang is None:
            score += 20

        # SVG 加分
        file_type = img.get("file_type", "")
        if file_type == ".svg":
            score += 20

        # 投票评分
        score += min((img.get("vote_average") or 0) * 3, 30)
        score += min((img.get("vote_count") or 0), 20)

        return score

    @staticmethod
    def _score_still(img: dict) -> float:
        """剧照评分

        优先级：
        1. 无语言
        2. vote_average
        3. 分辨率
        """
        score = 0.0
        lang = img.get("iso_639_1")

        if lang is None:
            score += 50
        elif lang in ("zh", "en", "ja"):
            score += 10

        score += min((img.get("vote_average") or 0) * 3, 30)
        score += min((img.get("vote_count") or 0), 20)
        score += min((img.get("width") or 0) / 100, 10)

        return score

    def select_best_poster(self, images: dict) -> Optional[str]:
        """从图片列表中选择最佳海报

        返回 file_path 或 None
        """
        posters = images.get("posters", [])
        if not posters:
            return None
        best = max(posters, key=self._score_poster)
        return best.get("file_path")

    def select_best_backdrop(self, images: dict) -> Optional[str]:
        """从图片列表中选择最佳背景图

        返回 file_path 或 None
        """
        backdrops = images.get("backdrops", [])
        if not backdrops:
            return None
        best = max(backdrops, key=self._score_backdrop)
        return best.get("file_path")

    def select_best_logo(self, images: dict) -> Optional[str]:
        """从图片列表中选择最佳 logo

        返回 file_path 或 None
        """
        logos = images.get("logos", [])
        if not logos:
            return None
        best = max(logos, key=self._score_logo)
        return best.get("file_path")

    def select_best_still(self, images: dict) -> Optional[str]:
        """从图片列表中选择最佳剧照

        返回 file_path 或 None
        """
        stills = images.get("stills", [])
        if not stills:
            return None
        best = max(stills, key=self._score_still)
        return best.get("file_path")

    # ============================================================
    # 图片下载
    # ============================================================

    def download_image(self, file_path: str, dest: Path, size: Optional[str] = None) -> bool:
        """下载图片

        参数:
            file_path: TMDB 图片路径，如 /abc123.jpg
            dest: 本地保存路径
            size: 图片尺寸，None 时自动选择默认尺寸

        返回:
            True 成功，False 失败
        """
        if not file_path:
            return False

        # 自动选择尺寸
        if size is None:
            # 根据目标文件名推断
            dest_name = dest.name.lower()
            if "poster" in dest_name:
                size = _DEFAULT_POSTER_SIZE
            elif "fanart" in dest_name or "backdrop" in dest_name:
                size = _DEFAULT_BACKDROP_SIZE
            elif "logo" in dest_name or "clearlogo" in dest_name:
                size = _DEFAULT_LOGO_SIZE
            elif "still" in dest_name or "thumb" in dest_name:
                size = _DEFAULT_STILL_SIZE
            else:
                size = _DEFAULT_POSTER_SIZE

        url = self.build_image_url(file_path, size)
        if not url:
            return False

        try:
            client = self._get_client()
            resp = client.get(url, timeout=self._timeout)
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp_dest = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
            try:
                temp_dest.write_bytes(resp.content)
                os.replace(temp_dest, dest)
            finally:
                temp_dest.unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.warning(f"图片下载失败 {url}: {e}")
            return False
