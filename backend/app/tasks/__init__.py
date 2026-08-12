# -*- coding: utf-8 -*-
"""异步任务包"""

from app.tasks.models import TaskRecord
from app.tasks.manager import TaskManager

__all__ = ["TaskRecord", "TaskManager"]
