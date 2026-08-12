"""job handler 注册表。

handler 只通过注册表名称恢复，不把 Python callable 存进数据库；
payload 必须可 JSON 序列化并通过白名单校验。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

#: 注册表：名称 -> 可调用（payload, progress_callback, should_cancel）-> dict
_HANDLERS: dict[str, Callable[..., dict]] = {}

_ALLOWED_TYPES = (dict, list, str, int, float, bool, type(None))


def validate_payload(payload: Any) -> dict:
    """白名单校验：只允许 JSON 标量/容器，拒绝任意对象。"""
    def check(value: Any, path: str = "payload") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} 含非字符串键")
                check(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check(item, f"{path}[{index}]")
        elif not isinstance(value, _ALLOWED_TYPES):
            raise ValueError(f"{path} 含不允许的类型: {type(value).__name__}")

    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 dict")
    check(payload)
    json.dumps(payload, ensure_ascii=False)  # 确保可 JSON 序列化
    return payload


def register(name: str, handler: Callable[..., dict]) -> None:
    """注册可恢复 handler。重复注册同一名称时覆盖（进程内最新定义）。"""
    if not name or not callable(handler):
        raise ValueError("handler 名称与可调用对象必须有效")
    _HANDLERS[name] = handler


def unregister(name: str) -> None:
    _HANDLERS.pop(name, None)


def get_handler(name: str) -> Callable[..., dict] | None:
    return _HANDLERS.get(name)


def registered_names() -> list[str]:
    return sorted(_HANDLERS)
