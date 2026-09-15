"""B7：外部 MPV 整合包档必须「零注入」。

用户要求：直接用别人现成的整合包，只加载自己那点东西，**不影响整合包内部**。
因此外部档的启动参数里不能出现 ``--config-dir`` / ``--include`` / ``--script`` /
``--script-opts``：整合包的 portable_config、input.conf、scripts、shaders 必须由
mpv 自行加载，KumiPlayer 一条都不覆盖。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

_FORBIDDEN_EXTERNAL_FLAGS = ("--config-dir", "--include", "--script", "--script-opts")


def _args(**kwargs) -> list[str]:
    from app.playback.mpv_runtime import build_mpv_playback_args

    return build_mpv_playback_args(Path("D:/pack/mpv.exe"), **kwargs)


def test_external_mode_injects_no_config_and_no_scripts():
    args = _args(
        external=True,
        ipc_server=r"\\.\pipe\kumiplayer-test",
        start_position=12.5,
        window_title="摇曳露营 · S02E01",
        media_title="摇曳露营 · S02E01",
        playlist_paths=["D:/a.strm", "D:/b.strm"],
        first_file="D:/a.strm",
    )

    assert args[0] == str(Path("D:/pack/mpv.exe"))
    for flag in _FORBIDDEN_EXTERNAL_FLAGS:
        assert not any(item.startswith(flag) for item in args), f"外部档不应注入 {flag}"

    # 会话必需参数必须保留：进度 IPC、进度起点、标题与受控播放列表。
    assert any(item.startswith("--input-ipc-server=") for item in args)
    assert any(item.startswith("--start=") for item in args)
    assert any(item.startswith("--title=") for item in args)
    assert any(item.startswith("--force-media-title=") for item in args)
    assert "--autocreate-playlist=no" in args
    assert "--save-position-on-quit=no" in args
    assert "--no-resume-playback" in args
    assert args[-2:] == ["D:/a.strm", "D:/b.strm"]


def test_internal_mode_still_injects_kumiplayer_config():
    args = _args(
        ipc_server="",
        start_position=0.0,
        window_title="标题",
        media_title="标题",
        first_file="D:/a.strm",
    )

    assert any(item.startswith("--config-dir=") for item in args)


def test_external_mode_requires_a_valid_player_path(monkeypatch):
    from app.core import config as core_config
    from app.playback.mpv import _resolve_player_executable

    monkeypatch.setattr(core_config, "load_config", lambda *a, **k: SimpleNamespace(
        player_mode="external", external_mpv_path="", mpv_path="",
    ))

    with pytest.raises(RuntimeError, match="外部播放器不可用"):
        _resolve_player_executable()


def test_external_mode_uses_configured_path(monkeypatch, tmp_path):
    from app.core import config as core_config
    from app.playback.mpv import _resolve_player_executable

    exe = tmp_path / "mpv.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(core_config, "load_config", lambda *a, **k: SimpleNamespace(
        player_mode="external", external_mpv_path=str(exe), mpv_path="",
    ))

    path, external = _resolve_player_executable()

    assert path == exe
    assert external is True


def test_legacy_mpv_path_is_reused_as_external_path(monkeypatch, tmp_path):
    """旧配置只有 mpv_path 时，外部档依然可用（读取期兼容，不自动切换模式）。"""

    from app.core import config as core_config
    from app.playback.mpv import _resolve_player_executable

    exe = tmp_path / "legacy-mpv.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(core_config, "load_config", lambda *a, **k: SimpleNamespace(
        player_mode="external", external_mpv_path="", mpv_path=str(exe),
    ))

    path, external = _resolve_player_executable()

    assert path == exe
    assert external is True


def test_internal_mode_ignores_external_path(monkeypatch, tmp_path):
    from app.core import config as core_config
    from app.playback.mpv import _resolve_player_executable

    exe = tmp_path / "pack-mpv.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(core_config, "load_config", lambda *a, **k: SimpleNamespace(
        player_mode="internal", external_mpv_path=str(exe), mpv_path="",
    ))

    _path, external = _resolve_player_executable()

    assert external is False
