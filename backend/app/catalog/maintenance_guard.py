# -*- coding: utf-8 -*-
"""整库维护屏障（进程内 admission gate）。

删除全程需要与任务入队/领取、导入批次创建互斥，且「检查屏障 → 写 SQLite」
必须原子：

- 普通操作（任务创建/领取、导入批次）先获取 admission（共享读）：
  屏障未激活时进入，**持有直到其 SQLite 事务提交**才释放；
- 删除（屏障）获取独占写：进入时**等待所有已进入的 admission 操作完成**，
  然后置位屏障；屏障激活期间新的 admission 操作被拒绝（入队抛异常、
  领取返回空、导入返回 409）。

由此保证不存在「检查通过后、事务提交前被删除插入」的窗口，也不存在
「批次已提交但入队被拒」的半成品导入。本项目后端为单进程 Tauri 子进程，
worker 线程与删除调用共享同一解释器，进程内锁即可覆盖全部路径。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class MaintenanceAdmissionDenied(RuntimeError):
    """媒体库维护进行中，admission 操作被拒绝。"""


class _AdmissionGate:
    """读写互斥门：admission(共享) 与 barrier(独占) 互斥。

    - ``acquire``：barrier 未激活时进入共享段并计数；激活时返回 False。
    - ``enter_barrier``：等待共享段清零后置位独占屏障（删除全程）。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._local = threading.local()
        self._active = 0  # 在途 admission 操作数
        self._barrier = 0  # 屏障层数（支持嵌套）
        self._barrier_owner: int | None = None
        self._waiting_barriers = 0  # 已请求独占、正在等待在途 admission 的删除数

    def acquire(self) -> bool:
        """尝试进入共享段；屏障激活时返回 False（不进入）。"""
        if getattr(self._local, "admission_depth", 0) > 0:
            self._local.admission_depth += 1
            return True
        with self._cond:
            # 删除一旦开始等待，后续 admission 不能插队，否则持续导入/领取会
            # 令整库删除永久饥饿。已进入的 admission 仍由删除方等待其提交完成。
            if self._barrier > 0 or self._waiting_barriers > 0:
                return False
            self._active += 1
            self._local.admission_depth = 1
            return True

    def release(self) -> None:
        """退出共享段；清零时唤醒等待的删除方。"""
        depth = getattr(self._local, "admission_depth", 0)
        if depth > 1:
            self._local.admission_depth = depth - 1
            return
        if depth == 1:
            self._local.admission_depth = 0
        with self._cond:
            self._active = max(0, self._active - 1)
            if self._active == 0:
                self._cond.notify_all()

    def enter_barrier(self) -> None:
        """删除进入：等待 admission 与其他删除完成，然后独占置位屏障。"""
        with self._cond:
            owner = threading.get_ident()
            if self._barrier_owner == owner:
                self._barrier += 1
                return
            self._waiting_barriers += 1
            try:
                while self._active > 0 or self._barrier_owner is not None:
                    self._cond.wait()
                self._barrier_owner = owner
                self._barrier += 1
            finally:
                self._waiting_barriers = max(0, self._waiting_barriers - 1)
                self._cond.notify_all()

    def exit_barrier(self) -> None:
        """删除退出：复位屏障并唤醒等待的 admission 操作。"""
        with self._cond:
            if self._barrier_owner != threading.get_ident():
                raise RuntimeError("维护屏障只能由持有它的线程退出")
            self._barrier -= 1
            if self._barrier == 0:
                self._barrier_owner = None
                self._cond.notify_all()

    def is_active(self) -> bool:
        with self._cond:
            return self._barrier > 0

    def reset_for_tests(self) -> None:
        """测试专用：清空计数，避免失败用例泄漏状态阻塞后续用例。"""
        with self._cond:
            self._active = 0
            self._barrier = 0
            self._barrier_owner = None
            self._waiting_barriers = 0
            self._local.admission_depth = 0
            self._cond.notify_all()


_gate = _AdmissionGate()


def is_active() -> bool:
    """屏障是否激活（删除进行中）。"""
    return _gate.is_active()


@contextmanager
def admission() -> Iterator[None]:
    """任务/导入准入临界区。

    屏障激活时抛 :class:`MaintenanceAdmissionDenied`（调用方决定 409/空结果）；
    否则持有准入直到上下文结束——调用方必须把「检查 → SQLite 事务提交」全部
    放在上下文内，删除方会等待本操作完成才置位屏障。
    """
    if not _gate.acquire():
        raise MaintenanceAdmissionDenied("媒体库维护进行中，暂不接受新的任务")
    try:
        yield
    finally:
        _gate.release()


class MaintenanceBarrier:
    """删除屏障：进入时等待在途 admission 完成并置位，退出时复位。"""

    def __enter__(self) -> MaintenanceBarrier:
        _gate.enter_barrier()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        _gate.exit_barrier()


def hold() -> MaintenanceBarrier:
    """进入删除屏障（建议 ``with maintenance_guard.hold():`` 使用）。"""
    return MaintenanceBarrier()
