"""共享数据库事务 helper。

所有读写事务必须通过本模块开启（``BEGIN IMMEDIATE``），禁止调用方直接
``conn.execute("BEGIN")`` 拼事务。异常时回滚，成功时提交。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.db.database import get_connection


@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """开启一个立即事务；异常自动回滚，正常结束自动提交。"""
    conn = conn or get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
