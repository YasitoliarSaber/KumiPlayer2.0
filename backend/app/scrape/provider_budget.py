"""名称 Provider 的进程共享准入；429 排程等待，不占锁睡眠。"""

import math
import threading
import time

_lock = threading.Lock()
_budgets: dict[str, tuple[float, float]] = {}


class ProviderDeferred(Exception):
    def __init__(self, retry_after: int):
        super().__init__("provider request deferred")
        self.retry_after = max(1, retry_after)


def acquire(provider: str, interval: float, should_cancel=lambda: False) -> None:
    if should_cancel():
        raise RuntimeError("provider request cancelled")
    with _lock:
        current = time.monotonic()
        next_at, cooldown = _budgets.get(provider, (0.0, 0.0))
        if cooldown > current:
            raise ProviderDeferred(math.ceil(cooldown - current))
        slot = max(current, next_at)
        _budgets[provider] = (slot + interval, cooldown)
    while slot > time.monotonic():
        if should_cancel():
            raise RuntimeError("provider request cancelled")
        time.sleep(min(0.1, max(0.0, slot - time.monotonic())))
    if should_cancel():
        raise RuntimeError("provider request cancelled")


def cool_down(provider: str, seconds: int) -> None:
    with _lock:
        next_at, cooldown = _budgets.get(provider, (0.0, 0.0))
        _budgets[provider] = (next_at, max(cooldown, time.monotonic() + max(1, min(seconds, 86400))))
