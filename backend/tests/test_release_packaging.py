# -*- coding: utf-8 -*-
"""桌面发布链路的静态回归检查。"""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_production_api_client_targets_the_desktop_backend():
    """Tauri 生产页面不是 FastAPI 同源页面，不能把 API_BASE 留空。"""
    client = (ROOT / "src" / "api" / "client.ts").read_text(encoding="utf-8")

    assert "http://127.0.0.1:37821" in client
    assert "http://127.0.0.1:8520" not in client
    assert "后端返回了非 JSON 响应" in client


def test_tauri_build_script_publishes_runtime_dll_and_safely_cleans_build_cache():
    """发布目录必须带运行时 DLL，并只清理可再生成的精确缓存目录。"""
    script = (ROOT / "build_tauri.bat").read_text(encoding="utf-8")

    assert "release\\WebView2Loader.dll" in script
    assert 'scripts\\verify_tauri_build.ps1' in script
    assert "release\\.staging" in script
    assert 'scripts\\ensure_kumiplayer_runtime_stopped.ps1' in script
    assert "KumiPlayer runtime preflight failed" in script
    assert "move /y \"release\\.staging\\KumiPlayer.exe\"" in script
    assert "Could not replace release\\KumiPlayer.exe" in script
    assert "KUMIPLAYER_KEEP_BUILD_CACHE" in script
    assert 'scripts\\cleanup_build_cache.ps1' in script
    assert '-ProjectRoot "%~dp0"' not in script
    assert "--no-bundle" in script
    assert "rmdir /s /q \"src-tauri\\target\"" not in script
    assert "rmdir /s /q \"dist\"" not in script

    cleanup = (ROOT / "scripts" / "cleanup_build_cache.ps1").read_text(encoding="utf-8")
    assert "Remove-Item -LiteralPath $target -Recurse -Force" in cleanup
    assert "Assert-WorkspacePath" in cleanup
    assert "$resolvedRootPrefix" in cleanup
    assert "$recursiveTargets" in cleanup
    assert 'src-tauri\\target' in cleanup
    assert 'node_modules' in cleanup
    assert "$protectedRoots" in cleanup
    assert "'data'" in cleanup
    assert "'mpv'" in cleanup
    assert "'release'" in cleanup


def test_tauri_release_verifier_checks_bundled_api_url_and_runtime_files():
    verifier = (ROOT / "scripts" / "verify_tauri_build.ps1").read_text(encoding="utf-8")

    assert "WebView2Loader.dll" in verifier
    assert "http://127.0.0.1:37821" in verifier
    assert "http://127.0.0.1:8520" not in verifier
    assert "Select-String" in verifier


def test_desktop_launcher_reports_missing_runtime_instead_of_silently_exiting():
    launcher = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    supervisor = (ROOT / "src-tauri" / "src" / "supervisor.rs").read_text(encoding="utf-8")
    job = (ROOT / "src-tauri" / "src" / "backend_job.rs").read_text(encoding="utf-8")
    cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")

    assert "DialogExt" in launcher
    assert "MessageDialogKind::Error" in launcher
    assert "KumiPlayerBackend.exe" in supervisor
    assert "start_bundled_backend" in supervisor
    assert "完整安装包" in launcher
    assert "普通用户不需要安装 Python" in launcher
    assert "import fastapi, uvicorn, httpx, multipart, websockets" in supervisor
    assert 'pub const BACKEND_PORT: u16 = 37821;' in supervisor
    assert '"8520"' not in supervisor
    assert "KUMIPLAYER_INSTALL_DIR" in supervisor
    assert "KUMIPLAYER_RUNTIME_KIND" in supervisor
    assert "KUMIPLAYER_RUNTIME_ID" in supervisor
    assert "KUMIPLAYER_INSTANCE_ID" in supervisor
    assert "KUMIPLAYER_PARENT_PID" in supervisor
    assert "KUMIPLAYER_AUTO_SHUTDOWN_ON_HEARTBEAT_TIMEOUT" in supervisor
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in job
    assert "backend_is_compatible" in supervisor
    assert "runtime_id_for" in supervisor
    assert "windows-sys" in cargo


