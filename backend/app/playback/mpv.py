"""mpv 进程启动与管理"""

import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from app.core.runtime import get_mpv_config_dir, get_mpv_executable
from app.playback.mpv_runtime import (
    build_mpv_playback_args,
    check_mpv_runtime,
    get_mpv_manifest_path,
    load_runtime_manifest,
)

SW_SHOW = 5
SW_SHOWNORMAL = 1
SW_RESTORE = 9
GW_OWNER = 4
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
FOREGROUND_RETRY_SECONDS = 5.0
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1
MAX_TITLE_LENGTH = 160
ASFW_ANY = -1
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE
_KUMIPLAYER_MPV_SCRIPTS = ("screenshot_to_video_dir.lua",)


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def start_mpv(
    strm_path: str,
    mpv_path: str | None = None,
    *,
    playlist_paths: list[str] | None = None,
    ipc_server: str = "",
    start_position: float = 0.0,
    display_title: str = "",
) -> subprocess.Popen:
    """启动 MPV 播放 .strm 文件。

    播放模式由配置决定：
    - ``internal``（默认）：KumiPlayer 内置干净 MPV + 自有配置与脚本；
    - ``external``：用户自备 MPV 整合包，**零注入**——不传 config-dir/include/
      script/script-opts，整合包的配置、脚本、着色器与界面完全自主。

    参数:
        strm_path: .strm 文件路径
        mpv_path: 仅测试注入用；为 None 时按播放模式解析可执行文件。

    返回:
        subprocess.Popen 实例

    异常:
        FileNotFoundError: 内置 mpv 缺失或 .strm 不存在
        RuntimeError: 内置运行时校验失败，或外部档未配置有效播放器
        OSError: 启动失败
    """
    external_mode = False
    if mpv_path:
        executable = Path(mpv_path)
    else:
        executable, external_mode = _resolve_player_executable()

    if not Path(strm_path).is_file():
        raise FileNotFoundError(f".strm 文件不存在: {strm_path}")

    # 完整性门控只针对内置运行时：外部整合包的完整性由用户自己负责。
    if mpv_path is None and not external_mode:
        status = check_mpv_runtime(verify_files=False)
        if not (status["available"] and status["files_valid"] and status["configuration_available"]):
            # 缓存为空或失效，降级到完整校验
            status = check_mpv_runtime(verify_files=True)
        if not status["available"]:
            raise RuntimeError(
                "KumiPlayer 内置 MPV 缺失或损坏，无法启动播放"
            )
        if not status["manifest_valid"]:
            raise RuntimeError(
                "KumiPlayer 内置 MPV 运行时清单缺失或非法，无法启动播放"
            )
        if not status["files_valid"]:
            raise RuntimeError(
                f"KumiPlayer 内置 MPV 运行文件校验失败，无法启动播放: {status['message']}"
            )
        if not status["configuration_available"]:
            raise RuntimeError(
                f"KumiPlayer 播放配置不完整，无法启动播放: {status['message']}"
            )

    media_paths = list(playlist_paths or [strm_path])
    if not media_paths:
        media_paths = [strm_path]

    # 使用参数列表，不使用 shell=True。mpv 自己保存同一文件的退出位置，
    # 前端仍只展示“继续播放第 X 集”，不暴露具体时间点。
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    startupinfo = _windows_startupinfo()
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    media_title = _clean_display_title(display_title) or Path(strm_path).stem.strip() or "mpv"
    window_title = _build_window_title(media_title)
    process = subprocess.Popen(
        _build_mpv_args(
            executable,
            strm_path,
            window_title,
            playlist_paths=media_paths,
            ipc_server=ipc_server,
            start_position=start_position,
            media_title=media_title,
            external=external_mode,
        ),
        **kwargs,
    )
    if _exited_immediately(process):
        process = subprocess.Popen(
            _build_fallback_mpv_args(
                executable,
                strm_path,
                window_title,
                playlist_paths=media_paths,
                ipc_server=ipc_server,
                start_position=start_position,
                media_title=media_title,
                external=external_mode,
            ),
            **kwargs,
        )
    _focus_mpv_window_async(process.pid, window_title)
    return process


def _build_window_title(media_title: str) -> str:
    title = _clean_display_title(media_title) or "KumiPlayer"
    return title[:MAX_TITLE_LENGTH]


def _clean_display_title(title: str) -> str:
    return " ".join(str(title or "").split())


