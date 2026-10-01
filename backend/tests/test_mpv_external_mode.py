"""B7：外部 MPV 整合包档必须「零注入」。

用户要求：直接用别人现成的整合包，只加载自己那点东西，**不影响整合包内部**。
因此外部档的启动参数里不能出现 ``--config-dir`` / ``--include`` / ``--script`` /
``--script-opts``：整合包的 portable_config、input.conf、scripts、shaders 必须由
mpv 自行加载，KumiPlayer 一条都不覆盖。
"""

from __future__ import annotations

import os
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

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
    assert args[-5:] == ["--{", "--start=12.500", "D:/a.strm", "--}", "D:/b.strm"]


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
    from app.playback import mpv
    from app.playback.mpv import _resolve_player_executable

    exe = tmp_path / "pack-mpv.exe"
    exe.write_bytes(b"x")
    builtin = tmp_path / "builtin-mpv.exe"
    builtin.write_bytes(b"x")
    monkeypatch.setattr(mpv, "get_mpv_executable", lambda: builtin)
    monkeypatch.setattr(core_config, "load_config", lambda *a, **k: SimpleNamespace(
        player_mode="internal", external_mpv_path=str(exe), mpv_path="",
    ))

    _path, external = _resolve_player_executable()
    assert _path == builtin

    assert external is False


def test_external_mode_accepts_pack_directory(monkeypatch, tmp_path):
    from app.core import config as core_config
    from app.playback.mpv import _resolve_player_executable

    exe = tmp_path / "mpv.exe"
    exe.write_bytes(b"mpv")
    monkeypatch.setattr(core_config, "load_config", lambda: SimpleNamespace(
        player_mode="external", external_mpv_path=f'"{tmp_path}"', mpv_path="",
    ))

    assert _resolve_player_executable() == (exe, True)


def test_external_launch_uses_pack_working_directory(monkeypatch, tmp_path):
    from app.playback import mpv

    exe = tmp_path / "pack" / "mpv.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"mpv")
    media = tmp_path / "episode.strm"
    media.write_text("https://example.test/video.mkv", encoding="utf-8")
    monkeypatch.setattr(mpv, "_resolve_player_executable", lambda: (exe, True))
    monkeypatch.setattr(mpv, "_exited_immediately", lambda _: False)
    monkeypatch.setattr(mpv, "_focus_mpv_window_async", lambda *args: None)
    popen = Mock()
    monkeypatch.setattr(mpv.subprocess, "Popen", popen)

    mpv.start_mpv(str(media), ipc_server="test-pipe", start_position=23)

    assert popen.call_args.kwargs["cwd"] == str(exe.parent)
    assert "--input-ipc-server=test-pipe" in popen.call_args.args[0]
    assert not any(arg.startswith(_FORBIDDEN_EXTERNAL_FLAGS) for arg in popen.call_args.args[0])


def test_external_validation_checks_selected_pack_instead_of_builtin(monkeypatch, tmp_path):
    from app.api import config
    from app.main import app
    from app.playback import mpv_runtime

    exe = tmp_path / "mpv.exe"
    exe.write_bytes(b"mpv")
    monkeypatch.setattr(mpv_runtime, "read_mpv_version", lambda path: ("mpv v0.41.0", ""))
    monkeypatch.setattr(config, "check_mpv_runtime", lambda **kwargs: pytest.fail("must not inspect builtin"))

    response = TestClient(app).post("/api/config/test/mpv-path", json={
        "mpv_path": str(tmp_path), "player_mode": "external",
    })

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["executable_path"] == str(exe)


def test_external_validation_rejects_non_mpv_executable(monkeypatch, tmp_path):
    from app.main import app
    from app.playback import mpv_runtime

    exe = tmp_path / "other.exe"
    exe.write_bytes(b"not mpv")
    monkeypatch.setattr(mpv_runtime, "read_mpv_version", lambda path: ("other app 1.0", ""))
    response = TestClient(app).post("/api/config/test/mpv-path", json={
        "mpv_path": str(exe), "player_mode": "external",
    })

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_external_open_config_uses_saved_pack(monkeypatch, tmp_path):
    from app.api import config
    from app.main import app

    exe = tmp_path / "mpv.exe"
    exe.write_bytes(b"mpv")
    portable = tmp_path / "portable_config"
    portable.mkdir()
    monkeypatch.setattr(config, "load_config", lambda: SimpleNamespace(
        player_mode="external", external_mpv_path=str(exe), mpv_path="",
    ))
    popen = Mock()
    monkeypatch.setattr(config.subprocess, "Popen", popen)
    response = TestClient(app).post("/api/config/mpv-runtime/open-config", json={"player_mode": "external"})

    assert response.status_code == 200
    assert response.json()["config_dir"] == str(portable)
    assert popen.call_args.args[0][-1] == str(portable)


