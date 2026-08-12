"""KumiPlayer 内置干净 MPV 接入契约测试。

旧架构依赖用户选择外部 MPV 并 --scripts-append 追加插件；新架构默认使用
KumiPlayer 内置干净 MPV，通过 --config-dir 加载自有配置并自动加载 scripts/。
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import invalidate_config_cache


@pytest.fixture
def client():
    invalidate_config_cache()
    return TestClient(app)


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    from app.core.config import CONFIG_FILE

    config_file = tmp_path / "config.json"
    monkeypatch.setattr("app.core.config.CONFIG_FILE", config_file)
    invalidate_config_cache()
    yield config_file
    invalidate_config_cache()


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_RELATIVE_PATH = Path("scripts") / "screenshot_to_video_dir.lua"


def _write_plugin(root: Path) -> Path:
    plugin = root / PLUGIN_RELATIVE_PATH
    plugin.parent.mkdir(parents=True)
    plugin.write_text("-- plugin", encoding="utf-8")
    return plugin


KUMIPLAYER_SCRIPTS = (
    "screenshot_to_video_dir.lua",
    "kumiplayer_anime4k.lua",
    "kumiplayer_bindings.lua",
    "kumiplayer_uosc_menu.lua",
)


def _write_kumiplayer_layer(root: Path) -> Path:
    """创建完整可用的 KumiPlayer 自有层（kumiplayer/），供测试通过校验。"""
    layer = root / "kumiplayer"
    scripts = layer / "scripts"
    scripts.mkdir(parents=True)
    (layer / "mpv.conf").write_text("hwdec=auto-safe\n", encoding="utf-8")
    for name in KUMIPLAYER_SCRIPTS:
        (scripts / name).write_text("-- " + name, encoding="utf-8")
    return layer


def _make_manifest(runtime_dir: Path, file_count: int = 1, override_sha256: str = "", extra_files: Optional[list[tuple[str, str]]] = None) -> Path:
    """为测试运行时生成合法清单。

    override_sha256 不为空时，mpv.exe 的 sha256 使用该值而非全零。
    extra_files 提供额外的 (path, sha256) 条目，常用于测试多文件清单场景。
    """
    manifest_path = runtime_dir.parent / "runtime-manifest.json"
    sha256 = override_sha256 or "0" * 64
    entries = [{"path": "mpv.exe", "sha256": sha256}]
    for index in range(file_count - 1):
        entries.append({"path": f"file{index}.dll", "sha256": "0" * 64})
    if extra_files:
        for path, extra_sha256 in extra_files:
            entries.append({"path": path, "sha256": extra_sha256})
    manifest_path.write_text(
        json.dumps({
            "schema_version": "1.0",
            "mpv_version": "0.41.0",
            "architecture": "x86_64",
            "target_triple": "x86_64-w64-mingw32",
            "upstream_repository": "https://github.com/mpv-player/mpv",
            "upstream_tag": "v0.41.0",
            "upstream_commit": "g41f6a6450",
            "artifact_provider": "mpv-first-party-github-ci",
            "artifact_filename": "mpv.zip",
            "artifact_url": "https://github.com/mpv-player/mpv/releases",
            "artifact_sha256": "0" * 64,
            "files": entries,
            "build_information": {},
            "acquired_at": "2026-08-01",
            "configuration_version": "test",
            "distribution_status": "development-only",
            "notes": "test fixture",
        }),
        encoding="utf-8",
    )
    return manifest_path


@pytest.fixture
def builtin_runtime(tmp_path, monkeypatch):
    """搭建一个可用的内置 MPV 测试运行时。"""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    # 真实 mpv.exe 与 27 个文件过大；测试用占位文件 + 清单哈希一致性即可。
    exe = runtime_dir / "mpv.exe"
    exe.write_bytes(b"placeholder-mpv")
    _make_manifest(runtime_dir, file_count=1)
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    _write_plugin(config_dir)
    layer_dir = _write_kumiplayer_layer(tmp_path)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(layer_dir))
    return runtime_dir, config_dir


def test_builtin_mpv_uses_config_dir_and_auto_loads_scripts_without_scripts_append(
    builtin_runtime,
):
    from app.playback import mpv

    runtime_dir, config_dir = builtin_runtime

    args = mpv._build_mpv_args(runtime_dir / "mpv.exe", "episode.strm")

    assert f"--config-dir={config_dir}" in args
    assert not any(arg.startswith("--no-config") for arg in args)
    assert not any(arg.startswith("--scripts-append=") for arg in args)
    assert "--autocreate-playlist=no" in args
    assert "--save-position-on-quit=no" in args
    assert "--no-resume-playback" in args
    # 可写状态必须隔离到 KumiPlayer 数据目录
    assert any(arg.startswith("--watch-later-directory=") for arg in args)
    assert any(arg.startswith("--demuxer-cache-dir=") for arg in args)
    assert any(arg.startswith("--log-file=") for arg in args)
    # thumbfast 缩略图缓存也必须隔离到 KumiPlayer 状态目录
    assert any(arg.startswith("--script-opts=thumbfast.thumbnail=") for arg in args)


def test_builtin_mpv_playback_args_preserve_window_and_media_title():
    from app.playback import mpv

    args = mpv._build_mpv_args(
        Path("mpv.exe"),
        "episode.strm",
        window_title="中文剧集",
        media_title="中文剧集 - S01E01",
        start_position=12.5,
    )

    assert "--title=中文剧集" in args
    assert "--force-media-title=中文剧集 - S01E01" in args
    assert "--start=12.500" in args
    assert "--no-terminal" in args


_REAL_MPV_EXE = ROOT / "third_party" / "mpv" / "runtime" / "mpv.exe"


@pytest.mark.skipif(
    not _REAL_MPV_EXE.is_file(),
    reason="内置 MPV 二进制不存在，跳过真实二进制冒烟测试",
)
def test_real_mpv_binary_accepts_full_playback_args():
    """真实 mpv 二进制必须接受 build_mpv_playback_args 构造的完整启动参数。

    防止回归：参数构造测试只 mock 参数本身，无法发现"真实二进制不接受某选项"
    的失败（如 v0.41.0 不支持 --cache-dir 导致所有播放 Fatal 退出）。
    这里用完整参数（含 --config-dir、--demuxer-cache-dir、--watch-later-directory、
    --log-file 等）播放内置测试源 av://lavfi:sine，验证退出码为 0。
    """
    import subprocess

    from app.playback.mpv_runtime import build_mpv_playback_args

    args = build_mpv_playback_args(
        _REAL_MPV_EXE,
        start_position=0.0,
        window_title="KumiPlayer smoke",
        media_title="smoke",
        first_file="av://lavfi:sine",
    )
    # 覆盖为无人值守冒烟模式：不弹窗、仅解码一帧、无终端
    args = [arg for arg in args if not arg.startswith("--force-window=")]
    args.extend(["--force-window=no", "--frames=1", "--no-terminal"])

    result = subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"真实 mpv 二进制拒绝启动参数，退出码 {result.returncode}：\n"
        f"{result.stderr[-2000:]}"
    )


def test_builtin_mpv_launch_fails_closed_when_script_is_missing(tmp_path, monkeypatch):
    from app.playback import mpv

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"exe")
    _make_manifest(runtime_dir, file_count=1)
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))

    with pytest.raises(FileNotFoundError, match="KumiPlayer MPV 插件"):
        mpv._build_mpv_args(runtime_dir / "mpv.exe", "episode.strm")


def test_kumiplayer_mpv_integration_reports_builtin_runtime_status(
    builtin_runtime,
):
    from app.playback import mpv

    runtime_dir, config_dir = builtin_runtime

    status = mpv.get_kumiplayer_mpv_integration()

    assert status["integration_dir"] == str(config_dir)
    assert status["integration_available"] is True
    assert status["plugin_available"] is True
    assert status["plugin_path"] == str(config_dir / "scripts" / "screenshot_to_video_dir.lua")
    assert status["mpv_available"] is True
    assert status["manifest_valid"] is True


def test_screenshot_plugin_uses_weak_hotkeys_and_self_contained_options():
    plugin = (
        ROOT
        / "resources"
        / "mpv-runtime"
        / "kumiplayer"
        / "scripts"
        / "screenshot_to_video_dir.lua"
    ).read_text(encoding="utf-8")

    assert "mp.add_key_binding('F10'" in plugin
    assert "mp.add_key_binding('Alt+F10'" in plugin
    assert "mp.add_forced_key_binding" not in plugin
    assert "user-data/kumiplayer/screenshot-plugin-loaded" in plugin
    assert "no-osd screenshot video" in plugin
    assert "screenshot subtitles" in plugin
    assert "mp.set_property('screenshot-format', 'jpg')" in plugin
    assert "mp.set_property_number('screenshot-jpeg-quality', 100)" in plugin
    assert "mp.set_property('screenshot-template', '%wH-%wM-%wS.%wT')" in plugin


def test_screenshot_uses_scraped_title_folder_and_millisecond_filename_without_sequence():
    plugin = (
        ROOT
        / "resources"
        / "mpv-runtime"
        / "kumiplayer"
        / "scripts"
        / "screenshot_to_video_dir.lua"
    ).read_text(encoding="utf-8")

    assert "~~desktop/动漫截图" in plugin
    assert "mp.get_property('media-title')" in plugin
    assert "mp.observe_property('media-title', 'string'" in plugin
    assert "sanitize_windows_component" in plugin
    assert "mp.set_property('screenshot-directory', screenshot_dir)" in plugin
    assert "%F" not in plugin
    assert "%n" not in plugin


def test_kumiplayer_mpv_plugin_is_strict_utf8_without_bom():
    path = (
        ROOT
        / "resources"
        / "mpv-runtime"
        / "kumiplayer"
        / "scripts"
        / "screenshot_to_video_dir.lua"
    )
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8", errors="strict")
    assert "�" not in text


def test_mpv_runtime_health_check_reports_all_fields(tmp_path, monkeypatch):
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"exe")
    _make_manifest(runtime_dir, file_count=1)
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    _write_plugin(config_dir)
    _write_kumiplayer_layer(tmp_path)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))

    status = mpv_runtime.check_mpv_runtime(verify_files=True)

    for key in (
        "available",
        "version",
        "architecture",
        "target_triple",
        "manifest_valid",
        "files_valid",
        "configuration_available",
        "scripts_available",
        "distribution_status",
        "message",
    ):
        assert key in status


def test_mpv_runtime_health_check_detects_missing_executable(tmp_path, monkeypatch):
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    _make_manifest(runtime_dir, file_count=1)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))

    status = mpv_runtime.check_mpv_runtime(verify_files=True)

    assert status["available"] is False
    assert "缺失" in status["message"]


def test_mpv_runtime_health_check_detects_missing_manifest(tmp_path, monkeypatch):
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"exe")
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))

    status = mpv_runtime.check_mpv_runtime(verify_files=True)

    assert status["manifest_valid"] is False
    assert "清单" in status["message"]


def test_mpv_runtime_health_check_detects_hash_mismatch(tmp_path, monkeypatch):
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"exe")
    manifest = _make_manifest(runtime_dir, file_count=1)
    # 篡改清单中的哈希使其与磁盘不匹配
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"][0]["sha256"] = "1" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))

    status = mpv_runtime.check_mpv_runtime(verify_files=True)

    assert status["files_valid"] is False
    assert "校验失败" in status["message"]


# ── 完整性门控与缓存 ──────────────────────────────────────────────────


def _make_strm(tmp_path: Path) -> Path:
    """创建临时 .strm 文件。"""
    strm = tmp_path / "test.strm"
    strm.write_text("https://media.example.invalid/test.mkv\n", encoding="utf-8")
    return strm


def _setup_runtime(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """搭建含完整清单的测试运行时，返回 (runtime_dir, config_dir)。

    写入占位 mpv.exe 后计算其实际 SHA-256 写入清单，使 files_valid=True。
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "mpv.exe"
    exe.write_bytes(b"placeholder-mpv")
    actual_hash = hashlib.sha256(b"placeholder-mpv").hexdigest().upper()
    _make_manifest(runtime_dir, file_count=1, override_sha256=actual_hash)
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    _write_plugin(config_dir)
    _write_kumiplayer_layer(tmp_path)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))
    return runtime_dir, config_dir