def _resolve_player_executable() -> tuple[Path, bool]:
    """按播放模式解析可执行文件；返回 ``(路径, 是否外部档)``。

    外部档要求用户已选择有效的 MPV 可执行文件：路径缺失或不存在时明确报错，
    不静默回退到内置播放器（否则用户以为自己用的是整合包，实际不是）。
    """

    from app.core.config import load_config

    try:
        config = load_config()
    except Exception:
        config = None
    mode = str(getattr(config, "player_mode", "internal") or "internal").strip().casefold()
    if mode != "external":
        return get_mpv_executable(), False
    # 旧配置兼容：mpv_path 历史上存的就是外部播放器路径。
    external_path = str(getattr(config, "external_mpv_path", "") or "").strip() or str(
        getattr(config, "mpv_path", "") or ""
    ).strip()
    if not external_path or not Path(external_path).is_file():
        raise RuntimeError(
            "外部播放器不可用：请在设置中选择有效的 MPV 可执行文件（当前播放模式为外部整合包）"
        )
    return Path(external_path), True


def _build_mpv_args(
    mpv_path: Path,
    strm_path: str,
    window_title: str | None = None,
    *,
    playlist_paths: list[str] | None = None,
    ipc_server: str = "",
    start_position: float = 0.0,
    media_title: str = "",
    external: bool = False,
) -> list[str]:
    return build_mpv_playback_args(
        mpv_path,
        ipc_server=ipc_server,
        start_position=start_position,
        window_title=window_title or "KumiPlayer",
        media_title=media_title or Path(strm_path).stem,
        playlist_paths=playlist_paths,
        first_file=strm_path,
        external=external,
    )


def _build_fallback_mpv_args(
    mpv_path: Path,
    strm_path: str,
    window_title: str | None = None,
    *,
    playlist_paths: list[str] | None = None,
    ipc_server: str = "",
    start_position: float = 0.0,
    media_title: str = "",
    external: bool = False,
) -> list[str]:
    return build_mpv_playback_args(
        mpv_path,
        ipc_server=ipc_server,
        start_position=start_position,
        window_title=window_title or "KumiPlayer",
        media_title=media_title or Path(strm_path).stem,
        playlist_paths=playlist_paths,
        first_file=strm_path,
        fallback=True,
        external=external,
    )


def _build_kumiplayer_script_args() -> list[str]:
    """KumiPlayer 自有脚本由 --config-dir 的 scripts/ 目录自动加载，无需 --scripts-append。

    这里仍校验脚本存在，缺失时明确报错，避免启动后才发现功能失效。
    """
    from app.core.runtime import get_mpv_config_dir

    scripts_dir = get_mpv_config_dir() / "scripts"
    missing_scripts = [name for name in _KUMIPLAYER_MPV_SCRIPTS if not (scripts_dir / name).is_file()]
    if missing_scripts:
        raise FileNotFoundError(
            f"KumiPlayer MPV 插件缺失: {', '.join(missing_scripts)}"
        )
    return []


def get_kumiplayer_mpv_integration() -> dict:
    """返回非敏感的 KumiPlayer 内置 MPV 功能状态（版本、清单、运行文件、配置与脚本）。"""
    from app.playback.mpv_runtime import (
        check_mpv_runtime,
    )

    status = check_mpv_runtime(verify_files=False)
    manifest = load_runtime_manifest()
    return {
        "integration_dir": str(get_mpv_config_dir()),
        "integration_available": status["configuration_available"],
        "plugin_available": status["scripts_available"],
        "plugin_path": str(get_mpv_config_dir() / "scripts" / _KUMIPLAYER_MPV_SCRIPTS[0]),
        "mpv_available": status["available"],
        "version": status["version"],
        "manifest_valid": status["manifest_valid"],
        "files_valid": status["files_valid"],
        "distribution_status": status["distribution_status"],
        "runtime_manifest": str(get_mpv_manifest_path()),
        "manifest_entries": len(manifest["files"]) if manifest else 0,
    }


def _exited_immediately(process: subprocess.Popen) -> bool:
    time.sleep(0.2)
    status = process.poll()
    return isinstance(status, int)


def _windows_startupinfo() -> Any | None:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_SHOWNORMAL
    return startupinfo


def _focus_mpv_window_async(pid: int, window_title: str = "") -> None:
    if os.name != "nt" or pid <= 0:
        return
    thread = threading.Thread(target=_focus_mpv_window, args=(pid, window_title), daemon=True)
    thread.start()