def test_external_setup_preserves_mode_without_requiring_builtin(monkeypatch, tmp_path):
    from app.api import config
    from app.core.config import AppConfig
    from app.main import app
    from app.playback import mpv_runtime

    exe = tmp_path / "mpv.exe"
    exe.write_bytes(b"mpv")
    saved = AppConfig(player_mode="external", external_mpv_path=str(exe))
    monkeypatch.setattr(config, "load_config", lambda: saved)
    monkeypatch.setattr(config, "save_config", lambda candidate: None)
    monkeypatch.setattr(config, "check_mpv_runtime", lambda **kwargs: pytest.fail("must not require builtin"))
    monkeypatch.setattr(mpv_runtime, "read_mpv_version", lambda path: ("mpv v0.41.0", ""))
    response = TestClient(app).post("/api/config/setup/complete", json={
        "mirror_dir": str(tmp_path / "mirror"), "local_root": str(tmp_path),
    })

    assert response.status_code == 200
    assert response.json()["player_mode"] == "external"


@pytest.mark.parametrize("external", [False, True])
def test_external_playlist_resume_applies_only_to_first_file(tmp_path, monkeypatch, external):
    """用隔离合成音频验证 MPV 的文件局部选项语义，不加载用户配置。"""
    from app.core import config
    from app.playback import mpv_runtime
    from app.playback.mpv_runtime import build_mpv_playback_args

    runtime_override = os.environ.get("KUMIPLAYER_TEST_MPV_EXE")
    executable = (
        Path(runtime_override) if runtime_override
        else Path(__file__).resolve().parents[2] / "mpv" / "runtime" / "mpv.exe"
    )
    if not executable.is_file():
        pytest.skip("requires local MPV runtime")
    media = tmp_path / "episode.wav"
    with wave.open(str(media), "wb") as handle:
        handle.setparams((1, 2, 8000, 0, "NONE", "none"))
        handle.writeframes(bytes(16000))
    probe = tmp_path / "probe.lua"
    probe.write_text(
        'mp.register_event("file-loaded", function() '
        'print("KUMI_START=" .. tostring(mp.get_property("options/start"))) end)',
        encoding="utf-8",
    )
    monkeypatch.setattr(mpv_runtime, "get_mpv_config_dir", lambda: tmp_path)
    monkeypatch.setattr(mpv_runtime, "get_mpv_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(mpv_runtime, "get_kumiplayer_layer_dir", lambda: tmp_path / "layer")
    monkeypatch.setattr(mpv_runtime, "_ensure_scripts_present", lambda: None)
    monkeypatch.setattr(config, "load_config", lambda: SimpleNamespace())
    args = build_mpv_playback_args(
        executable, external=external, fallback=True, start_position=0.5,
        playlist_paths=[str(media), str(media)], first_file=str(media),
    )
    # 测试专用配置目录和观察脚本全部位于 tmp_path。
    args[1:1] = [
        f"--config-dir={tmp_path}", f"--script={probe}", "--ao=null", "--vo=null",
        "--idle=no", "--keep-open=no", "--speed=100",
    ]
    args = [arg for arg in args if arg != "--no-terminal"]
    result = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.returncode == 0, result.stderr
    starts = [line.split("KUMI_START=", 1)[1] for line in result.stdout.splitlines() if "KUMI_START=" in line]
    assert starts == ["0.5", "none"]


def test_external_mode_never_gets_builtin_window_policy_options():
    """F-009：初始窗口尺寸策略只属于内置模式，外部整合包 argv 不得出现这些选项。"""

    args = _args(external=True, first_file="D:/a.mkv", ipc_server=r"\.\pipe\kumi")
    for option in ("--autofit=", "--geometry=", "--auto-window-resize=", "--fullscreen="):
        assert not any(arg.startswith(option) for arg in args), f"外部模式不应出现 {option}"


def test_internal_mode_declares_bounded_initial_window_policy():
    """F-009：内置模式每次新建进程按工作区限定的中等窗口、居中、非全屏启动。"""

    args = _args(first_file="D:/a.mkv")
    assert "--autofit=70%x60%" in args, "初始尺寸必须受屏幕尺寸限制"
    assert "--geometry=50%:50%" in args, "初始窗口必须居中"
    assert "--auto-window-resize=no" in args, "同一窗口切集不得自动改尺寸"
    assert "--fullscreen=no" in args, "初始必须是非全屏"
    # 不记住上次尺寸：不得引入 watch-later 之外的窗口状态持久化
    assert not any(arg.startswith("--watch-later=") for arg in args)


def test_external_mode_never_overrides_the_pack_thumbnail_backend():
    """F-008：外部整合包自带 thumbfast/缩略图方案，KumiPlayer 不得注入任何 thumbfast 选项。"""

    args = _args(external=True, first_file="D:/a.mkv", ipc_server=r"\.\pipe\kumi")
    assert not any("thumbfast" in arg for arg in args), args


def test_external_mode_does_not_auto_fallback_to_english_subtitles():
    args = _args(external=True, first_file="D:/a.mkv")
    assert "--subs-fallback=no" in args
    assert not any(arg.startswith("--slang=") for arg in args)
