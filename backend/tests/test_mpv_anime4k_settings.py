# -*- coding: utf-8 -*-
"""MPV Anime4K 永久默认设置：配置字段、API 枚举校验与 IPC 协调。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import AppConfig, load_config, save_config, invalidate_config_cache


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """隔离配置目录，避免写入真实 data/config.json。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    invalidate_config_cache()
    yield
    invalidate_config_cache()


def test_anime4k_config_defaults_are_safe():
    """默认 mode=off、quality=balanced；不应首次安装就强制改变画面。"""
    config = AppConfig()
    assert config.mpv_anime4k_mode == "off"
    assert config.mpv_anime4k_quality == "balanced"


def test_anime4k_config_roundtrip():
    """字段保存后能重新读取。"""
    config = load_config()
    config.mpv_anime4k_mode = "a+a"
    config.mpv_anime4k_quality = "high"
    save_config(config)
    reloaded = load_config(force_reload=True)
    assert reloaded.mpv_anime4k_mode == "a+a"
    assert reloaded.mpv_anime4k_quality == "high"


def test_anime4k_api_rejects_invalid_mode_and_quality():
    """非法 mode/quality 必须 400 拒绝，不传给 MPV。"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    ok_mode = client.patch("/api/config", json={"mpv_anime4k_mode": "b"})
    assert ok_mode.status_code == 200, ok_mode.text

    bad_mode = client.patch("/api/config", json={"mpv_anime4k_mode": "ultra"})
    assert bad_mode.status_code == 400

    bad_quality = client.patch("/api/config", json={"mpv_anime4k_quality": "ultra"})
    assert bad_quality.status_code == 400

    bad_enhanced = client.patch("/api/config", json={"mpv_anime4k_mode": "A"})
    assert bad_enhanced.status_code == 400  # 大小写敏感，A 不是合法枚举


def test_anime4k_public_config_exposes_fields():
    """公开配置响应包含 anime4k 默认值字段。"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "mpv_anime4k_mode" in data
    assert "mpv_anime4k_quality" in data


def test_anime4k_save_sends_set_default_to_active_mpv():
    """保存默认值后向活动 MPV 发送 set-default；无活动会话时静默跳过。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.playback.models import PlaybackSession

    class FakeManager:
        def __init__(self):
            self._current_session = PlaybackSession(
                session_id="sess-1", work_id="w1", episode_id="ep1", ipc_server="\\\\.\\pipe\\fake"
            )

    client = TestClient(app)
    with patch("app.playback.service.get_playback_manager", return_value=FakeManager()), patch(
        "app.playback.mpv_ipc.send_mpv_script_message"
    ) as mock_send:
        response = client.patch("/api/config", json={"mpv_anime4k_mode": "c+a"})
        assert response.status_code == 200
        mock_send.assert_called_once()
        args = mock_send.call_args.args
        assert args[1] == "kumiplayer_anime4k"
        assert args[2] == "set-default"
        assert args[3] == ("c+a", "balanced")


def test_anime4k_save_without_active_session_does_not_fail():
    """无活动播放会话时保存不应报错。"""
    from fastapi.testclient import TestClient
    from app.main import app

    class EmptyManager:
        _current_session = None

    client = TestClient(app)
    with patch("app.playback.service.get_playback_manager", return_value=EmptyManager()):
        response = client.patch("/api/config", json={"mpv_anime4k_quality": "light"})
        assert response.status_code == 200