def test_start_mpv_raises_runtime_error_when_manifest_missing(tmp_path, monkeypatch):
    """内置 MPV 运行时清单缺失时，start_mpv 必须阻止 Popen 并抛出 RuntimeError。"""
    from app.playback import mpv

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"placeholder-mpv")
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    strm = _make_strm(tmp_path)

    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        with patch("subprocess.Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="清单缺失"):
                mpv.start_mpv(str(strm))
            mock_popen.assert_not_called()


def test_start_mpv_raises_runtime_error_when_executable_missing(tmp_path, monkeypatch):
    """内置 MPV mpv.exe 缺失时，start_mpv 必须阻止 Popen 并抛出 RuntimeError。"""
    from app.playback import mpv

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    _make_manifest(runtime_dir, file_count=1)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    # 没有写入 mpv.exe
    strm = _make_strm(tmp_path)

    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        with patch("subprocess.Popen") as mock_popen:
            # get_mpv_executable() 在完整性门控之前抛出 FileNotFoundError
            with pytest.raises(FileNotFoundError, match="缺失或损坏"):
                mpv.start_mpv(str(strm))
            mock_popen.assert_not_called()


def test_start_mpv_raises_runtime_error_when_hash_mismatch(tmp_path, monkeypatch):
    """内置 MPV 运行文件哈希不匹配时，start_mpv 必须阻止 Popen 并抛出 RuntimeError。"""
    from app.playback import mpv

    # 使用 _setup_runtime 创建完整运行时，再篡改 mpv.exe
    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)
    # 篡改 mpv.exe 的内容使其哈希与清单不匹配
    (runtime_dir / "mpv.exe").write_bytes(b"tampered-content")
    strm = _make_strm(tmp_path)

    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        with patch("subprocess.Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="校验失败"):
                mpv.start_mpv(str(strm))
            mock_popen.assert_not_called()


def test_start_mpv_raises_runtime_error_when_config_missing(tmp_path, monkeypatch):
    """KumiPlayer 播放配置缺失时，start_mpv 必须阻止 Popen 并抛出 RuntimeError。"""
    from app.playback import mpv

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"placeholder-mpv")
    actual_hash = hashlib.sha256(b"placeholder-mpv").hexdigest().upper()
    _make_manifest(runtime_dir, file_count=1, override_sha256=actual_hash)
    # 配置目录存在但没有 mpv.conf
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    # 不写入 mpv.conf 和 input.conf
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))
    strm = _make_strm(tmp_path)

    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        with patch("subprocess.Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="配置不完整"):
                mpv.start_mpv(str(strm))
            mock_popen.assert_not_called()


def test_mpv_runtime_cache_avoids_redundant_hash_computation(tmp_path, monkeypatch):
    """缓存命中时 verify_files=False 不应重复调用 check_runtime_files。"""
    from app.playback import mpv_runtime

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    # 首次完整校验，填充缓存
    mpv_runtime.invalidate_mpv_runtime_cache()
    status1 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status1["files_valid"] is True

    # 不修改任何文件，缓存应保持有效
    with patch("app.playback.mpv_runtime.check_runtime_files") as mock_check:
        status2 = mpv_runtime.check_mpv_runtime(verify_files=False)
        mock_check.assert_not_called()

    # 缓存结果应保持 valid=True
    assert status2["files_valid"] is True


def test_mpv_runtime_cache_invalidates_on_file_tamper(tmp_path, monkeypatch):
    """篡改 mpv.exe 后缓存应失效，播放链路须重新校验并阻止 Popen。"""
    from app.playback import mpv_runtime, mpv

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    # 首次完整校验，填充缓存
    mpv_runtime.invalidate_mpv_runtime_cache()
    status1 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status1["files_valid"] is True

    # 篡改 mpv.exe（mtime 变化）
    (runtime_dir / "mpv.exe").write_bytes(b"tampered-content")

    # verify_files=False 发现缓存失效，返回未校验状态
    status2 = mpv_runtime.check_mpv_runtime(verify_files=False)
    assert status2["files_valid"] is False

    # start_mpv 会降级到完整校验，发现哈希不匹配并阻止 Popen
    strm = _make_strm(tmp_path)
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        with patch("subprocess.Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="校验失败"):
                mpv.start_mpv(str(strm))
            mock_popen.assert_not_called()


def test_mpv_runtime_cache_invalidates_on_dll_tamper(tmp_path, monkeypatch):
    """篡改清单中已登记的 DLL 后缓存应失效，Popen 不被调用。"""
    from app.playback import mpv_runtime, mpv

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "mpv.exe"
    exe.write_bytes(b"placeholder-mpv")
    dll = runtime_dir / "libmpv.dll"
    dll.write_bytes(b"dll-content")
    actual_exe_hash = hashlib.sha256(b"placeholder-mpv").hexdigest().upper()
    actual_dll_hash = hashlib.sha256(b"dll-content").hexdigest().upper()
    _make_manifest(runtime_dir, file_count=1,
                   override_sha256=actual_exe_hash,
                   extra_files=[("libmpv.dll", actual_dll_hash)])
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    _write_plugin(config_dir)
    _write_kumiplayer_layer(tmp_path)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))

    # 首次完整校验，填充缓存
    mpv_runtime.invalidate_mpv_runtime_cache()
    status1 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status1["files_valid"] is True

    # 篡改 DLL
    (runtime_dir / "libmpv.dll").write_bytes(b"tampered-dll")

    # 缓存失效，verify_files=False 返回未校验状态
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        status2 = mpv_runtime.check_mpv_runtime(verify_files=False)
        assert status2["files_valid"] is False

    # verify_files=True 重新校验，应发现 DLL 哈希不匹配
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        status3 = mpv_runtime.check_mpv_runtime(verify_files=True)
        assert status3["files_valid"] is False
        assert "libmpv.dll" in status3["message"]


