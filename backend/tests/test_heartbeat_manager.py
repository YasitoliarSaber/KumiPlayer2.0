# -*- coding: utf-8 -*-
"""HeartbeatManager 测试"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_connect_sets_active():
    """connect 后 active_connections=1"""
    from app.system.heartbeat import HeartbeatManager

    mgr = HeartbeatManager()
    mgr.connect()
    state = mgr.get_state()
    assert state.active_connections == 1
    assert state.connected is True
    mgr.disconnect()


def test_receive_heartbeat_updates_last_seen():
    """receive_heartbeat 更新 last_seen"""
    from app.system.heartbeat import HeartbeatManager

    mgr = HeartbeatManager()
    mgr.connect()
    before = mgr.get_state().last_seen
    time.sleep(0.05)
    mgr.receive_heartbeat()
    after = mgr.get_state().last_seen
    assert after >= before
    mgr.disconnect()


def test_disconnect_sets_disconnected():
    """disconnect 后 connected=false 且记录 disconnected_at"""
    from app.system.heartbeat import HeartbeatManager

    mgr = HeartbeatManager()
    mgr.connect()
    mgr.disconnect()
    state = mgr.get_state()
    assert state.connected is False
    assert state.active_connections == 0
    assert state.disconnected_at != ""


def test_heartbeat_enabled_false_no_shutdown():
    """heartbeat_enabled=false 不触发 shutdown"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig, invalidate_config_cache
    import app.core.config as cfg

    # 设置 heartbeat_enabled=false
    old = cfg._cached_config
    cfg._cached_config = AppConfig(heartbeat_enabled=False, heartbeat_timeout=1)

    shutdown_called = []
    mgr = HeartbeatManager(shutdown_callback=lambda r: shutdown_called.append(r))
    mgr.connect()
    mgr.disconnect()
    # 超时
    result = mgr.should_shutdown(now=time.monotonic() + 100)
    assert result is False
    assert shutdown_called == []

    cfg._cached_config = old


def test_disconnect_within_timeout_no_shutdown():
    """连接断开未超过 heartbeat_timeout 不触发 shutdown"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(heartbeat_enabled=True, heartbeat_timeout=30)

    shutdown_called = []
    mgr = HeartbeatManager(shutdown_callback=lambda r: shutdown_called.append(r))
    mgr.connect()
    now = time.monotonic()
    mgr.disconnect()
    # 5 秒后检查，timeout=30
    result = mgr.should_shutdown(now=now + 5)
    assert result is False

    cfg._cached_config = old


def test_auto_shutdown_default_false_no_shutdown():
    """默认不因心跳超时自动退出后端"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(heartbeat_enabled=True, heartbeat_timeout=10)

    try:
        mgr = HeartbeatManager(
            shutdown_callback=lambda r: None,
            is_playing_callback=lambda: False,
            is_busy_callback=lambda: False,
        )
        now = time.monotonic()
        mgr.connect()
        mgr._last_seen_mono = now
        mgr.disconnect()
        assert mgr.should_shutdown(now=now + 30) is False
    finally:
        cfg._cached_config = old


def test_monitor_timeout_starts_before_first_websocket_connection():
    """桌面前端未能连上时，后端也不能永久残留。"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    try:
        mgr = HeartbeatManager(
            shutdown_callback=lambda reason: None,
            is_playing_callback=lambda: False,
            is_busy_callback=lambda: False,
        )
        mgr.start_monitor(interval=60)
        started_at = mgr._disconnected_mono

        assert started_at > 0
        assert mgr.should_shutdown(now=started_at + 11) is True
    finally:
        mgr.stop_monitor()
        cfg._cached_config = old


def test_disconnect_timeout_no_playing_triggers_shutdown():
    """连接断开超过 heartbeat_timeout 且未播放 → 触发 shutdown"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    shutdown_called = []
    mgr = HeartbeatManager(
        shutdown_callback=lambda r: shutdown_called.append(r),
        is_playing_callback=lambda: False,
    )
    now = time.monotonic()
    mgr.connect()
    # 手动设置 last_seen_mono
    mgr._last_seen_mono = now
    mgr.disconnect()
    # 断开时 _disconnected_mono 已设置

    # 30 秒后检查，timeout=10
    result = mgr.should_shutdown(now=now + 30)
    assert result is True

    cfg._cached_config = old


def test_disconnect_timeout_playing_no_shutdown():
    """连接断开超过 heartbeat_timeout 但正在播放 → 不触发 shutdown"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    shutdown_called = []
    mgr = HeartbeatManager(
        shutdown_callback=lambda r: shutdown_called.append(r),
        is_playing_callback=lambda: True,  # 正在播放
    )
    now = time.monotonic()
    mgr.connect()
    mgr._last_seen_mono = now
    mgr.disconnect()

    result = mgr.should_shutdown(now=now + 30)
    assert result is False

    cfg._cached_config = old


def test_disconnect_timeout_busy_task_no_shutdown():
    """连接断开超过 heartbeat_timeout 但后台任务运行中 → 不触发 shutdown"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    shutdown_called = []
    mgr = HeartbeatManager(
        shutdown_callback=lambda r: shutdown_called.append(r),
        is_playing_callback=lambda: False,
        is_busy_callback=lambda: True,
    )
    now = time.monotonic()
    mgr.connect()
    mgr._last_seen_mono = now
    mgr.disconnect()

    result = mgr.should_shutdown(now=now + 30)
    assert result is False

    cfg._cached_config = old


