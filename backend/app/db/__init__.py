# -*- coding: utf-8 -*-
"""数据库模块

启动时 init_db() 初始化表结构。
"""

from app.db.database import init_db, get_connection, close_connection

__all__ = ["init_db", "get_connection", "close_connection"]
