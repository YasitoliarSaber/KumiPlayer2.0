from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from app.db.database import close_connection, get_connection


@dataclass
class WorkOverride:
    work_id: str
    poster_path: str = ""
    fanart_path: str = ""
    clearlogo_path: str = ""
    metadata: dict | None = None
    updated_at: str = ""


def get_work_override(work_id: str) -> WorkOverride | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM work_overrides WHERE work_id = ?", (work_id,)).fetchone()
    close_connection()
    if not row:
        return None
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.get("metadata") or "{}")
    except json.JSONDecodeError:
        data["metadata"] = {}
    return WorkOverride(**data)


def set_artwork_override(work_id: str, kind: str, path: str) -> WorkOverride:
    if kind not in {"poster", "fanart", "clearlogo"}:
        raise ValueError("未知图片类型")
    item = get_work_override(work_id) or WorkOverride(work_id=work_id, metadata={})
    item = replace(item, **{f"{kind}_path": path}, updated_at=_now())
    _save(item)
    return item


def clear_artwork_override(work_id: str, kind: str) -> WorkOverride:
    return set_artwork_override(work_id, kind, "")


def set_title_override(work_id: str, title: str) -> WorkOverride:
    normalized = " ".join((title or "").split()).strip()
    if not normalized:
        raise ValueError("作品标题不能为空")
    if len(normalized) > 160:
        raise ValueError("作品标题不能超过 160 个字符")
    item = get_work_override(work_id) or WorkOverride(work_id=work_id, metadata={})
    metadata = dict(item.metadata or {})
    metadata["title"] = normalized
    item = replace(item, metadata=metadata, updated_at=_now())
    _save(item)
    return item


def clear_title_override(work_id: str) -> WorkOverride:
    item = get_work_override(work_id) or WorkOverride(work_id=work_id, metadata={})
    metadata = dict(item.metadata or {})
    metadata.pop("title", None)
    item = replace(item, metadata=metadata, updated_at=_now())
    _save(item)
    return item


def _save(item: WorkOverride) -> None:
    conn = get_connection()
    conn.execute("""
        INSERT INTO work_overrides (
            work_id, poster_path, fanart_path, clearlogo_path, metadata, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
            poster_path=excluded.poster_path, fanart_path=excluded.fanart_path,
            clearlogo_path=excluded.clearlogo_path, metadata=excluded.metadata,
            updated_at=excluded.updated_at
    """, (
        item.work_id, item.poster_path, item.fanart_path, item.clearlogo_path,
        json.dumps(item.metadata or {}, ensure_ascii=False), item.updated_at,
    ))
    conn.commit()
    close_connection()


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()