def test_mpv_runtime_cache_invalidates_on_config_change(tmp_path, monkeypatch):
    """修改 mpv.conf 后缓存应失效，播放链路须重新校验并阻止 Popen。"""
    from app.playback import mpv_runtime, mpv

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    # 首次完整校验，填充缓存
    mpv_runtime.invalidate_mpv_runtime_cache()
    status1 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status1["files_valid"] is True

    # 修改 mpv.conf
    (config_dir / "mpv.conf").write_text("# modified\n", encoding="utf-8")

    # verify_files=False 应发现缓存失效
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        status2 = mpv_runtime.check_mpv_runtime(verify_files=False)
    # 配置仍在（只是内容变了），所以 configuration_available 仍为 True
    # 但文件快照不匹配，缓存失效导致 files_valid 为 False（未校验）
    assert status2["files_valid"] is False

    # 重新检测（verify_files=True）应能通过（内容没变，只是文本变了）
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        status3 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status3["files_valid"] is True


def test_mpv_runtime_cache_invalidates_on_script_change(tmp_path, monkeypatch):
    """修改必需 Lua 脚本后缓存应失效，播放链路须重新校验。"""
    from app.playback import mpv_runtime

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    # 首次完整校验，填充缓存
    mpv_runtime.invalidate_mpv_runtime_cache()
    status1 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status1["files_valid"] is True

    # 修改脚本
    (config_dir / "scripts" / "screenshot_to_video_dir.lua").write_text(
        "-- modified\n", encoding="utf-8"
    )

    # 缓存失效
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        status2 = mpv_runtime.check_mpv_runtime(verify_files=False)
    assert status2["files_valid"] is False


