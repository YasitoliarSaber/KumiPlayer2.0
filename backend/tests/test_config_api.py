# -*- coding: utf-8 -*-
"""Config API 测试"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import AppConfig, invalidate_config_cache, load_config, save_config


@pytest.fixture
def client():
    """创建测试客户端"""
    invalidate_config_cache()
    return TestClient(app)


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    """使用临时配置文件"""
    config_file = tmp_path / "config.json"
    monkeypatch.setattr("app.core.config.CONFIG_FILE", config_file)
    invalidate_config_cache()
    yield config_file
    invalidate_config_cache()


# ============================================================
# GET /api/config
# ============================================================

class TestGetConfig:
    """测试获取配置"""

    def test_returns_all_fields(self, client, temp_config):
        """应返回所有配置字段"""
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        # 检查关键字段存在
        assert "mpv_path" in data
        assert "mirror_dir" in data
        assert "tmdb_language" in data
        assert "server_port" in data
        assert "auto_play_next_episode" in data
        assert "heartbeat_enabled" in data

    def test_sensitive_fields_masked(self, client, temp_config):
        """敏感字段应脱敏"""
        # 写入带 token 的配置
        config = AppConfig(
            tmdb_bearer_token="sk-1234567890abcdef",
            deepseek_api_key="dk-abcdef1234567890",
        )
        save_config(config)

        resp = client.get("/api/config")
        data = resp.json()

        # token 应被脱敏
        assert data["tmdb_bearer_token"] != "sk-1234567890abcdef"
        assert "..." in data["tmdb_bearer_token"]

        assert data["deepseek_api_key"] != "dk-abcdef1234567890"
        assert "..." in data["deepseek_api_key"]

    def test_empty_sensitive_fields_not_masked(self, client, temp_config):
        """空敏感字段不应脱敏"""
        config = AppConfig(tmdb_bearer_token="", deepseek_api_key="")
        save_config(config)

        resp = client.get("/api/config")
        data = resp.json()

        assert data["tmdb_bearer_token"] == ""
        assert data["deepseek_api_key"] == ""

    def test_empty_mpv_path_stays_empty_until_user_selects_external_player(self, client, temp_config):
        """安装包不携带 mpv.exe，首次启动必须由用户明确选择播放器。"""
        save_config(AppConfig(mpv_path=""))

        resp = client.get("/api/config")

        assert resp.status_code == 200
        assert resp.json()["mpv_path"] == ""
        assert json.loads(temp_config.read_text(encoding="utf-8"))["mpv_path"] == ""

    @patch("app.api.config.get_default_mirror_dir")
    def test_empty_mirror_path_prefills_install_default_without_saving_it(self, get_default, client, temp_config, tmp_path):
        default_mirror = tmp_path / "Installed" / "mirror"
        get_default.return_value = default_mirror

        resp = client.get("/api/config")

        assert resp.status_code == 200
        assert resp.json()["mirror_dir"] == str(default_mirror)
        assert not temp_config.exists()

    @patch("app.api.config.get_default_mirror_dir")
    def test_existing_mirror_path_is_not_overwritten_by_install_default(self, get_default, client, temp_config, tmp_path):
        custom_mirror = tmp_path / "custom-mirror"
        save_config(AppConfig(mirror_dir=str(custom_mirror)))
        get_default.return_value = tmp_path / "Installed" / "mirror"

        resp = client.get("/api/config")

        assert resp.status_code == 200
        assert resp.json()["mirror_dir"] == str(custom_mirror)

    @patch("app.api.config.get_default_mirror_dir")
    def test_completed_legacy_profile_without_mirror_keeps_existing_fallback(self, get_default, client, temp_config, tmp_path):
        save_config(AppConfig(setup_completed=True, setup_version=1, mirror_dir=""))
        get_default.return_value = tmp_path / "Installed" / "mirror"

        resp = client.get("/api/config")

        assert resp.status_code == 200
        assert resp.json()["mirror_dir"] == ""


class TestFirstRunSetup:
    def test_new_install_starts_in_setup_mode(self, temp_config):
        config = load_config(force_reload=True)
        assert config.setup_completed is False
        assert config.setup_version == 0

    def test_legacy_config_is_migrated_without_interrupting_user(self, temp_config):
        temp_config.write_text(json.dumps({"mpv_path": "C:/existing/mpv.exe"}), encoding="utf-8")
        config = load_config(force_reload=True)
        assert config.setup_completed is True
        assert config.setup_version >= 1

    @patch("app.api.config.check_mpv_runtime")
    def test_setup_complete_validates_and_saves_zero_start_config(self, mock_run, client, temp_config, tmp_path):
        mpv_path = tmp_path / "mpv.exe"
        mpv_path.write_bytes(b"exe")
        mirror_dir = tmp_path / "mirror"
        media_root = tmp_path / "media"
        tree_dir = tmp_path / "trees"
        mirror_dir.mkdir()
        media_root.mkdir()
        tree_dir.mkdir()
        mock_run.return_value = {"available": True, "manifest_valid": True, "files_valid": True, "configuration_available": True, "message": "内置播放器已就绪"}

        response = client.post("/api/config/setup/complete", json={
            "mpv_path": str(mpv_path),
            "mirror_dir": str(mirror_dir),
            "local_root": str(media_root),
            "directory_tree_dir": str(tree_dir),
        })

        assert response.status_code == 200
        assert response.json()["setup_completed"] is True
        saved = json.loads(temp_config.read_text(encoding="utf-8"))
        assert saved["setup_completed"] is True
        assert saved["mpv_path"] == str(mpv_path)

    @patch("app.api.config.check_mpv_runtime")
    def test_setup_complete_creates_missing_mirror_directory(self, mock_run, client, temp_config, tmp_path):
        mpv_path = tmp_path / "mpv.exe"
        mpv_path.write_bytes(b"exe")
        mirror_dir = tmp_path / "Installed" / "mirror"
        media_root = tmp_path / "media"
        media_root.mkdir()
        mock_run.return_value = {"available": True, "manifest_valid": True, "files_valid": True, "configuration_available": True, "message": "内置播放器已就绪"}

        response = client.post("/api/config/setup/complete", json={
            "mpv_path": str(mpv_path),
            "mirror_dir": str(mirror_dir),
            "local_root": str(media_root),
        })

        assert response.status_code == 200
        assert mirror_dir.is_dir()
        assert json.loads(temp_config.read_text(encoding="utf-8"))["mirror_dir"] == str(mirror_dir)

    @patch("app.api.config.check_mpv_runtime")
    def test_setup_complete_reports_unwritable_mirror_directory(self, mock_run, client, temp_config, tmp_path):
        mpv_path = tmp_path / "mpv.exe"
        mpv_path.write_bytes(b"exe")
        mirror_dir = tmp_path / "protected" / "mirror"
        media_root = tmp_path / "media"
        media_root.mkdir()
        mock_run.return_value = {"available": True, "manifest_valid": True, "files_valid": True, "configuration_available": True, "message": "内置播放器已就绪"}

        with patch("app.api.config.Path.mkdir", side_effect=PermissionError("denied")):
            response = client.post("/api/config/setup/complete", json={
                "mpv_path": str(mpv_path),
                "mirror_dir": str(mirror_dir),
                "local_root": str(media_root),
            })

        assert response.status_code == 400
        assert "无法创建或不可写" in response.json()["detail"]
        assert not temp_config.exists()

    @patch("app.api.config.check_mpv_runtime")
    def test_setup_complete_requires_at_least_one_media_source(self, mock_run, client, temp_config, tmp_path):
        mpv_path = tmp_path / "mpv.exe"
        mpv_path.write_bytes(b"exe")
        mirror_dir = tmp_path / "mirror"
        mirror_dir.mkdir()
        mock_run.return_value = {"available": True, "manifest_valid": True, "files_valid": True, "configuration_available": True, "message": "内置播放器已就绪"}

        response = client.post("/api/config/setup/complete", json={
            "mpv_path": str(mpv_path),
            "mirror_dir": str(mirror_dir),
        })

        assert response.status_code == 400
        assert "媒体来源" in response.json()["detail"]

    @patch("app.api.config.check_mpv_runtime")
    def test_failed_setup_does_not_create_default_mirror_directory(self, mock_run, client, temp_config, tmp_path):
        mpv_path = tmp_path / "mpv.exe"
        mpv_path.write_bytes(b"exe")
        mirror_dir = tmp_path / "Installed" / "mirror"
        mock_run.return_value = {"available": True, "manifest_valid": True, "files_valid": True, "configuration_available": True, "message": "内置播放器已就绪"}

        response = client.post("/api/config/setup/complete", json={
            "mpv_path": str(mpv_path),
            "mirror_dir": str(mirror_dir),
        })

        assert response.status_code == 400
        assert "媒体来源" in response.json()["detail"]
        assert not mirror_dir.exists()
        assert not temp_config.exists()

    @patch("app.api.config.check_mpv_runtime")
    def test_setup_complete_validates_and_saves_bangumi_personal_access_token(self, mock_run, client, temp_config, tmp_path):
        mpv_path = tmp_path / "mpv.exe"
        mpv_path.write_bytes(b"exe")
        mirror_dir = tmp_path / "mirror"
        media_root = tmp_path / "media"
        mirror_dir.mkdir()
        media_root.mkdir()
        mock_run.return_value = {"available": True, "manifest_valid": True, "files_valid": True, "configuration_available": True, "message": "内置播放器已就绪"}

        with patch("app.api.config.BangumiClient") as bangumi_client:
            bangumi_client.return_value.get_me.return_value = {"id": 1, "username": "tester"}
            response = client.post("/api/config/setup/complete", json={
                "mpv_path": str(mpv_path),
                "mirror_dir": str(mirror_dir),
                "local_root": str(media_root),
                "bangumi_access_token": "bangumi-personal-token",
            })

        assert response.status_code == 200
        bangumi_client.assert_called_once_with(access_token="bangumi-personal-token", timeout=12.0)
        saved = json.loads(temp_config.read_text(encoding="utf-8"))
        assert saved["bangumi_access_token"] == "bangumi-personal-token"

    @patch("app.api.config.check_mpv_runtime")
    def test_setup_complete_explains_when_tmdb_v3_api_key_is_pasted(self, mock_run, client, temp_config, tmp_path):
        mpv_path = tmp_path / "mpv.exe"
        mpv_path.write_bytes(b"exe")
        mirror_dir = tmp_path / "mirror"
        media_root = tmp_path / "media"
        mirror_dir.mkdir()
        media_root.mkdir()
        mock_run.return_value = {"available": True, "manifest_valid": True, "files_valid": True, "configuration_available": True, "message": "内置播放器已就绪"}

        response = client.post("/api/config/setup/complete", json={
            "mpv_path": str(mpv_path),
            "mirror_dir": str(mirror_dir),
            "local_root": str(media_root),
            "tmdb_bearer_token": "0123456789abcdef0123456789abcdef",
        })

        assert response.status_code == 400
        assert "API 密钥" in response.json()["detail"]
        assert "API 读取访问令牌" in response.json()["detail"]


# ============================================================
# PATCH /api/config
# ============================================================

class TestPatchConfig:
    """测试更新配置"""

    def test_patch_single_field(self, client, temp_config):
        """应能更新单个字段"""
        resp = client.patch("/api/config", json={"mpv_path": "C:/mpv/mpv.exe"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["mpv_path"] == "C:/mpv/mpv.exe"

    def test_patch_multiple_fields(self, client, temp_config):
        """应能更新多个字段"""
        resp = client.patch("/api/config", json={
            "mpv_path": "C:/mpv/mpv.exe",
            "tmdb_language": "en-US",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["mpv_path"] == "C:/mpv/mpv.exe"
        assert data["tmdb_language"] == "en-US"

    def test_patch_server_port(self, client, temp_config):
        """后端端口应能通过设置保存并返回"""
        resp = client.patch("/api/config", json={"server_port": 8765})
        assert resp.status_code == 200
        data = resp.json()
        assert data["server_port"] == 8765

        saved = json.loads(temp_config.read_text(encoding="utf-8"))
        assert saved["server_port"] == 8765

    def test_patch_directory_tree_dir(self, client, temp_config, tmp_path):
        """目录树文件目录应能通过设置保存并返回。"""
        tree_dir = tmp_path / "目录树"
        resp = client.patch("/api/config", json={"directory_tree_dir": str(tree_dir)})

        assert resp.status_code == 200
        assert resp.json()["directory_tree_dir"] == str(tree_dir)
        saved = json.loads(temp_config.read_text(encoding="utf-8"))
        assert saved["directory_tree_dir"] == str(tree_dir)

    @pytest.mark.parametrize("port", [0, 70000])
    def test_patch_rejects_invalid_server_port(self, client, temp_config, port):
        """无效端口不能写入配置"""
        resp = client.patch("/api/config", json={"server_port": port})
        assert resp.status_code in (400, 422)

    def test_patch_preserves_unset_fields(self, client, temp_config):
        """未传入的字段应保持原值"""
        # 先设置初始值
        config = AppConfig(mpv_path="original/path", tmdb_language="ja")
        save_config(config)

        # 只更新 mpv_path
        resp = client.patch("/api/config", json={"mpv_path": "new/path"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["mpv_path"] == "new/path"
        assert data["tmdb_language"] == "ja"  # 保持原值

    def test_patch_rejects_unknown_field(self, client, temp_config):
        """应拒绝未知字段"""
        resp = client.patch("/api/config", json={"unknown_field": "value"})
        assert resp.status_code in (400, 422)

    def test_patch_rejects_unknown_field_mixed_with_valid_field(self, client, temp_config):
        """合法字段和未知字段混传时也必须拒绝，不能静默丢弃未知字段"""
        resp = client.patch("/api/config", json={
            "mpv_path": "C:/mpv/mpv.exe",
            "unknown_field": "value",
        })
        assert resp.status_code in (400, 422)

    def test_patch_empty_body_rejected(self, client, temp_config):
        """空请求应被拒绝"""
        resp = client.patch("/api/config", json={})
        assert resp.status_code == 400

    def test_patch_sensitive_empty_string_not_overwrite(self, client, temp_config):
        """敏感字段传空字符串不应覆盖原值"""
        config = AppConfig(tmdb_bearer_token="original-token")
        save_config(config)

        # 传空字符串
        resp = client.patch("/api/config", json={"tmdb_bearer_token": ""})
        assert resp.status_code == 200

        # 验证原值未被覆盖（需要重新读取配置）
        invalidate_config_cache()
        new_config = AppConfig()
        # 从文件读取验证
        data = json.loads(temp_config.read_text(encoding="utf-8"))
        assert data["tmdb_bearer_token"] == "original-token"

    def test_patch_sensitive_nonempty_updates(self, client, temp_config):
        """敏感字段传非空值应更新"""
        resp = client.patch("/api/config", json={"tmdb_bearer_token": "new-token"})
        assert resp.status_code == 200

        # 验证已更新
        data = json.loads(temp_config.read_text(encoding="utf-8"))
        assert data["tmdb_bearer_token"] == "new-token"


# ============================================================
# POST /api/config/test/mpv
# ============================================================

class TestTestMpv:
    """测试 mpv 测试端点"""

    def _runtime_status(self, *, available: bool, message: str, files_valid: bool = True) -> dict:
        return {
            "available": available,
            "version": "mpv v0.41.0-dev-g41f6a6450" if available else "",
            "architecture": "x86_64" if available else "",
            "target_triple": "x86_64-w64-mingw32" if available else "",
            "manifest_valid": True,
            "files_valid": files_valid,
            "configuration_available": True,
            "scripts_available": True,
            "distribution_status": "development-only",
            "message": message,
        }

    @patch("app.api.config.check_mpv_runtime")
    def test_mpv_not_found(self, mock_check, client, temp_config):
        """内置 MPV 缺失时应返回失败"""
        mock_check.return_value = self._runtime_status(
            available=False,
            message="内置 MPV 缺失或损坏",
        )

        resp = client.post("/api/config/test/mpv")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "内置 MPV" in data["message"]

    @patch("app.api.config.check_mpv_runtime")
    def test_mpv_success(self, mock_check, client, temp_config):
        """内置 MPV 可用时应返回成功"""
        mock_check.return_value = self._runtime_status(
            available=True,
            message="内置播放器已就绪",
        )

        resp = client.post("/api/config/test/mpv")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "mpv v0.41.0" in data["message"]

    @patch("app.api.config.check_mpv_runtime")
    def test_mpv_execution_failed(self, mock_check, client, temp_config):
        """内置 MPV 校验失败时应返回失败原因"""
        mock_check.return_value = self._runtime_status(
            available=True,
            message="内置 MPV 运行文件校验失败: mpv.exe",
            files_valid=False,
        )

        resp = client.post("/api/config/test/mpv")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "校验失败" in data["message"]


def test_media_paths_endpoint_reports_saved_mount_roots(client, temp_config, tmp_path):
    """设置页应能一次验证网盘挂载目录，不要求逐个目录树配置。"""
    pan115_root = tmp_path / "115open"
    baidu_root = tmp_path / "百度网盘"
    pan115_root.mkdir()
    baidu_root.mkdir()
    save_config(AppConfig(pan115_root=str(pan115_root), baidu_root=str(baidu_root)))

    resp = client.post("/api/config/test/media-paths")

    assert resp.status_code == 200
    data = resp.json()
    # OpenList 作为正式来源一并纳入检测；未配置时返回可读的未配置状态
    assert {item["source"] for item in data["sources"]} == {"pan115", "baidu", "openlist"}
    assert all("configured_root" in item for item in data["sources"])
    openlist_item = next(item for item in data["sources"] if item["source"] == "openlist")
    assert openlist_item["ok"] is False
    assert "尚未配置挂载目录" in openlist_item["message"]


def test_media_paths_endpoint_reports_openlist_mount_root(client, temp_config, tmp_path):
    """OpenList 挂载根已配置时，媒体路径检测应反映其健康状态。"""
    mount_root = tmp_path / "夸克挂载"
    mount_root.mkdir()
    save_config(AppConfig(openlist_mount_root=str(mount_root)))

    resp = client.post("/api/config/test/media-paths")

    assert resp.status_code == 200
    data = resp.json()
    openlist_item = next(item for item in data["sources"] if item["source"] == "openlist")
    assert openlist_item["configured_root"] == str(mount_root)
    assert openlist_item["ok"] is True
    assert openlist_item["status"] == "verified"


# ============================================================
# POST /api/config/test/tmdb
# ============================================================

class TestTestTmdb:
    """测试 TMDB 测试端点"""

    def test_no_token(self, client, temp_config):
        """未配置 token 时应返回失败"""
        config = AppConfig(tmdb_bearer_token="")
        save_config(config)

        resp = client.post("/api/config/test/tmdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "未配置" in data["message"]

    @patch("app.api.config.TMDBClient")
    def test_tmdb_success(self, mock_client_class, client, temp_config):
        """认证成功时应返回成功"""
        mock_client = MagicMock()
        mock_client.test_authentication.return_value = (True, "TMDB 认证成功")
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client_class.return_value = mock_client

        config = AppConfig(tmdb_bearer_token="valid-token")
        save_config(config)

        resp = client.post("/api/config/test/tmdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "成功" in data["message"]

    @patch("app.api.config.TMDBClient")
    def test_tmdb_invalid_token(self, mock_client_class, client, temp_config):
        """token 无效时应返回失败"""
        mock_client = MagicMock()
        mock_client.test_authentication.return_value = (
            False,
            "TMDB 认证失败: token 无效或已过期",
        )
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client_class.return_value = mock_client

        config = AppConfig(tmdb_bearer_token="invalid-token")
        save_config(config)

        resp = client.post("/api/config/test/tmdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "无效" in data["message"] or "过期" in data["message"]

    @patch("app.api.config.TMDBClient")
    def test_tmdb_ssl_error_uses_scrape_client_message(
        self,
        mock_client_class,
        client,
        temp_config,
    ):
        """证书错误应沿用刮削客户端的友好提示"""
        mock_client = MagicMock()
        mock_client.test_authentication.return_value = (
            False,
            "TMDB 认证测试失败: TMDB SSL 证书校验失败：请检查代理/VPN、DNS、杀毒软件 HTTPS 扫描或网络拦截。",
        )
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        mock_client_class.return_value = mock_client

        config = AppConfig(tmdb_bearer_token="valid-token")
        save_config(config)

        resp = client.post("/api/config/test/tmdb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "SSL 证书校验失败" in data["message"]


# ============================================================
# POST /api/config/test/deepseek
# ============================================================

class TestTestDeepseek:
    """测试 DeepSeek 测试端点"""

    def test_no_api_key(self, client, temp_config):
        """未配置 API key 时应返回失败"""
        config = AppConfig(deepseek_api_key="")
        save_config(config)

        resp = client.post("/api/config/test/deepseek")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "未配置" in data["message"]

    @patch("httpx.get")
    def test_deepseek_success(self, mock_get, client, temp_config):
        """连通成功时应返回成功"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        config = AppConfig(deepseek_api_key="valid-key")
        save_config(config)

        resp = client.post("/api/config/test/deepseek")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "成功" in data["message"]