def _focus_mpv_window(pid: int, window_title: str = "") -> None:
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + FOREGROUND_RETRY_SECONDS
    related_pids = {pid}
    border_stripped = False

    while time.monotonic() < deadline:
        related_pids.update(_child_process_ids(pid))
        hwnd = _find_main_window_for_pid(user32, related_pids, window_title)
        if hwnd:
            if not border_stripped:
                _strip_dwm_border(hwnd)
                border_stripped = True
            _force_window_to_front(user32, hwnd, pid)
            if user32.GetForegroundWindow() == hwnd:
                return
        time.sleep(0.1)


def _strip_dwm_border(hwnd: int) -> None:
    """去掉 Windows 11 给无边框窗口画的 1px 边框。

    ``--border=no`` 时 mpv 的窗口样式仍是 ``WS_THICKFRAME``（可缩放）但没有
    ``WS_CAPTION``，DWM 因此仍会绘制一圈可见边框（实测窗口样式 ``0x140E0000``、
    ``DWMWA_VISIBLE_FRAME_BORDER_THICKNESS == 1``），表现为画面四周一圈浅色
    “白边”。``--window-corners=donotround`` 无法消除（实测该值仍为 1），
    mpv 也没有对应选项，只能由调用方把边框颜色设为 ``DWMWA_COLOR_NONE``。

    实测（本机）：``DwmSetWindowAttribute`` 返回 S_OK；``DwmGetWindowAttribute``
    对该属性返回 E_INVALIDARG（本机构建不支持读取），因此无法回读校验，
    需要在真机上肉眼确认边框是否消失。失败时静默忽略，不影响播放。
    """
    try:
        value = ctypes.c_uint(DWMWA_COLOR_NONE)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            DWMWA_BORDER_COLOR,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except (AttributeError, OSError):
        pass


def _find_main_window_for_pid(user32, pids: set[int], window_title: str = "") -> int:
    found_hwnd = ctypes.c_void_p()

    def callback(hwnd, _lparam):
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        title_matches = False
        if window_title:
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title_matches = window_title in buffer.value
        if window_pid.value not in pids and not title_matches:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        found_hwnd.value = hwnd
        return False

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(callback)
    user32.EnumWindows(enum_proc, None)
    return int(found_hwnd.value or 0)


def _force_window_to_front(user32, hwnd: int, pid: int = 0) -> None:
    _allow_foreground_activation(user32, pid)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.BringWindowToTop(hwnd)
    user32.SetActiveWindow(hwnd)
    user32.SetFocus(hwnd)

    foreground = user32.GetForegroundWindow()
    current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

    attached_target = False
    attached_foreground = False
    try:
        if target_thread and target_thread != current_thread:
            attached_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        if foreground_thread and foreground_thread != current_thread:
            attached_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
        for _ in range(3):
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except AttributeError:
                pass
            if user32.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.05)
    finally:
        if attached_foreground:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
        if attached_target:
            user32.AttachThreadInput(current_thread, target_thread, False)

    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)


def _allow_foreground_activation(user32, pid: int = 0) -> None:
    """尝试为 mpv 进程争取前台窗口权限。

    注意：``AllowSetForegroundWindow`` 只对**前台进程自身**的调用有效；
    KumiPlayer 的后端是 Tauri 应用的子进程、并非前台进程，因此这里的调用
    多数情况下不生效（返回值被忽略）。真正起作用的是后续
    ``AttachThreadInput`` + ``SetForegroundWindow`` 那一段。

    这里**不再模拟 Alt 按键**：自 Vista 起 Windows 只认真实输入，
    ``keybd_event`` 合成的按键无法解除前台锁定，反而会把一个 Alt 事件丢给
    当时的前台窗口（用户侧的「首次交互被吞掉」）。属实测无效的手法，已移除。
    """
    try:
        if pid > 0:
            user32.AllowSetForegroundWindow(pid)
        else:
            user32.AllowSetForegroundWindow(ASFW_ANY)
    except AttributeError:
        pass


def _child_process_ids(parent_pid: int) -> set[int]:
    if os.name != "nt" or parent_pid <= 0:
        return set()

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return set()

    children: set[int] = set()
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)

    try:
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            if int(entry.th32ParentProcessID) == parent_pid:
                children.add(int(entry.th32ProcessID))
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    return children