def test_mpv_runtime_verify_files_false_skips_hash_computation(tmp_path, monkeypatch):
    """verify_files=False 且无缓存时不应计算完整哈希（files_valid=False）。"""
    from app.playback import mpv_runtime

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    # 清空缓存
    mpv_runtime.invalidate_mpv_runtime_cache()

    # verify_files=False 且无缓存，应跳过哈希计算
    with patch("app.playback.mpv_runtime.check_runtime_files") as mock_check:
        status = mpv_runtime.check_mpv_runtime(verify_files=False)
        mock_check.assert_not_called()

    # files_valid 应为 False（未校验）
    assert status["files_valid"] is False
    # 消息应明确区分"未校验"和"校验失败"
    assert "尚未校验" in status["message"]
    # 但其他字段应正确
    assert status["available"] is True
    assert status["manifest_valid"] is True
    assert status["configuration_available"] is True


def test_mpv_runtime_verify_files_true_returns_ready_message(tmp_path, monkeypatch):
    """verify_files=True 且全部通过时返回"已就绪"。"""
    from app.playback import mpv_runtime

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    mpv_runtime.invalidate_mpv_runtime_cache()
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        status = mpv_runtime.check_mpv_runtime(verify_files=True)

    assert status["files_valid"] is True
    assert status["message"] == "内置播放器已就绪"


