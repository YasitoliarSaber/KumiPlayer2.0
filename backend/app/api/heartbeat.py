# -*- coding: utf-8 -*-
"""WebSocket 心跳端点

WS /ws/heartbeat
"""

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.api_security import websocket_token_is_valid
from app.system.heartbeat import get_heartbeat_manager

router = APIRouter()


@router.websocket("/ws/heartbeat")
async def heartbeat_ws(websocket: WebSocket):
    """WebSocket 心跳端点

    协议：
    - 客户端每 10 秒发送 {"type": "heartbeat"} 或 "heartbeat"
    - 服务端回复 {"type": "heartbeat_ack", "server_time": "..."}
    - 断开时更新状态
    """
    if not websocket_token_is_valid(websocket.scope.get("query_string", b"")):
        await websocket.close(code=1008, reason="KumiPlayer desktop session required")
        return

    manager = get_heartbeat_manager()
    await websocket.accept()
    manager.connect()

    try:
        while True:
            await websocket.receive_text()
            # 收到任何消息都视为心跳
            manager.receive_heartbeat()

            # 回复 ack
            from datetime import datetime, timezone, timedelta
            server_time = datetime.now(timezone(timedelta(hours=8))).isoformat()
            ack = json.dumps({"type": "heartbeat_ack", "server_time": server_time})
            await websocket.send_text(ack)
    except WebSocketDisconnect:
        manager.disconnect()
    except Exception:
        manager.disconnect()
