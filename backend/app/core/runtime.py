# -*- coding: utf-8 -*-
"""桌面源码版与安装版共用的运行目录发现。"""

import os
import sys
from pathlib import Path


def get_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def is_frozen_backend() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_runtime_dir() -> Path:
    """返回只读程序运行时目录，不在这里创建或清理任何文件。"""
    override = os.environ.get("KUMIPLAYER_RUNTIME_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen_backend():
        executable_dir = Path(sys.executable).resolve().parent
        return executable_dir.parent if executable_dir.name.casefold() == "backend" else executable_dir
    return get_project_root()


def get_default_data_dir() -> Path:
    """源码版沿用项目 data；安装版使用当前用户的 LocalAppData。"""
    if is_frozen_backend():
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "KumiPlayer" / "data"
    return get_project_root() / "data"


def get_install_root() -> Path:
    """返回桌面安装根目录；源码模式返回工程根目录。"""
    override = os.environ.get("KUMIPLAYER_INSTALL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen_backend():
        runtime_dir = get_runtime_dir()
        return runtime_dir.parent if runtime_dir.name.casefold() == "runtime" else runtime_dir
    return get_project_root()


def get_default_mirror_dir() -> Path:
    """安装版默认放在安装根目录，源码版继续使用工程 data。"""
    if is_frozen_backend():
        return get_install_root() / "mirror"
    return get_project_root() / "data" / "mirror"


def _uses_packaged_runtime() -> bool:
    runtime_kind = os.environ.get("KUMIPLAYER_RUNTIME_KIND", "").strip().casefold()
    return runtime_kind == "bundled" or is_frozen_backend()


def get_mpv_runtime_dir() -> Path:
    """返回 KumiPlayer 内置干净 MPV 运行文件目录（含 mpv.exe）。

    源码模式使用项目 third_party；安装模式使用桌面运行时目录下的 mpv/。
    测试可通过 KUMIPLAYER_MPV_RUNTIME_DIR 覆盖，但不得读取真实用户目录。
    """
    override = os.environ.get("KUMIPLAYER_MPV_RUNTIME_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if _uses_packaged_runtime():
        return get_runtime_dir() / "mpv"
    return get_project_root() / "third_party" / "mpv" / "runtime"


def get_mpv_executable() -> Path:
    """返回内置 MPV 可执行文件路径；运行文件缺失时抛出明确错误。"""
    exe = get_mpv_runtime_dir() / "mpv.exe"
    if not exe.is_file():
        raise FileNotFoundError("KumiPlayer 内置 MPV 缺失或损坏，请重新检测播放器运行时")
    return exe


def get_mpv_config_dir() -> Path:
    """返回 KumiPlayer 自有的 MPV 配置目录（portable_config）。

    源码模式使用项目 resources/mpv-runtime；安装模式使用运行时目录下的 mpv/portable_config。
    测试可通过 KUMIPLAYER_MPV_CONFIG_DIR 覆盖。
    """
    override = os.environ.get("KUMIPLAYER_MPV_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if _uses_packaged_runtime():
        return get_runtime_dir() / "mpv" / "portable_config"
    return get_project_root() / "resources" / "mpv-runtime" / "portable_config"


def get_kumiplayer_layer_dir() -> Path:
    """返回 KumiPlayer 自有插件层目录（kumiplayer）。

    与 portable_config（可替换的整合包层）分离：本目录随应用分发、不可替换，
    承载 KumiPlayer 自有脚本与强制配置。测试可通过 KUMIPLAYER_MPV_LAYER_DIR 覆盖。
    """
    override = os.environ.get("KUMIPLAYER_MPV_LAYER_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if _uses_packaged_runtime():
        return get_runtime_dir() / "mpv" / "kumiplayer"
    return get_project_root() / "resources" / "mpv-runtime" / "kumiplayer"


def get_mpv_state_dir() -> Path:
    """返回 KumiPlayer 自有的 MPV 可写状态目录（cache/log/watch_later）。"""
    return get_default_data_dir() / "mpv-state"


def get_kumiplayer_mpv_plugins_dir() -> Path:
    """返回 KumiPlayer 自有的 MPV 脚本目录。

    分层架构（2026-08-12）：KumiPlayer 自有脚本位于 kumiplayer/scripts/
    （不可替换层）；旧路径 resources/mpv-plugins 与 portable_config/scripts
    已废弃。本函数重定向到自有层，保留兼容调用，不再返回旧路径或整合包路径。
    """
    return get_kumiplayer_layer_dir() / "scripts"
