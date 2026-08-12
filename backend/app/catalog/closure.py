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
    """该 boundary 自身及全部当前有效后代目录必须 complete 才收口。"""
    normalized = boundary.rstrip("/") or "/"
    prefix = "/" if normalized == "/" else normalized + "/"

    rows = get_connection().execute(
        """
        SELECT remote_path, state
        FROM source_directories
        WHERE root_id = ?
          AND (
              remote_path = ?
              OR (
                  substr(remote_path, 1, length(?)) = ?
                  AND length(remote_path) > length(?)
              )
          )
        """,
        (root_id, normalized, prefix, prefix, prefix),
    ).fetchall()

    if not rows:
        return False

    boundary_seen = False
    for row in rows:
        if row["remote_path"] == normalized:
            boundary_seen = True
        if row["state"] != _COMPLETE_STATE:
            return False

    return boundary_seen
