# -*- coding: utf-8 -*-
"""异步任务数据结构"""

from dataclasses import dataclass, field
from typing import Optional


class TaskCancelledError(RuntimeError):
    """用户主动停止任务。它是正常终态，不属于执行失败。"""

    def __init__(self, message: str = "任务已停止"):
        super().__init__(message)


@dataclass
class TaskRecord:
    """任务记录"""

    task_id: str = ""
    task_type: str = ""        # mirror_generate / scrape_auto / library_rescan / source_parse
    source: str = ""           # pan115 / baidu / local
    status: str = "pending"    # pending / running / succeeded / failed / cancelled
    progress: int = 0          # 0-100
    message: str = ""
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: dict = field(default_factory=dict)
