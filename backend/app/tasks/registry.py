# -*- coding: utf-8 -*-
"""TaskManager 全局单例注册表

所有 API 模块统一从此处获取 TaskManager 实例。
测试可调用 reset_task_manager 清理线程池和状态。
"""

from app.tasks.manager import TaskManager

_task_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    """获取全局 TaskManager 单例"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager


def reset_task_manager() -> None:
    """重置全局 TaskManager（测试用）

    关闭已有线程池并清除引用，下次 get_task_manager 会创建新实例。
    """
    global _task_manager
    if _task_manager is not None:
        _task_manager.shutdown()
        _task_manager = None
