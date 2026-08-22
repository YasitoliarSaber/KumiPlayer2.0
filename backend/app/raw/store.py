# -*- coding: utf-8 -*-
"""RawSnapshot JSON 持久化

保存路径：
data/raw_snapshots/{snapshot_id}.json
data/raw_snapshots/{source}_latest.json
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from app.raw.models import RawFile, RawSnapshot
from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK


def _get_snapshots_dir() -> Path:
    from app.core.paths import get_data_dir
    snapshots_dir = get_data_dir() / "raw_snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    return snapshots_dir


def save_raw_snapshot(snapshot: RawSnapshot, update_latest: bool = True) -> str:
    """保存 RawSnapshot

    保存到：
    - data/raw_snapshots/{snapshot_id}.json
    - data/raw_snapshots/{source}_latest.json

    返回:
        保存路径
    """
    with DATA_WRITE_LOCK:
        snapshots_dir = _get_snapshots_dir()
        data = asdict(snapshot)
        path = snapshots_dir / f"{snapshot.snapshot_id}.json"
        write_json_atomic(path, data)
        if update_latest:
            latest_path = snapshots_dir / f"{snapshot.source}_latest.json"
            write_json_atomic(latest_path, data)
        return str(path)


def load_raw_snapshot(snapshot_id: str) -> Optional[RawSnapshot]:
    """加载指定 RawSnapshot"""
    snapshots_dir = _get_snapshots_dir()
    path = snapshots_dir / f"{snapshot_id}.json"
    if not path.exists():
        return None
    return _load_from_path(path)


def load_latest_raw_snapshot(source: str) -> Optional[RawSnapshot]:
    """加载指定来源最新的来源级 RawSnapshot。

    旧版本可能让单部作品追更切片覆盖 ``{source}_latest.json``。读取时会
    自动跳过这种切片并回退到最近的全量快照，避免增量 diff 误删整库。
    """
    snapshots_dir = _get_snapshots_dir()
    path = snapshots_dir / f"{source}_latest.json"
    if path.exists():
        latest = _load_from_path(path)
        if latest and latest.source == source and not _is_scoped_tracking_snapshot(latest):
            return latest

    best_snapshot = None
    best_time = 0.0
    for candidate_path in snapshots_dir.glob("*.json"):
        if "_latest" in candidate_path.name:
            continue
        snapshot = _load_from_path(candidate_path)
        if (
            snapshot is None
            or snapshot.source != source
            or _is_scoped_tracking_snapshot(snapshot)
        ):
            continue
        try:
            mtime = candidate_path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_time:
            best_time = mtime
            best_snapshot = snapshot
    return best_snapshot


def _is_scoped_tracking_snapshot(snapshot: RawSnapshot) -> bool:
    """作品级追更快照不能充当整个来源的增量基线。"""
    return "_tracking_" in (snapshot.snapshot_id or "")


def _load_from_path(path: Path) -> Optional[RawSnapshot]:
    """从文件加载 RawSnapshot"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = [RawFile(**f) for f in data.get("files", [])]
        return RawSnapshot(
            snapshot_id=data.get("snapshot_id", ""),
            source=data.get("source", ""),
            provider_id=data.get("provider_id", ""),
            ingest_method=data.get("ingest_method", ""),
            source_route_id=data.get("source_route_id", ""),
            source_root=data.get("source_root", ""),
            root_container=data.get("root_container", ""),
            import_family=data.get("import_family", ""),
            import_scope=data.get("import_scope", ""),
            created_at=data.get("created_at", ""),
            input_file=data.get("input_file", ""),
            file_count=data.get("file_count", 0),
            video_count=data.get("video_count", 0),
            files=files,
        )
    except (json.JSONDecodeError, KeyError):
        return None
