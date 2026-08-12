# -*- coding: utf-8 -*-
"""心跳状态管理器

管理 WebSocket 连接状态、心跳超时判断、自动退出触发。
"""

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from app.system.shutdown import request_backend_shutdown


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _default_is_playing() -> bool:
    """默认播放状态检查"""
    try:
        from app.playback.service import get_playback_manager
        return get_playback_manager().status()["status"] == "playing"
    except Exception:
        return False


def _default_is_busy() -> bool:
    """后台任务运行中时不允许心跳监控自动退出后端。"""
    try:
        from app.tasks.registry import get_task_manager
        return get_task_manager().has_running_tasks()
    except Exception:
        return False


def _default_is_parent_alive() -> bool:
    """桌面壳仍存活时，后端不能因 WebSocket 抖动自行退出。"""
    raw_pid = os.environ.get("KUMIPLAYER_PARENT_PID", "").strip()
    try:
        parent_pid = int(raw_pid)
    except (TypeError, ValueError):
        return False
    if parent_pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            synchronize = 0x00100000
            wait_timeout = 0x00000102
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(synchronize, False, parent_pid)
            if not handle:
                # 87 = ERROR_INVALID_PARAMETER，通常表示 PID 已不存在。
                # 其他探测错误按“仍存活”处理，避免权限或系统瞬态异常误杀后端。
                return ctypes.get_last_error() != 87
            try:
                wait_result = kernel32.WaitForSingleObject(handle, 0)
                if wait_result == wait_timeout:
                    return True
                if wait_result == 0:
                    return False
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True

    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class HeartbeatState:
    """心跳状态"""
    connected: bool = False
    last_seen: str = ""
    disconnected_at: str = ""
    active_connections: int = 0
    shutdown_requested: bool = False
    shutdown_reason: str = ""


class HeartbeatManager:
    """心跳管理器"""

    def __init__(
        self,
        shutdown_callback: Optional[Callable[[str], None]] = None,
        is_playing_callback: Optional[Callable[[], bool]] = None,
        is_busy_callback: Optional[Callable[[], bool]] = None,
        is_parent_alive_callback: Optional[Callable[[], bool]] = None,
    ):
        self._state = HeartbeatState()
        self._lock = threading.Lock()
        self._shutdown_callback = shutdown_callback or request_backend_shutdown
        self._is_playing = is_playing_callback or _default_is_playing
        self._is_busy = is_busy_callback or _default_is_busy
        self._is_parent_alive = is_parent_alive_callback or _default_is_parent_alive
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        # 用 monotonic 避免系统时间跳变
        self._last_seen_mono: float = 0.0
        self._disconnected_mono: float = 0.0

    def connect(self) -> None:
        """WebSocket 连接建立"""
        with self._lock:
            self._state.active_connections += 1
            self._state.connected = True
            self._state.last_seen = _now_iso()
            self._state.disconnected_at = ""
            self._last_seen_mono = time.monotonic()

    def receive_heartbeat(self) -> None:
        """收到心跳"""
        with self._lock:
            self._state.last_seen = _now_iso()
            self._state.connected = True
            self._last_seen_mono = time.monotonic()

    def disconnect(self) -> None:
        """WebSocket 连接断开"""
        with self._lock:
            self._state.active_connections = max(0, self._state.active_connections - 1)
            if self._state.active_connections <= 0:
                self._state.connected = False
                self._state.disconnected_at = _now_iso()
                self._disconnected_mono = time.monotonic()

    def get_state(self) -> HeartbeatState:
        """获取当前状态（返回副本）"""
        with self._lock:
            return HeartbeatState(
                connected=self._state.connected,
                last_seen=self._state.last_seen,
                disconnected_at=self._state.disconnected_at,
                active_connections=self._state.active_connections,
                shutdown_requested=self._state.shutdown_requested,
                shutdown_reason=self._state.shutdown_reason,
            )

    def should_shutdown(self, now: Optional[float] = None) -> bool:
        """判断是否应该退出

        参数:
            now: monotonic 时间，用于测试注入。None 时使用 time.monotonic()
        """
        from app.core.config import load_config
        config = load_config()

        if not config.heartbeat_enabled:
            return False

        # heartbeat_enabled 只代表前端连接状态监测是否启用。
        # 桌面使用时，浏览器休眠、切换窗口、WebSocket 抖动都可能让心跳短暂中断；
        # 默认不能因此杀掉后端。只有用户显式打开自动退出开关时才允许 shutdown。
        if not getattr(config, "auto_shutdown_on_heartbeat_timeout", False):
            return False

        # 已请求过退出则不重复触发
        with self._lock:
            if self._state.shutdown_requested:
                return False

        if now is None:
            now = time.monotonic()

        timeout = config.heartbeat_timeout

        with self._lock:
            # 有活跃连接：检查心跳是否超时
            if self._state.active_connections > 0:
                if self._last_seen_mono == 0.0:
                    return False
                elapsed = now - self._last_seen_mono
                if elapsed <= timeout:
                    return False
                # 心跳超时，进入退出判断
            else:
                # 无活跃连接：检查断开是否超时
                if self._disconnected_mono == 0.0:
                    return False
                elapsed = now - self._disconnected_mono
                if elapsed <= timeout:
                    return False
                # 断开超时，进入退出判断

        # 心跳退出只是桌面壳异常消失后的二级兜底。窗口仍存活时即使
        # WebSocket 抖动也不能杀掉后端；播放和后台任务同样阻止退出。
        if self._is_parent_alive() or self._is_playing() or self._is_busy():
            return False

        return True

    def monitor_once(self) -> None:
        """执行一次监控检查"""
        if self.should_shutdown():
            with self._lock:
                if self._state.shutdown_requested:
                    return
                self._state.shutdown_requested = True
                self._state.shutdown_reason = "心跳超时且无播放任务"
            self._shutdown_callback(self._state.shutdown_reason)

    def start_monitor(self, interval: float = 2.0) -> None:
        """启动后台监控线程"""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return

        with self._lock:
            if self._state.active_connections == 0 and self._disconnected_mono == 0.0:
                self._state.disconnected_at = _now_iso()
                self._disconnected_mono = time.monotonic()

        self._monitor_stop.clear()

        def _loop():
            while not self._monitor_stop.is_set():
                self.monitor_once()
                self._monitor_stop.wait(timeout=interval)

        self._monitor_thread = threading.Thread(target=_loop, daemon=True)
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        """停止后台监控线程"""
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
            self._monitor_thread = None


# 全局单例
_heartbeat_manager: Optional[HeartbeatManager] = None


def get_heartbeat_manager() -> HeartbeatManager:
    """获取全局 HeartbeatManager 单例"""
    global _heartbeat_manager
    if _heartbeat_manager is None:
        _heartbeat_manager = HeartbeatManager()
    return _heartbeat_manager


def reset_heartbeat_manager() -> None:
    """重置 HeartbeatManager（测试用）"""
    global _heartbeat_manager
    if _heartbeat_manager is not None:
        _heartbeat_manager.stop_monitor()
        _heartbeat_manager = None
