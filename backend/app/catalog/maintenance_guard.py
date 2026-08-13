# -*- coding: utf-8 -*-
"""整库维护屏障（进程内）。

整库删除会在「初次任务门控 → 删除镜像/状态 → 事务化清理 Source Catalog」
之间有一段窗口：若持久任务恰好在此刻入队或被领取为 running，第二次事务复查
会抛 409，但此前已删除的文件/状态无法被 SQLite 回滚，造成"接口失败但部分已删"。

本屏障在删除全程置位，供任务入队（``app.pipeline.orchestrator.enqueue_*``）与
任务领取（``app.jobs.store.claim_jobs``）共同遵守：屏障期间拒绝产生新的相关
任务，从而保证删除全程不存在新的任务竞争，配合 DB 的 ``BEGIN IMMEDIATE``
复查形成双重保险。

屏障是进程内的：本项目后端为单进程 Tauri 子进程，worker 线程与删除调用共享
同一解释器，因此进程内标志足以覆盖所有入队/领取路径。
"""

from __future__ import annotations

import threading

_lock = threading.RLock()
_active = 0  # 进入层数（支持嵌套）


class MaintenanceBarrier:
    """上下文管理器：进入时置位屏障，退出时复位。"""

    def __enter__(self) -> MaintenanceBarrier:
        global _active
        with _lock:
            _active += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        global _active
        with _lock:
            _active = max(0, _active - 1)


def is_active() -> bool:
    """屏障是否处于活跃（删除进行中）。"""
    with _lock:
        return _active > 0


def hold() -> MaintenanceBarrier:
    """进入维护屏障（建议 ``with maintenance_guard.hold():`` 使用）。"""
    return MaintenanceBarrier()