def test_mpv_runtime_api_uses_full_check(tmp_path, monkeypatch):
    """/api/config/mpv-runtime 应使用完整校验，返回 files_valid=True。"""
    from app.api import config

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    # 模拟第一次访问（无缓存）
    from app.playback import mpv_runtime
    mpv_runtime.invalidate_mpv_runtime_cache()

    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        status = config.get_mpv_runtime_status()

    # 首次引导 API 应使用完整校验，files_valid 应为 True
    assert status["available"] is True
    assert status["manifest_valid"] is True
    assert status["files_valid"] is True
    assert status["configuration_available"] is True
    assert status["message"] == "内置播放器已就绪"


def test_start_mpv_blocks_popen_when_dll_is_tampered(tmp_path, monkeypatch):
    """已登记 DLL 被篡改后，start_mpv 必须重新校验并阻止 Popen。"""
    from app.playback import mpv, mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "mpv.exe"
    exe.write_bytes(b"placeholder-mpv")
    dll = runtime_dir / "libmpv-2.dll"
    dll.write_bytes(b"dll-content")
    actual_exe_hash = hashlib.sha256(b"placeholder-mpv").hexdigest().upper()
    actual_dll_hash = hashlib.sha256(b"dll-content").hexdigest().upper()
    _make_manifest(runtime_dir, file_count=1,
                   override_sha256=actual_exe_hash,
                   extra_files=[("libmpv-2.dll", actual_dll_hash)])
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    _write_plugin(config_dir)
    _write_kumiplayer_layer(tmp_path)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))

    # 首次完整校验，填充缓存
    mpv_runtime.invalidate_mpv_runtime_cache()
    status1 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status1["files_valid"] is True

    # 篡改 DLL
    (runtime_dir / "libmpv-2.dll").write_bytes(b"tampered-dll")

    # start_mpv 必须重新校验并阻止 Popen
    strm = _make_strm(tmp_path)
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        with patch("subprocess.Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="校验失败"):
                mpv.start_mpv(str(strm))
            mock_popen.assert_not_called()


def test_start_mpv_blocks_popen_when_lua_script_is_missing(tmp_path, monkeypatch):
    """必需 Lua 脚本缺失后，start_mpv 必须重新校验并阻止 Popen。"""
    from app.playback import mpv, mpv_runtime

    runtime_dir, config_dir = _setup_runtime(tmp_path, monkeypatch)

    # 首次完整校验，填充缓存
    mpv_runtime.invalidate_mpv_runtime_cache()
    status1 = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert status1["files_valid"] is True

    # 删除必需 Lua 脚本（KumiPlayer 自有层 kumiplayer/scripts/）
    (tmp_path / "kumiplayer" / "scripts" / "screenshot_to_video_dir.lua").unlink()

    # start_mpv 必须重新校验并阻止 Popen（自有层脚本缺失为硬失败）
    strm = _make_strm(tmp_path)
    with patch("app.playback.mpv_runtime.read_mpv_version") as mock_version:
        mock_version.return_value = ("mpv v0.41.0", "")
        with patch("subprocess.Popen") as mock_popen:
            with pytest.raises(FileNotFoundError, match="KumiPlayer MPV 插件缺失"):
                mpv.start_mpv(str(strm))
            mock_popen.assert_not_called()


def test_complete_setup_rejects_missing_manifest(tmp_path, monkeypatch, client, temp_config):
    """complete_setup 在清单缺失时必须拒绝 setup_completed=True。"""
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"placeholder-mpv")
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    mpv_runtime.invalidate_mpv_runtime_cache()
    (tmp_path / "mirror").mkdir(parents=True)
    (tmp_path / "media").mkdir(parents=True)

    response = client.post("/api/config/setup/complete", json={
        "mpv_path": "",
        "mirror_dir": str(tmp_path / "mirror"),
        "local_root": str(tmp_path / "media"),
    })
    assert response.status_code == 400
    assert "清单" in response.json()["detail"]


