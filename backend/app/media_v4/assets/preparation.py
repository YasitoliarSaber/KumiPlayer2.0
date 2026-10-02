"""单线程、有界、可停止的旧图片缓存补齐队列。媒体事实仍只来自 V4 投影。"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path

from .thumbnails import cache_path, find_thumbnail, get_or_create_thumbnail


class ThumbnailPreparer:
    def __init__(self, capacity: int = 2048):
        self.capacity = capacity
        self._pending: dict[tuple[Path, int], int] = {}
        self._active: tuple[Path, int] | None = None
        self._failed: OrderedDict[str, float] = OrderedDict()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._stopped = False
            self._thread = threading.Thread(target=self._run, name="artwork-thumbnails", daemon=True)
            self._thread.start()

    def enqueue(self, source: Path, width: int, *, priority: int = 0) -> bool:
        key = (source, width)
        with self._condition:
            if self._stopped:
                return False
            if key == self._active:
                return True
            if key in self._pending:
                self._pending[key] = min(priority, self._pending[key])
                return True
            if len(self._pending) >= self.capacity:
                worst = max(self._pending, key=self._pending.__getitem__)
                if self._pending[worst] <= priority:
                    return False
                del self._pending[worst]
            self._pending[key] = priority
            self._condition.notify()
            return True

    def status(self, source: Path, width: int, *, priority: int = 0) -> str:
        if find_thumbnail(source, width) is not None:
            return "ready"
        try:
            cache_key = str(cache_path(source, width))
        except OSError:
            return "unavailable"
        with self._condition:
            failed_at = self._failed.get(cache_key)
            if failed_at is not None:
                if time.monotonic() - failed_at < 30:
                    return "retry"
                del self._failed[cache_key]
        # 可见图片优先于旧库补齐，队列满时由下一次批量查询重试。
        self.enqueue(source, width, priority=priority)
        return "pending"

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or bool(self._pending))
                if self._stopped:
                    return
                key = min(self._pending, key=self._pending.__getitem__)
                del self._pending[key]
                self._active = key
            source, width = key
            failed_key = None
            try:
                failed_key = str(cache_path(source, width))
                result = get_or_create_thumbnail(source, width)
            except Exception:
                result = None
            finally:
                with self._condition:
                    if result is None and failed_key:
                        self._failed[failed_key] = time.monotonic()
                        while len(self._failed) > self.capacity:
                            self._failed.popitem(last=False)
                    self._active = None

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._pending.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._condition:
            self._thread = None
            self._failed.clear()


thumbnail_preparer = ThumbnailPreparer()
