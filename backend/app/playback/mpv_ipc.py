# -*- coding: utf-8 -*-
"""Small MPV JSON IPC client used for local playback progress tracking."""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass(frozen=True)
class MpvProgressEvent:
    position: float
    duration: float
    playlist_position: int
    force_checkpoint: bool = False
    media_path: str = ""


def make_ipc_server_name(session_id: str) -> str:
    safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in {"_", "-"})
    if os.name == "nt":
        return rf"\\.\pipe\kumiplayer-{safe_id}"
    return str(Path(tempfile.gettempdir()) / f"kumiplayer-{safe_id}.sock")


def read_mpv_progress(ipc_server: str, timeout: float = 0.5) -> Optional[tuple[float, float, int]]:
    """Read progress and the active playlist position from mpv.

    Returns None while mpv is still starting or when the IPC pipe is unavailable.
    """
    try:
        with _open_ipc(ipc_server, timeout) as pipe:
            position = _get_property(pipe, "time-pos", 1)
            duration = _get_property(pipe, "duration", 2)
            playlist_position = _get_property(pipe, "playlist-pos", 3)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if position is None or duration is None or playlist_position is None:
        return None
    return float(position), float(duration), int(playlist_position)


def observe_mpv_progress(ipc_server: str, timeout: float = 0.5) -> Iterator[MpvProgressEvent]:
    """Yield progress changes from one persistent MPV JSON IPC connection."""
    observations = (
        (1, "playlist-pos"),
        (5, "path"),
        (2, "duration"),
        (3, "time-pos"),
        (4, "pause"),
    )
    pending_requests = {100 + observation_id for observation_id, _name in observations}
    playlist_position: Optional[int] = None
    position: Optional[float] = None
    duration: Optional[float] = None
    media_path = ""
    force_next_checkpoint = False

    with _open_ipc(ipc_server, timeout) as pipe:
        for observation_id, property_name in observations:
            _write_command(
                pipe,
                ["observe_property", observation_id, property_name],
                100 + observation_id,
            )

        while True:
            line = pipe.readline()
            if not line:
                raise OSError("mpv IPC closed while observing progress")
            message = json.loads(line.decode("utf-8"))

            request_id = message.get("request_id")
            if request_id in pending_requests:
                if message.get("error") != "success":
                    raise OSError(f"mpv refused progress observation {request_id}")
                pending_requests.discard(request_id)
                continue

            event_name = message.get("event")
            if event_name == "seek":
                force_next_checkpoint = True
                continue
            if event_name != "property-change":
                continue

            property_name = message.get("name")
            value = message.get("data")
            if property_name == "playlist-pos":
                next_playlist_position = int(value) if value is not None else None
                if playlist_position is not None and next_playlist_position != playlist_position:
                    position = None
                    duration = None
                    force_next_checkpoint = True
                playlist_position = next_playlist_position
            elif property_name == "path":
                next_media_path = str(value or "")
                if media_path and next_media_path != media_path:
                    position = None
                    duration = None
                    force_next_checkpoint = True
                media_path = next_media_path
            elif property_name == "duration":
                duration = float(value) if value is not None else None
            elif property_name == "time-pos":
                position = float(value) if value is not None else None
            elif property_name == "pause" and value is True:
                force_next_checkpoint = True

            has_complete_sample = (
                playlist_position is not None
                and position is not None
                and duration is not None
                and bool(media_path)
            )
            should_emit = property_name == "time-pos" or (property_name == "pause" and value is True)
            if not has_complete_sample or not should_emit:
                continue

            force_checkpoint = force_next_checkpoint
            force_next_checkpoint = False
            yield MpvProgressEvent(
                position=position,
                duration=duration,
                playlist_position=playlist_position,
                force_checkpoint=force_checkpoint,
                media_path=media_path,
            )


def set_mpv_playback_title(ipc_server: str, media_title: str, timeout: float = 0.5) -> bool:
    """Update both the native window title and fullscreen controller title."""
    try:
        with _open_ipc(ipc_server, timeout) as pipe:
            media_updated = _set_property(pipe, "force-media-title", media_title, 11)
            window_updated = _set_property(pipe, "title", media_title, 12)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return media_updated and window_updated


def send_mpv_script_message(
    ipc_server: str,
    script_name: str,
    message: str,
    args: tuple[str, ...],
    timeout: float = 0.5,
) -> bool:
    """向运行中的 MPV 发送 script-message（如 Anime4K set-default）。

    命令格式：script-message <script_name> <message> [args...]
    失败静默返回 False，不阻塞调用方（配置保存不应因播放器离线而失败）。
    """
    command = f"script-message {script_name} {message}"
    if args:
        command += " " + " ".join(str(arg) for arg in args)
    try:
        with _open_ipc(ipc_server, timeout) as pipe:
            request = json.dumps({"command": command.split()}).encode("utf-8") + b"\n"
            pipe.write(request)
            pipe.flush()
            response = pipe.readline()
            try:
                data = json.loads(response)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return True  # 无响应体时视为已投递
            return data.get("error") is None or data.get("error") == "success"
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def send_mpv_quit(ipc_server: str, timeout: float = 1.0) -> bool:
    """通过 IPC 发送 quit 命令，让 MPV 优雅退出。

    优先于 process.terminate() 使用，避免 Windows 硬杀触发
    "mpv has stopped working" 崩溃对话框和 CrashSender.exe。
    成功返回 True；IPC 不可用或超时返回 False（调用方应回退到 terminate）。
    """
    try:
        with _open_ipc(ipc_server, timeout) as pipe:
            request = json.dumps({"command": ["quit"], "request_id": 999}).encode("utf-8") + b"\n"
            pipe.write(request)
            pipe.flush()
            # 不等待响应：MPV 收到 quit 后立即开始关闭，pipe 会断开
            return True
    except (OSError, TimeoutError, ValueError):
        return False


def _open_ipc(ipc_server: str, timeout: float):
    if os.name == "nt":
        return _open_windows_named_pipe(ipc_server, timeout)
    return open(ipc_server, "r+b", buffering=0)


def _open_windows_named_pipe(pipe_name: str, timeout: float):
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel32.WaitNamedPipeW.restype = ctypes.c_int
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p

    timeout_ms = max(1, int(timeout * 1000))
    if not kernel32.WaitNamedPipeW(pipe_name, timeout_ms):
        raise TimeoutError(pipe_name)

    handle = kernel32.CreateFileW(
        pipe_name,
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0,
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), pipe_name)

    fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
    return open(fd, "r+b", buffering=0, closefd=True)


def _get_property(pipe, property_name: str, request_id: int) -> Optional[float]:
    response = _send_command(pipe, ["get_property", property_name], request_id)
    if response.get("error") != "success":
        return None
    value = response.get("data")
    if value is None:
        return None
    return float(value)


def _set_property(pipe, property_name: str, value: str, request_id: int) -> bool:
    response = _send_command(pipe, ["set_property", property_name, value], request_id)
    return response.get("error") == "success"


def _send_command(pipe, command: list[Any], request_id: int) -> dict[str, Any]:
    _write_command(pipe, command, request_id)
    for _ in range(20):
        line = pipe.readline()
        if not line:
            raise OSError("mpv IPC closed before replying")
        response = json.loads(line.decode("utf-8"))
        if response.get("request_id") == request_id:
            return response
    raise ValueError(f"mpv IPC response missing for request {request_id}")


def _write_command(pipe, command: list[Any], request_id: int) -> None:
    payload = json.dumps(
        {"command": command, "request_id": request_id},
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    pipe.write(payload)