def test_complete_setup_rejects_hash_mismatch(tmp_path, monkeypatch, client, temp_config):
    """complete_setup 在运行文件校验失败时必须拒绝 setup_completed=True。"""
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"placeholder-mpv")
    actual_hash = hashlib.sha256(b"placeholder-mpv").hexdigest().upper()
    _make_manifest(runtime_dir, file_count=1, override_sha256=actual_hash)
    (runtime_dir / "mpv.exe").write_bytes(b"tampered-exe")
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    mpv_runtime.invalidate_mpv_runtime_cache()
    (tmp_path / "mirror").mkdir(parents=True)
    (tmp_path / "media").mkdir(parents=True)

    response = client.post("/api/config/setup/complete", json={
        "mpv_path": "",
        "mirror_dir": str(tmp_path / "mirror"),
        "local_root": str(tmp_path / "media"),
    })
    assert response.status_code == 400
    assert "校验失败" in response.json()["detail"]


def test_complete_setup_rejects_missing_config(tmp_path, monkeypatch, client, temp_config):
    """complete_setup 在播放配置缺失时必须拒绝 setup_completed=True。"""
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "mpv.exe").write_bytes(b"placeholder-mpv")
    actual_hash = hashlib.sha256(b"placeholder-mpv").hexdigest().upper()
    _make_manifest(runtime_dir, file_count=1, override_sha256=actual_hash)
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))
    mpv_runtime.invalidate_mpv_runtime_cache()
    (tmp_path / "mirror").mkdir(parents=True)
    (tmp_path / "media").mkdir(parents=True)

    response = client.post("/api/config/setup/complete", json={
        "mpv_path": "",
        "mirror_dir": str(tmp_path / "mirror"),
        "local_root": str(tmp_path / "media"),
    })
    assert response.status_code == 400
    assert "配置不完整" in response.json()["detail"]