def test_backend_default_port_matches_desktop_port():
    """后端默认配置必须和桌面壳、前端固定地址保持一致。"""
    config = (ROOT / "backend" / "app" / "core" / "config.py").read_text(encoding="utf-8")
    env_file = (ROOT / ".env").read_text(encoding="utf-8")

    assert "server_port: int = 37821" in config
    assert "VITE_API_BASE=http://127.0.0.1:37821" in env_file


def test_installer_build_stages_builtin_mpv_but_blocks_development_only_release():
    """安装版暂存内置 MPV 运行时，但 development-only 状态下正式安装包必须被拦截。"""
    script = (ROOT / "build_installer.bat").read_text(encoding="utf-8")
    config = (ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    installer_config = (ROOT / "src-tauri" / "tauri.installer.conf.json").read_text(
        encoding="utf-8"
    )
    stager = (ROOT / "scripts" / "stage_mpv_runtime.ps1").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts" / "verify_installer_bundle.ps1").read_text(encoding="utf-8")
    backend_entrypoint = (ROOT / "backend" / "desktop_backend.py").read_text(encoding="utf-8")

    assert "build_backend_sidecar.ps1" in script
    assert "--bundles nsis" in script
    assert "KumiPlayerBackend.exe" in script
    assert "runtime/backend" not in config
    assert "runtime/mpv-plugins" not in config
    assert '"../packaging/runtime/mpv/": "runtime/mpv/"' not in config
    assert "tauri.installer.conf.json" in script
    assert "runtime/backend" in installer_config
    assert "runtime/mpv-plugins" not in installer_config
    assert '"../packaging/runtime/mpv/": "runtime/mpv/"' in installer_config
    assert "runtime/mpv-profile" not in installer_config
    assert "runtime/mpv-tools" not in installer_config
    assert "runtime/root/WebView2Loader.dll" in installer_config
    assert '"WebView2Loader.dll"' in installer_config
    assert "stage_tauri_runtime.ps1" in script
    assert "verify_installer_bundle.ps1" in script
    assert "cargo build --release" in script
    assert "release\\KumiPlayer-Setup.exe" in script
    assert "downloadBootstrapper" in config
    assert "from app.main import app" in backend_entrypoint
    assert "uvicorn.run(app" in backend_entrypoint
    assert "data/" not in config
    assert "data\\" not in script

    # 暂存器必须把内置 MPV 运行时（含 27 文件、portable_config、manifest）放到 packaging/runtime/mpv
    assert "third_party\\mpv\\runtime" in stager
    assert "mpv-runtime\\portable_config" in stager
    assert "runtime-manifest.json" in stager
    assert "packaging\\runtime" in stager
    assert "'mpv'" in stager or 'mpv' in stager
    assert "Copy-Item" in stager
    # development-only 状态必须被暂存器与验证器双层拦截
    assert "distribution_status" in stager
    assert "development-only" in stager
    assert "第三方许可证" in stager
    assert "distribution_status" in verifier
    assert "development-only" in verifier
    assert "第三方许可证" in verifier


def test_installer_runtime_stager_and_verifier_cover_webview2_loader():
    """GNU 版桌面壳动态依赖的 Loader DLL 必须被暂存并写入 NSIS 清单。"""
    stager = (ROOT / "scripts" / "stage_tauri_runtime.ps1").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts" / "verify_installer_bundle.ps1").read_text(encoding="utf-8")

    assert "src-tauri\\target\\release\\WebView2Loader.dll" in stager
    assert "packaging\\runtime\\root" in stager
    assert "WebView2Loader.dll" in stager
    assert "Get-FileHash" in stager
    assert "installer.nsi" in verifier
    assert "WebView2Loader.dll" in verifier
    assert "Select-String" in verifier
    assert "Recursive deletion of the install root is forbidden" in verifier
    assert "The installer manifest must not manage the user mirror directory" in verifier


def test_builds_only_stop_verified_orphaned_kumiplayer_backends():
    raw_build = (ROOT / "build_tauri.bat").read_text(encoding="utf-8")
    installer_build = (ROOT / "build_installer.bat").read_text(encoding="utf-8")
    preflight = (ROOT / "scripts" / "ensure_kumiplayer_runtime_stopped.ps1").read_text(
        encoding="utf-8"
    )

    assert "ensure_kumiplayer_runtime_stopped.ps1" in raw_build
    assert "ensure_kumiplayer_runtime_stopped.ps1" in installer_build
    assert "Get-NetTCPConnection" in preflight
    assert "Invoke-RestMethod" in preflight
    assert "ParentProcessId" in preflight
    assert "KumiPlayerBackend.exe" in preflight
    assert "pythonw.exe" in preflight
    assert "Stop-Process -Id" in preflight
    assert "No process was stopped" in preflight
    assert "another program" in preflight
    assert "data" not in preflight.casefold()


def test_mpv_staging_refuses_development_only_runtime(tmp_path):
    """development-only 状态的内置 MPV 不得被暂存进正式安装包目录。"""
    # 构造一个最小但真实的第三方运行时 + 清单（development-only）
    source = tmp_path / "third_party" / "mpv" / "runtime"
    source.mkdir(parents=True)
    (source / "mpv.exe").write_bytes(b"exe")
    (source / "mpv.com").write_bytes(b"com")
    config_source = tmp_path / "resources" / "mpv-runtime" / "portable_config"
    config_source.mkdir(parents=True)
    (config_source / "mpv.conf").write_text("# test\n", encoding="utf-8")
    (config_source / "input.conf").write_text("# test\n", encoding="utf-8")
    layer_source = tmp_path / "resources" / "mpv-runtime" / "kumiplayer"
    layer_source.mkdir(parents=True)
    (layer_source / "mpv.conf").write_text("hwdec=auto-safe\n", encoding="utf-8")
    (layer_source / "scripts").mkdir()
    (layer_source / "scripts" / "screenshot_to_video_dir.lua").write_text("-- test\n", encoding="utf-8")
    manifest_dir = tmp_path / "third_party" / "mpv"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    import json
    (manifest_dir / "runtime-manifest.json").write_text(
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
            "files": [{"path": "mpv.exe", "sha256": "0" * 64}],
            "build_information": {},
            "acquired_at": "2026-08-01",
            "configuration_version": "test",
            "distribution_status": "development-only",
            "notes": "test fixture",
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "stage_mpv_runtime.ps1"),
            "-ProjectRoot",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    # development-only 必须被拦截，且不得生成 packaging/runtime/mpv
    assert result.returncode != 0
    assert "development-only" in result.stderr
    assert "distribution_status is development-only" in result.stderr
    # PowerShell 5.1 的 stderr 中文按系统代码页输出，逐字节断言不可靠；
    # 直接验证脚本源码包含许可证未补齐的拦截文案。
    stager_source = (ROOT / "scripts" / "stage_mpv_runtime.ps1").read_text(encoding="utf-8-sig")
    assert "第三方许可证" in stager_source
    staged = tmp_path / "packaging" / "runtime" / "mpv"
    assert not staged.exists()


def test_first_run_uses_builtin_mpv_and_does_not_ask_user_to_pick_external_player():
    page = (ROOT / "src" / "pages" / "FirstRunSetup.tsx").read_text(encoding="utf-8")

    assert "内置播放器" in page
    assert "安装包不包含 mpv.exe" not in page
    assert "选择 MPV" not in page
    assert "保留该播放器原有的界面、快捷键和插件" not in page
    assert "getMpvRuntime" in page
