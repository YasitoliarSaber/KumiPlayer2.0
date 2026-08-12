"""OpenList 目录清单序列化。

把一次扫描的远端条目序列化为 KumiPlayer 自有格式的版本清单
（不伪造 115 / 百度 TXT），原子写入 ``data/openlist_manifests/``，
并用 SHA-256 做去重与审计。

清单内容只包含白名单字段；直链、缩略图、哈希、存储内部路径不落盘。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir
from app.integrations.openlist.models import OpenListEntry

MANIFEST_FORMAT = "kumiplayer-openlist-manifest"
MANIFEST_FORMAT_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def canonical_sha256(entries: list[OpenListEntry]) -> str:
    """按排序后的 (remote_path, is_dir, size, modified) 计算内容哈希。

    排序保证两次扫描即使遍历顺序不同，相同目录内容也得到相同哈希。
    """
    lines = []
    for entry in sorted(entries, key=lambda item: item.remote_path):
        lines.append(
            "\t".join(
                (
                    entry.remote_path,
                    "d" if entry.is_dir else "f",
                    str(entry.size if entry.size is not None else ""),
                    str(int(entry.modified) if entry.modified is not None else ""),
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def manifest_dir() -> Path:
    directory = get_data_dir() / "openlist_manifests"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_manifest(
    manifest_id: str,
    entries: list[OpenListEntry],
    *,
    remote_locator: str,
    source_root: str,
    provider_id: str = "",
    ingest_method: str = "openlist_api",
    source_route_id: str = "",
) -> tuple[Path, str]:
    """原子写入清单，返回 (路径, sha256)。"""
    content_hash = canonical_sha256(entries)
    payload = {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "manifest_id": manifest_id,
        "source": "openlist",
        "provider_id": provider_id,
        "ingest_method": ingest_method,
        "source_route_id": source_route_id,
        "remote_locator": remote_locator,
        "source_root": source_root,
        "created_at": now_iso(),
        "sha256": content_hash,
        "entry_count": len(entries),
        "entries": [
            {
                "name": entry.name,
                "is_dir": entry.is_dir,
                "size": entry.size,
                "modified": entry.modified,
                "remote_path": entry.remote_path,
                "depth": entry.depth,
            }
            for entry in entries
        ],
    }
    path = manifest_dir() / f"{manifest_id}.json"
    with DATA_WRITE_LOCK:
        write_json_atomic(path, payload)
    return path, content_hash


def read_manifest(path: Path) -> dict:
    """读取清单；格式或字段非法时返回空 dict（不抛异常）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        return {}
    return data
