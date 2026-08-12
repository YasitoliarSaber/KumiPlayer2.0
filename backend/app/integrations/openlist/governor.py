"""统一连接级请求限速与冷却准入（模块 1 阶段 B）。

职责：
- :class:`OpenListRequestGovernor`：按连接键（conn_key）串行化准入，
  同一连接请求间隔 >= 1/rate_per_second，不同连接互不阻塞；
- :func:`governor_connection_key`：由 server_url + username 计算匿名连接键
  （sha256 十六进制，绝不包含密码 / Token）；
- 模块级单例 :data:`_GOVERNOR`：进程内所有 OpenListClient 默认共享同一个
  governor，避免多个客户端/线程各自限速导致整体速率超限。

设计说明：
- 单例的 rate_per_second 是进程级安全默认（1 次/秒/连接，无 burst）；
- ``sleep`` 可注入（默认 time.sleep），测试可精确断言且不真实等待；
- 锁只保护状态读写（O(1)），sleep 在锁外执行：同一连接串行化间隔，
  不同连接互不阻塞。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable

#: 模块 1 安全默认：1 次/秒/连接，无 burst（原实例级 2.0 已收口到连接级）
DEFAULT_RATE_PER_SECOND = 1.0


def governor_connection_key(server_url: str, username: str) -> str:
    """连接级匿名键：sha256(f"{server_url}|{username}") 十六进制。

    - 绝不包含密码；
    - 不包含用户名/服务地址明文，可用于日志、状态表与健康记录 key。
    """
    raw = f"{server_url or ''}|{username or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OpenListRequestGovernor:
    """连接级请求限速器（线程安全）。

    ``acquire(conn_key)`` 保证同一 conn_key 的相邻请求间隔不小于
    ``1/rate_per_second`` 秒；不同 conn_key 互不阻塞（各自独立计时）。
    """

    def __init__(
        self,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate_per_second = rate_per_second
        self._sleep = sleep
        self._lock = threading.Lock()
        #: conn_key -> 上次准入的 monotonic 时间
        self._last_request_at: dict[str, float] = {}

    def acquire(self, conn_key: str) -> None:
        """按 conn_key 串行化准入；需要等待时 sleep（锁外等待，互不阻塞）。

        实现要点：锁内判定并**预留**下一个窗口（``_last_request_at[conn_key]``
        推进为 last + interval），锁外只 sleep 一次。这样：
        - 限速不依赖 sleep 实际消耗的时间（注入型 sleep 立即返回也不会破坏
          队列时间线，后续同 key 线程按预留窗口排队）；
        - 不同 conn_key 互不阻塞（锁内只做 O(1) 读写）。
        """
        rate = self.rate_per_second
        if rate <= 0:
            return
        interval = 1.0 / rate
        with self._lock:
            now = time.monotonic()
            last = self._last_request_at.get(conn_key, 0.0)
            if now >= last + interval:
                self._last_request_at[conn_key] = now
                return
            wait = last + interval - now
            # 预留窗口：同 key 后续线程在此时间线基础上排队
            self._last_request_at[conn_key] = last + interval
        if wait > 0:
            self._sleep(wait)
            # 校准：sleep 期间可能被调度延迟，把预留时间线推进到实际时刻，
            # 防止同 key 后续线程在延迟窗口上紧贴连发。
            with self._lock:
                now = time.monotonic()
                if now > self._last_request_at.get(conn_key, 0.0):
                    self._last_request_at[conn_key] = now


#: 进程内共享单例：所有未显式指定 governor 的 OpenListClient 默认接入
_GOVERNOR: OpenListRequestGovernor | None = None
_GOVERNOR_LOCK = threading.Lock()


def get_governor() -> OpenListRequestGovernor:
    """获取进程内共享的 OpenListRequestGovernor 单例。"""
    global _GOVERNOR
    if _GOVERNOR is None:
        with _GOVERNOR_LOCK:
            if _GOVERNOR is None:
                _GOVERNOR = OpenListRequestGovernor()
    return _GOVERNOR