def test_disk_snapshot_skips_hash_recompute_after_restart(monkeypatch, tmp_path):
    """应用重启（进程内缓存清空）后，运行时文件未变时复用磁盘快照，不重算哈希。

    这是播放启动提速的关键路径：verify_files=False 时磁盘快照命中，
    27 个文件 SHA-256 与 mpv --version 子进程都不再执行。
    """
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "mpv.exe"
    exe.write_bytes(b"placeholder-mpv")
    exe_sha = mpv_runtime._sha256(exe)
    _make_manifest(runtime_dir, file_count=1, override_sha256=exe_sha)
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    _write_plugin(config_dir)
    _write_kumiplayer_layer(tmp_path)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))

    mpv_runtime.invalidate_mpv_runtime_cache()
    first = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert first["files_valid"] is True

    # 模拟应用重启：清空进程内缓存
    mpv_runtime.invalidate_mpv_runtime_cache()

    calls = {"n": 0}
    original_sha = mpv_runtime._sha256

    def counting_sha256(path):
        calls["n"] += 1
        return original_sha(path)

    monkeypatch.setattr(mpv_runtime, "_sha256", counting_sha256)

    second = mpv_runtime.check_mpv_runtime(verify_files=False)
    assert second["files_valid"] is True
    assert second["configuration_available"] is True
    # 磁盘快照命中：未计算任何运行文件哈希
    assert calls["n"] == 0


