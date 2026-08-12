# -*- coding: utf-8 -*-
"""内置 MPV 运行时的统一解析、健康检查与启动参数构造。

本模块是 KumiPlayer 内置干净 MPV 的唯一事实来源：
- 解析内置 MPV 路径（源码 third_party / 安装 runtime/mpv）；
- 校验运行时清单（runtime-manifest.json）与 27 个运行文件；
- 构造 KumiPlayer 自有配置与隔离状态参数；
- 提供首次引导、设置页与播放共用的健康检查结果。

不读取系统 PATH、旧整合包 mpv/、用户 %APPDATA%\\mpv 或 config.json 中的 mpv_path。
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from app.core.runtime import (
    get_kumiplayer_layer_dir,
    get_mpv_config_dir,
    get_mpv_runtime_dir,
    get_mpv_state_dir,
)

# 运行时健康检查缓存
# 应用启动 / 首次访问执行一次完整校验，之后播放使用缓存，避免每次重复计算 27 个文件哈希。
# 缓存分两层：
# - 进程内缓存：同一次后端进程内多次播放直接命中；
# - 磁盘快照：应用重启后进程内缓存为空，但运行时文件 mtime/size 未变时
#   复用上次完整校验结果，避免每次重启后的首次播放都重算哈希与 mpv --version。
_MPV_RUNTIME_CACHE: dict = {}  # {"result": {...}, "snapshot": {...}}

MANIFEST_FILENAME = "runtime-manifest.json"
# 分层架构：portable_config 是用户可替换的整合包层（mpv.conf/input.conf 由整合包决定）；
# KumiPlayer 自有层 kumiplayer/ 承载应用必需配置与脚本（不可替换）。
REQUIRED_CONFIG_FILES = ("mpv.conf",)  # 仅校验自有层强制配置
REQUIRED_SCRIPTS = ("screenshot_to_video_dir.lua", "kumiplayer_anime4k.lua", "kumiplayer_bindings.lua")
_VERSION_TIMEOUT_SECONDS = 10
_RUNTIME_CHECK_FILENAME = "runtime-check.json"


# KumiPlayer 自有层脚本（通过 --script 显式加载，与整合包 scripts/ 自动加载并行）
KUMIPLAYER_SCRIPTS = (
    "screenshot_to_video_dir.lua",
    "kumiplayer_anime4k.lua",
    "kumiplayer_bindings.lua",
    "kumiplayer_uosc_menu.lua",
)
KUMIPLAYER_FORCED_CONFIG = "mpv.conf"


def get_mpv_manifest_path() -> Path:
    return get_mpv_runtime_dir().parent / MANIFEST_FILENAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def invalidate_mpv_runtime_cache() -> None:
    """清空运行时健康检查缓存（测试用）。"""
    _MPV_RUNTIME_CACHE.clear()


def _build_file_snapshot(
    manifest: Optional[dict],
    config_ok: bool,
    config_missing: list[str],
) -> dict:
    """记录所有受信任输入文件的 mtime 与 size，用于缓存失效检测。

    覆盖清单列出的每个运行文件、mpv.conf、input.conf、必需 Lua 脚本及清单文件自身。
    文件修改后父目录 mtime 不一定变化，所以必须按文件级别跟踪。
    """
    runtime_dir = get_mpv_runtime_dir()
    config_dir = get_mpv_config_dir()
    manifest_path = get_mpv_manifest_path()
    snapshot: dict = {}

    # 清单文件自身
    if manifest_path.is_file():
        try:
            stat = manifest_path.stat()
            snapshot[str(manifest_path)] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass

    # 清单列出的每个运行文件
    if manifest and isinstance(manifest.get("files"), list):
        for entry in manifest["files"]:
            rel = entry.get("path", "")
            if not rel:
                continue
            target = runtime_dir / rel
            try:
                stat = target.stat()
                snapshot[str(target)] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                pass

    # 配置与脚本文件
    for name in REQUIRED_CONFIG_FILES:
        target = config_dir / name
        try:
            stat = target.stat()
            snapshot[str(target)] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
    for name in REQUIRED_SCRIPTS:
        target = config_dir / "scripts" / name
        try:
            stat = target.stat()
            snapshot[str(target)] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass

    return snapshot


def _cache_valid() -> bool:
    """缓存存在且所有跟踪文件 mtime/size 与快照一致时返回 True。

    任意文件缺失、被修改、被删除都会使缓存失效。
    """
    snapshot = _MPV_RUNTIME_CACHE.get("file_snapshot", {})
    if not snapshot:
        return False
    for path_str, (expected_mtime, expected_size) in snapshot.items():
        try:
            stat = Path(path_str).stat()
            if stat.st_mtime_ns != expected_mtime or stat.st_size != expected_size:
                return False
        except OSError:
            return False
    return True


def _snapshot_matches(snapshot: dict) -> bool:
    """磁盘快照中的 mtime/size 是否仍与磁盘一致（不计算任何哈希）。"""
    if not snapshot:
        return False
    for path_str, (expected_mtime, expected_size) in snapshot.items():
        try:
            stat = Path(path_str).stat()
        except OSError:
            return False
        if stat.st_mtime_ns != expected_mtime or stat.st_size != expected_size:
            return False
    return True


def _runtime_check_path() -> Path:
    """磁盘校验快照路径。

    必须使用随数据目录隔离的 get_data_dir（测试经 KUMIPLAYER_DATA_DIR
    重定向），不能使用 get_mpv_state_dir——后者在测试环境不隔离，会把
    测试环境的校验结果写进真实项目 data 并污染后续校验。
    """
    from app.core.paths import get_data_dir

    return get_data_dir() / "mpv-state" / _RUNTIME_CHECK_FILENAME


def _load_disk_runtime_check() -> Optional[dict]:
    """读取上次完整校验的磁盘快照；缺失或损坏时返回 None。"""
    path = _runtime_check_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("file_snapshot"), dict):
        return None
    return data


def _save_disk_runtime_check(result: dict, snapshot: dict) -> None:
    """原子写入磁盘快照；失败不影响播放（下次播放重新完整校验）。"""
    try:
        path = _runtime_check_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"result": result, "file_snapshot": snapshot}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def load_runtime_manifest() -> Optional[dict]:
    """读取运行时清单，非法或缺失时返回 None。"""
    manifest_path = get_mpv_manifest_path()
    if not manifest_path.is_file():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    return data


def check_runtime_files(manifest: Optional[dict]) -> tuple[bool, str]:
    """校验清单中登记的文件在运行时目录中是否存在且 SHA-256 匹配。"""
    if not manifest or not manifest.get("files"):
        return False, "运行时清单缺失或非法"
    runtime_dir = get_mpv_runtime_dir()
    mismatches: list[str] = []
    missing: list[str] = []
    for entry in manifest["files"]:
        rel = entry.get("path")
        expected = str(entry.get("sha256") or "").upper()
        if not rel:
            continue
        target = runtime_dir / rel
        if not target.is_file():
            missing.append(rel)
            continue
        actual = _sha256(target)
        if expected and actual != expected:
            mismatches.append(rel)
    if missing:
        return False, f"内置 MPV 运行文件缺失: {', '.join(missing)}"
    if mismatches:
        return False, f"内置 MPV 运行文件校验失败: {', '.join(mismatches[:5])}"
    return True, ""


def check_configuration() -> tuple[bool, list[str]]:
    """校验 KumiPlayer 自有层（kumiplayer/）配置与脚本是否就绪。

    分层架构：portable_config 是用户可替换的整合包层，只要目录存在即可；
    应用必需文件（强制配置 + 自有脚本）在 kumiplayer/ 自有层校验。
    """
    config_dir = get_mpv_config_dir()
    layer_dir = get_kumiplayer_layer_dir()
    missing: list[str] = []
    if not config_dir.is_dir():
        missing.append(f"配置目录缺失: {config_dir.name}")
    if not layer_dir.is_dir():
        return False, [f"KumiPlayer 自有层缺失: {layer_dir.name}"]
    for name in REQUIRED_CONFIG_FILES:
        if not (layer_dir / name).is_file():
            missing.append(name)
    scripts_dir = layer_dir / "scripts"
    if not scripts_dir.is_dir():
        missing.append("kumiplayer/scripts/")
    else:
        for name in REQUIRED_SCRIPTS:
            if not (scripts_dir / name).is_file():
                missing.append(f"kumiplayer/scripts/{name}")
    return not missing, missing


def _ensure_scripts_present() -> None:
    """KumiPlayer 自有层脚本缺失时明确报错（硬失败语义）。"""
    layer_dir = get_kumiplayer_layer_dir()
    scripts_dir = layer_dir / "scripts"
    missing_scripts = [
        name for name in REQUIRED_SCRIPTS if not (scripts_dir / name).is_file()
    ]
    if missing_scripts:
        raise FileNotFoundError(
            f"KumiPlayer MPV 插件缺失: {', '.join(missing_scripts)}"
        )


def read_mpv_version(exe: Path) -> tuple[str, str]:
    """读取 MPV 版本与目标三元组，返回 (version_line, error)。"""
    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "", "mpv 版本检测超时"
    except OSError:
        return "", "mpv 无法启动"
    if result.returncode != 0:
        return "", (result.stderr.strip()[:120] or "mpv 版本检测失败")
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
    return first_line, ""


def check_mpv_runtime(*, verify_files: bool = True) -> dict:
    """返回统一的内置 MPV 健康检查结果。

    verify_files 控制是否逐个计算 27 个文件哈希：
    - True：执行完整校验（首次引导、设置页查看、重新检测）。
    - False：使用缓存结果（播放前），避免每次重复计算。
              缓存为空或失效时仍执行完整校验以确保正确性。
    """
    # 缓存命中且 verify_files=False 时直接返回，不计算哈希
    if not verify_files:
        if _cache_valid():
            return _MPV_RUNTIME_CACHE["result"]
        # 进程内缓存为空（应用重启后首次播放）：尝试磁盘持久化快照。
        # mtime/size 未变说明运行时文件未被修改，直接复用上次完整校验结果，
        # 跳过 27 个文件哈希与 mpv --version 子进程（启动提速的关键路径）。
        disk = _load_disk_runtime_check()
        if (
            disk
            and _snapshot_matches(disk.get("file_snapshot", {}))
            and disk.get("result", {}).get("files_valid")
        ):
            result = disk["result"]
            _MPV_RUNTIME_CACHE["result"] = result
            _MPV_RUNTIME_CACHE["file_snapshot"] = disk["file_snapshot"]
            return result

    runtime_dir = get_mpv_runtime_dir()
    exe = runtime_dir / "mpv.exe"
    manifest = load_runtime_manifest()
    config_ok, config_missing = check_configuration()

    available = exe.is_file()
    manifest_valid = manifest is not None
    files_valid = False
    file_message = ""
    # 只在 verify_files=True 时计算 27 个文件哈希
    if manifest and verify_files:
        files_valid, file_message = check_runtime_files(manifest)
    elif manifest:
        # verify_files=False 且无缓存：标记为未校验，由调用方决定是否降级到完整校验
        pass
    elif verify_files:
        file_message = "运行时清单缺失，无法校验运行文件"

    version = ""
    architecture = "x86_64" if available else ""
    target_triple = ""
    if available and not file_message:
        try:
            version, version_error = read_mpv_version(exe)
        except Exception:
            version = ""
            version_error = "mpv 版本检测异常"
        manifest_triple = (manifest or {}).get("target_triple", "")
        if manifest_triple:
            target_triple = manifest_triple
        elif version_error:
            file_message = version_error

    scripts_available = not config_missing
    distribution_status = (manifest or {}).get("distribution_status", "") or "unknown"

    problems: list[str] = []
    if not available:
        problems.append("内置 MPV 缺失或损坏")
    if file_message:
        problems.append(file_message)
    # 区分"未校验"和"校验失败"
    if not files_valid and not file_message:
        problems.append("内置 MPV 运行文件尚未校验")
    if config_missing:
        problems.append(f"KumiPlayer 播放配置不完整: {', '.join(config_missing)}")

    result = {
        "available": available,
        "version": version,
        "architecture": architecture,
        "target_triple": target_triple,
        "manifest_valid": manifest_valid,
        "files_valid": files_valid,
        "configuration_available": config_ok,
        "scripts_available": scripts_available,
        "distribution_status": distribution_status,
        "message": "内置播放器已就绪" if not problems else "；".join(problems),
    }

    # 缓存结果与文件级快照（无论 verify_files 值，最新计算结果都应缓存），
    # 并写入磁盘快照供应用重启后的首次播放复用。
    _MPV_RUNTIME_CACHE["result"] = result
    _MPV_RUNTIME_CACHE["file_snapshot"] = _build_file_snapshot(
        manifest, config_ok, config_missing
    )
    _save_disk_runtime_check(result, _MPV_RUNTIME_CACHE["file_snapshot"])
    return result


def build_mpv_playback_args(
    executable: Path,
    *,
    ipc_server: str = "",
    start_position: float = 0.0,
    window_title: str = "KumiPlayer",
    media_title: str = "",
    playlist_paths: Optional[list[str]] = None,
    first_file: str = "",
    fallback: bool = False,
) -> list[str]:
    """构造 KumiPlayer 内置 MPV 启动参数。

    使用 --config-dir 只加载 KumiPlayer 自有配置（MPV 官方手册：指定配置目录会忽略
    全局、用户与 MPV_HOME 配置），并隔离可写状态到 KumiPlayer 数据目录。
    """
    config_dir = get_mpv_config_dir()
    state_dir = get_mpv_state_dir()

    # 防御性创建可写状态目录（cache/log/watch_later），避免目录缺失导致缓存写入异常
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "cache").mkdir(parents=True, exist_ok=True)
    (state_dir / "watch_later").mkdir(parents=True, exist_ok=True)
    (state_dir / "thumbfast").mkdir(parents=True, exist_ok=True)

    args = [str(executable)]
    _ensure_scripts_present()
    args.append(f"--config-dir={config_dir}")
    # 分层架构：KumiPlayer 自有层强制配置在整合包 mpv.conf 之后追加解析
    layer_dir = get_kumiplayer_layer_dir()
    forced_config = layer_dir / KUMIPLAYER_FORCED_CONFIG
    if forced_config.is_file():
        args.append(f"--include={forced_config}")
    # KumiPlayer 自有脚本显式加载（与整合包 scripts/ 自动加载并行，互不覆盖）
    for script_name in KUMIPLAYER_SCRIPTS:
        script_path = layer_dir / "scripts" / script_name
        if script_path.is_file():
            args.append(f"--script={script_path}")
    args.append(f"--watch-later-directory={state_dir / 'watch_later'}")
    args.append(f"--demuxer-cache-dir={state_dir / 'cache'}")
    args.append(f"--log-file={state_dir / 'mpv.log'}")
    # thumbfast 缩略图缓存只写入 KumiPlayer 状态目录，不写用户/视频目录
    args.append(f"--script-opts=thumbfast.thumbnail={state_dir / 'thumbfast'}")
    # Anime4K 永久默认值：启动时注入，右键临时切换不影响
    try:
        from app.core.config import load_config
        _cfg = load_config()
        _mode = getattr(_cfg, "mpv_anime4k_mode", "off") or "off"
        _quality = getattr(_cfg, "mpv_anime4k_quality", "balanced") or "balanced"
        if _mode not in {"off", "a", "b", "c", "a+a", "b+b", "c+a"}:
            _mode = "off"
        if _quality not in {"light", "balanced", "high"}:
            _quality = "balanced"
        args.append(f"--script-opts=kumiplayer_anime4k.default_mode={_mode}")
        args.append(f"--script-opts=kumiplayer_anime4k.default_quality={_quality}")
    except Exception:
        # 配置读取失败不影响播放启动，使用默认值
        pass
    if ipc_server:
        args.append(f"--input-ipc-server={ipc_server}")
    args.append(f"--start={max(0.0, start_position):.3f}")
    args.extend([
        f"--title={window_title or 'KumiPlayer'}",
        f"--force-media-title={media_title or Path(first_file).stem}",
        "--save-position-on-quit=no",
        "--no-resume-playback",
        "--autocreate-playlist=no",
        "--reset-on-next-file=start",
        "--no-terminal",
    ])
    if not fallback:
        args.extend(["--force-window=immediate", "--focus-on=all", "--window-minimized=no"])
    args.extend(playlist_paths or ([first_file] if first_file else []))
    return args
