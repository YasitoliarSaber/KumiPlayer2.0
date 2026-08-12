# -*- coding: utf-8 -*-
"""Local watch-status store for works.

This is KumiPlayer's own media-library state for filtering and display.  It
must not be used as an automatic source for Bangumi collection changes; those
remote changes require an explicit user action after a confirmed subject match.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal, Optional

from app.core.paths import get_cache_dir
from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK

WatchStatusValue = Literal["", "watching", "watched", "on_hold", "dropped"]
VALID_WATCH_STATUS = {"", "watching", "watched", "on_hold", "dropped"}


@dataclass
class WorkWatchStatus:
    work_id: str
    status: WatchStatusValue = ""
    note: str = ""
    favorite: bool = False
    updated_at: str = ""


def _status_path() -> Path:
    return get_cache_dir() / "watch_status.json"


def load_watch_statuses() -> dict[str, WorkWatchStatus]:
    path = _status_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    items: dict[str, WorkWatchStatus] = {}
    for raw in data.get("items", []):
        work_id = str(raw.get("work_id") or "")
        if not work_id:
            continue
        status = str(raw.get("status") or "")
        if status not in VALID_WATCH_STATUS:
            status = ""
        items[work_id] = WorkWatchStatus(
            work_id=work_id,
            status=status,  # type: ignore[arg-type]
            note=str(raw.get("note") or ""),
            favorite=bool(raw.get("favorite", False)),
            updated_at=str(raw.get("updated_at") or ""),
        )
    return items


def save_watch_statuses(items: dict[str, WorkWatchStatus]) -> None:
    with DATA_WRITE_LOCK:
        path = _status_path()
        payload = {
            "version": 1,
            "items": [asdict(item) for item in sorted(items.values(), key=lambda value: value.work_id)],
        }
        write_json_atomic(path, payload)


def get_watch_status(work_id: str) -> WorkWatchStatus:
    return load_watch_statuses().get(work_id) or WorkWatchStatus(work_id=work_id)


def set_watch_status(
    work_id: str,
    status: str,
    note: str = "",
    favorite: Optional[bool] = None,
) -> WorkWatchStatus:
    with DATA_WRITE_LOCK:
        if status not in VALID_WATCH_STATUS:
            raise ValueError("未知观看状态")
        items = load_watch_statuses()
        previous = items.get(work_id) or WorkWatchStatus(work_id=work_id)
        resolved_favorite = previous.favorite if favorite is None else favorite
        item = WorkWatchStatus(
            work_id=work_id,
            status=status,  # type: ignore[arg-type]
            note=note,
            favorite=resolved_favorite,
            updated_at=_now() if status or note or resolved_favorite else "",
        )
        if status == "" and note == "" and not resolved_favorite:
            items.pop(work_id, None)
        else:
            items[work_id] = item
        save_watch_statuses(items)
        return item


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()