def test_disk_snapshot_invalidated_when_runtime_file_changes(monkeypatch, tmp_path):
    """运行文件被修改（mtime 与 size 变化）后磁盘快照失效，重新完整校验并发现哈希不匹配。"""
    from app.playback import mpv_runtime

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)
    exe = runtime_dir / "mpv.exe"
    exe.write_bytes(b"placeholder-mpv")
    exe_sha = mpv_runtime._sha256(exe)
    _make_manifest(runtime_dir, file_count=1, override_sha256=exe_sha)
    config_dir = tmp_path / "portable_config"
    config_dir.mkdir(parents=True)
    (config_dir / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_dir / "input.conf").write_text("# test\n", encoding="utf-8")
    _write_plugin(config_dir)
    _write_kumiplayer_layer(tmp_path)
    monkeypatch.setenv("KUMIPLAYER_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KUMIPLAYER_MPV_LAYER_DIR", str(tmp_path / "kumiplayer"))

    mpv_runtime.invalidate_mpv_runtime_cache()
    assert mpv_runtime.check_mpv_runtime(verify_files=True)["files_valid"] is True

    # 修改运行文件 → mtime 与 size 都变化 → 磁盘快照失效 → 重新完整校验
    exe.write_bytes(b"placeholder-mpv-tampered")
    mpv_runtime.invalidate_mpv_runtime_cache()
    # verify_files=False 只能标记"未校验"（快照已失效），start_mpv 会降级完整校验
    status = mpv_runtime.check_mpv_runtime(verify_files=False)
    assert status["files_valid"] is False
    assert "尚未校验" in status["message"]
    # 降级后的完整校验发现哈希不匹配
    full = mpv_runtime.check_mpv_runtime(verify_files=True)
    assert full["files_valid"] is False
    assert "校验失败" in full["message"]


@pytest.mark.skipif(
    not _REAL_MPV_EXE.is_file(),
    reason="内置 MPV 二进制不存在，跳过真实脚本链路冒烟测试",
)
def test_real_mpv_anime4k_menu_click_forwards_set_session(tmp_path):
    """真实 mpv 上验证右键菜单点击链路。

    测试辅助脚本在 file-loaded 时向菜单脚本发送 uosc 回调模式事件
    （menu-event + activate + value=mode:b），菜单脚本必须按 value 前缀
    转发 set-session 给 Anime4K 脚本，后者日志出现 "applied mode=b"。
    覆盖 uosc 回调模式与 Lua 脚本通讯；辅助脚本只存在于测试临时目录。
    """
    import subprocess as _subprocess

    scripts_dir = ROOT / "resources/mpv-runtime/kumiplayer/scripts"
    log_path = tmp_path / "mpv-menu-smoke.log"

    # 测试辅助脚本：模拟 uosc 点击「Anime4K Mode B」
    trigger = tmp_path / "trigger_click.lua"
    event_json = '{"type":"activate","value":"mode:b"}'
    trigger_body = (
        'mp.register_event("file-loaded", function()\n'
        '    mp.commandv("script-message-to", "kumiplayer_uosc_menu", "menu-event",\n'
        "        '" + event_json + "')\n"
        'end)\n'
    )
    trigger.write_text(trigger_body, encoding="utf-8")

    proc = _subprocess.Popen(
        [
            str(_REAL_MPV_EXE),
            "--no-terminal",
            "--force-window=no",
            "--frames=120",
            "av://lavfi:sine",
            f"--script={scripts_dir / 'kumiplayer_uosc_menu.lua'}",
            f"--script={scripts_dir / 'kumiplayer_anime4k.lua'}",
            f"--script={trigger}",
            f"--log-file={log_path}",
        ],
        stdout=_subprocess.DEVNULL,
        stderr=_subprocess.DEVNULL,
        creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()

    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert "kumiplayer_uosc_menu] loaded" in log, f"菜单脚本未加载：{chr(10)}{log[-800:]}"
    assert "kumiplayer_anime4k] loaded" in log, f"Anime4K 脚本未加载：{chr(10)}{log[-800:]}"
    assert "applied mode=b" in log, (
        "菜单点击未转发到 Anime4K set-session（menu-event → set-session 链路断开）；"
        "mpv 日志：" + chr(10) + log[-1500:]
    )
