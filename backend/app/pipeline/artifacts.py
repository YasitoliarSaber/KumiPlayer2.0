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


def upsert_artifact(*, kind: str, path: str, revision_id: str, work_id: str) -> None:
    """登记一个已确认物化的本地产物；同 path 新 revision 重写则更新归属。"""
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"不支持的 artifact kind: {kind}")
    if not path:
        raise ValueError("artifact path 不能为空")
    conn = get_connection()
    timestamp = revision_store.now_iso()
    conn.execute(
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
    conn.commit()