def test_disconnect_timeout_parent_alive_no_shutdown():
    """桌面父进程仍存活时，心跳断开不能误杀后端。"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    try:
        mgr = HeartbeatManager(
            shutdown_callback=lambda reason: None,
            is_playing_callback=lambda: False,
            is_busy_callback=lambda: False,
            is_parent_alive_callback=lambda: True,
        )
        now = time.monotonic()
        mgr.connect()
        mgr.disconnect()
        mgr._disconnected_mono = now

        assert mgr.should_shutdown(now=now + 30) is False
    finally:
        cfg._cached_config = old


def test_current_desktop_parent_process_is_detected(monkeypatch):
    """Windows 64 位 HANDLE 探测应能识别真实存活进程。"""
    from app.system.heartbeat import _default_is_parent_alive

    monkeypatch.setenv("KUMIPLAYER_PARENT_PID", str(os.getpid()))

    assert _default_is_parent_alive() is True


def test_active_heartbeat_timeout_triggers_shutdown():
    """active connection 心跳超时且未播放 → 触发 shutdown"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    shutdown_called = []
    mgr = HeartbeatManager(
        shutdown_callback=lambda r: shutdown_called.append(r),
        is_playing_callback=lambda: False,
    )
    now = time.monotonic()
    mgr.connect()
    mgr._last_seen_mono = now

    # 30 秒后检查，仍然连接但心跳超时
    result = mgr.should_shutdown(now=now + 30)
    assert result is True

    cfg._cached_config = old


def test_shutdown_only_once():
    """同一次超时只触发一次 shutdown_callback"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    shutdown_called = []
    mgr = HeartbeatManager(
        shutdown_callback=lambda r: shutdown_called.append(r),
        is_playing_callback=lambda: False,
    )
    now = time.monotonic()
    mgr.connect()
    mgr._last_seen_mono = now
    mgr.disconnect()
    mgr._disconnected_mono = now

    # 让 monitor_once 使用超时后的 monotonic 时间
    original_should_shutdown = mgr.should_shutdown
    mgr.should_shutdown = lambda now=None: original_should_shutdown(now=now + 30 if now is not None else time.monotonic() + 30)

    mgr.monitor_once()
    assert len(shutdown_called) == 1

    # 第二次 monitor_once 不应重复触发
    mgr.monitor_once()
    assert len(shutdown_called) == 1

    cfg._cached_config = old


def test_multi_connect_one_disconnect():
    """多连接场景：一个断开但仍有连接时不视为 disconnected"""
    from app.system.heartbeat import HeartbeatManager

    mgr = HeartbeatManager()
    mgr.connect()
    mgr.connect()
    assert mgr.get_state().active_connections == 2

    mgr.disconnect()
    state = mgr.get_state()
    assert state.active_connections == 1
    assert state.connected is True  # 仍有连接

    mgr.disconnect()
    state = mgr.get_state()
    assert state.active_connections == 0
    assert state.connected is False


def test_playing_blocks_then_later_shutdown():
    """超时时正在播放不退出，播放结束后下一轮 monitor 可退出"""
    from app.system.heartbeat import HeartbeatManager
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(
        heartbeat_enabled=True,
        heartbeat_timeout=10,
        auto_shutdown_on_heartbeat_timeout=True,
    )

    try:
        shutdown_called = []
        playing = {"value": True}
        mgr = HeartbeatManager(
            shutdown_callback=lambda r: shutdown_called.append(r),
            is_playing_callback=lambda: playing["value"],
        )
        now = time.monotonic()
        mgr.connect()
        mgr.disconnect()
        mgr._disconnected_mono = now

        original_should_shutdown = mgr.should_shutdown
        mgr.should_shutdown = lambda now=None: original_should_shutdown(now=now + 30 if now is not None else time.monotonic() + 30)

        mgr.monitor_once()
        assert shutdown_called == []
        assert mgr.get_state().shutdown_requested is False

        playing["value"] = False
        mgr.monitor_once()
        assert len(shutdown_called) == 1
        assert mgr.get_state().shutdown_requested is True
    finally:
        cfg._cached_config = old


if __name__ == "__main__":
    tests = [
        test_connect_sets_active,
        test_receive_heartbeat_updates_last_seen,
        test_disconnect_sets_disconnected,
        test_heartbeat_enabled_false_no_shutdown,
        test_disconnect_within_timeout_no_shutdown,
        test_disconnect_timeout_no_playing_triggers_shutdown,
        test_disconnect_timeout_playing_no_shutdown,
        test_active_heartbeat_timeout_triggers_shutdown,
        test_shutdown_only_once,
        test_multi_connect_one_disconnect,
        test_playing_blocks_then_later_shutdown,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
