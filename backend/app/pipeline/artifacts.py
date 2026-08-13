"""Artifact Store：已物化文件/产物的统一登记（SQLite artifact_records）。

Module 5 规划员拍板：
- artifact_records = 已物化文件/产物登记（projection），不是语义事实；
- 同一 (kind, path) 被新 revision 重新生成时，attribution 必须切到当前 revision
  （ON CONFLICT(kind, path) DO UPDATE），不能继续 INSERT OR IGNORE。
"""

from __future__ import annotations

import uuid

from app.db.database import get_connection
from app.import_plan import revision_store

#: 允许登记的产物 kind（STRM / NFO / 本地图片）
ARTIFACT_KINDS = ("strm", "nfo", "poster", "fanart", "clearlogo")


class StaleRevisionError(RuntimeError):
    """V3 事实写入被拒：目标 revision 已不再是 current（stale/superseded）。"""


def upsert_artifact(
    *,
    kind: str,
    path: str,
    revision_id: str,
    work_id: str,
    require_current: bool = False,
) -> None:
    """登记一个已确认物化的本地产物；同 path 新 revision 重写则更新归属。

    Review Fix 2：``require_current=True``（V3 mirror/scrape）时，current
    检查与 upsert 在同一个 ``BEGIN IMMEDIATE`` 写事务内完成——写入前 revision
    已切换 current 的 stale worker 会被拒（StaleRevisionError），不能把
    attribution 抢回旧 revision。legacy/默认保持 False 不误杀。
    """
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"不支持的 artifact kind: {kind}")
    if not path:
        raise ValueError("artifact path 不能为空")
    from app.db.transactions import transaction
    from app.import_plan import revision_store

    conn = get_connection()
    timestamp = revision_store.now_iso()
    with transaction(conn) as tx:
        if require_current and not revision_store.is_current_revision(revision_id):
            raise StaleRevisionError(
                f"artifact 写入被拒：revision {revision_id} 已不再是 current"
            )
        tx.execute(
            """
            INSERT INTO artifact_records (artifact_id, kind, path, revision_id, work_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (kind, path) DO UPDATE SET
                revision_id = excluded.revision_id,
                work_id = excluded.work_id,
                created_at = excluded.created_at
            """,
            (uuid.uuid4().hex, kind, path, revision_id, work_id, timestamp),
        )
