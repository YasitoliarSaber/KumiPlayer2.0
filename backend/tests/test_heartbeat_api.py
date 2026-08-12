# -*- coding: utf-8 -*-
"""WebSocket 心跳 API 测试"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from app.main import app


def _cleanup():
    from app.system.heartbeat import reset_heartbeat_manager
    reset_heartbeat_manager()


def test_ws_connect():
    """连接 /ws/heartbeat 成功"""
    _cleanup()
    try:
        client = TestClient(app)
        with client.websocket_connect("/ws/heartbeat") as ws:
            # 连接成功
            from app.system.heartbeat import get_heartbeat_manager
            state = get_heartbeat_manager().get_state()
            assert state.active_connections >= 1
    finally:
        _cleanup()


def test_ws_heartbeat_text():
    """发送 "heartbeat" 收到 heartbeat_ack"""
    _cleanup()
    try:
        client = TestClient(app)
        with client.websocket_connect("/ws/heartbeat") as ws:
            ws.send_text("heartbeat")
            data = ws.receive_text()
            resp = json.loads(data)
            assert resp["type"] == "heartbeat_ack"
            assert "server_time" in resp
    finally:
        _cleanup()


def test_ws_heartbeat_json():
    """发送 {"type":"heartbeat"} 收到 heartbeat_ack"""
    _cleanup()
    try:
        client = TestClient(app)
        with client.websocket_connect("/ws/heartbeat") as ws:
            ws.send_text(json.dumps({"type": "heartbeat"}))
            data = ws.receive_text()
            resp = json.loads(data)
            assert resp["type"] == "heartbeat_ack"
    finally:
        _cleanup()


def test_ws_disconnect_updates_state():
    """断开后 manager 状态更新"""
    _cleanup()
    try:
        from app.system.heartbeat import get_heartbeat_manager
        client = TestClient(app)
        with client.websocket_connect("/ws/heartbeat") as ws:
            ws.send_text("heartbeat")
            ws.receive_text()

        # 断开后检查
        state = get_heartbeat_manager().get_state()
        assert state.active_connections == 0
        assert state.connected is False
    finally:
        _cleanup()


def test_ws_heartbeat_enabled_still_acks():
    """heartbeat_enabled=false 时连接仍可 ack"""
    _cleanup()
    try:
        from app.core.config import AppConfig
        import app.core.config as cfg

        old = cfg._cached_config
        cfg._cached_config = AppConfig(heartbeat_enabled=False)

        client = TestClient(app)
        with client.websocket_connect("/ws/heartbeat") as ws:
            ws.send_text("heartbeat")
            data = ws.receive_text()
            resp = json.loads(data)
            assert resp["type"] == "heartbeat_ack"

        cfg._cached_config = old
    finally:
        _cleanup()


if __name__ == "__main__":
    tests = [
        test_ws_connect,
        test_ws_heartbeat_text,
        test_ws_heartbeat_json,
        test_ws_disconnect_updates_state,
        test_ws_heartbeat_enabled_still_acks,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
