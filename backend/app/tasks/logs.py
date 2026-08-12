# -*- coding: utf-8 -*-
"""统一任务日志工具

pipeline / mirror / scrape 所有真实执行任务共用一个日志 schema：
    {"time": iso, "kind": ..., "message": ...}
最多保留最近 TASK_LOG_LIMIT 条（160）。

kind 只承载日志呈现语义（info/done/warn/error/search），不参与任何业务判断。
解析阶段的派生日志没有逐条真实执行时间，不使用本工具。
"""

from datetime import datetime, timedelta, timezone

TASK_LOG_LIMIT = 160

_ALLOWED_KINDS = {"info", "done", "warn", "error", "search"}


def now_iso() -> str:
    """生成东八区 ISO 时间戳。"""
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def append_task_log(logs: list, message: str, kind: str = "info") -> None:
    """原地追加一条任务日志并截断为最近 TASK_LOG_LIMIT 条。"""
    if kind not in _ALLOWED_KINDS:
        kind = "info"
    logs.append({"time": now_iso(), "kind": kind, "message": message})
    del logs[:-TASK_LOG_LIMIT]
