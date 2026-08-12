"""MediaUnit 收口（closure）唯一判断。

同一作品的 boundary 下所有「当前有效」的 source_directories checkpoint 必须
全部为 ``complete`` 状态，作品才算完整，才允许生成可执行版本（revision /
scrape）。任一 queued / scanning / failed（以及未来新增的任何非 complete
状态）都视为未完整。

- 正向定义（禁止反面白名单）：``all(state == 'complete')``，新状态天然阻塞；
- boundary 自身也计入相关集合（boundary 不是 complete 就不能收口）；
- 无任何相关 checkpoint（boundary 下从未确认过任何目录）→ False，不能把
  一个从未确认过的边界当成 complete；
- 「当前有效」= 存在且未被删除的 checkpoint 行（目录消失时由 store 层直接
  删除行，相关目录自然不在 relevant 集合中，两套语义自洽）。
"""

from __future__ import annotations

from app.db.database import get_connection

#: source_directories.state 中唯一允许收口的状态（与 store.py 枚举对齐：
#: queued / scanning / complete / failed）
_COMPLETE_STATE = "complete"


def is_boundary_complete(root_id: str, boundary: str) -> bool:
    """该 boundary 下所有当前有效的目录 checkpoint 必须全部 complete 才收口。"""
    prefix = boundary.rstrip("/") + "/"
    rows = get_connection().execute(
        """
        SELECT state FROM source_directories
        WHERE root_id = ? AND (remote_path = ? OR remote_path LIKE ?)
        """,
        (root_id, boundary, prefix + "%"),
    ).fetchall()
    if not rows:
        return False
    return all(row["state"] == _COMPLETE_STATE for row in rows)
