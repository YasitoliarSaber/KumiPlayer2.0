"""OpenList 单层目录浏览的本地持久缓存。

职责：
- 按「连接身份 + 规范化远端路径」保存单层目录列表，加速浏览、减少上游请求；
- 是浏览体验加速层，不是媒体库真相；已导入目录的完整快照/清单另存（manifest.py）；
- 只缓存白名单字段，禁止缓存用户名、密码、Token、Authorization、直链、
  缩略图或 OpenList 原始内部 path；
- 每个目录一个 JSON 文件 + 每连接一个 LRU 索引，容量超限按最近使用淘汰，
  不随 10T 目录增长而反复整文件重写巨型 JSON。

布局：``data/openlist_cache/conn_{conn_hash}/{path_hash}.json`` + ``index.json``
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir
from app.integrations.openlist.client import normalize_remote_path
from app.integrations.openlist.models import OpenListEntry

#: 每个连接最多缓存的目录数
MAX_CACHE_PATHS = 2000
#: 每个连接缓存总字节上限（近似按文件大小统计）
MAX_CACHE_BYTES = 64 * 1024 * 1024

#: 缓存条目白名单字段（其余字段一律不落盘）
_ENTRY_FIELDS = ("name", "is_dir", "size", "modified", "remote_path")

_cache_lock = threading.RLock()


def connection_key(server_url: str, username: str, remote_root: str) -> str:
    """连接身份哈希：server_url + username + remote_root 的组合指纹。

    不包含密码；Token 从不进入缓存模块。
    """
    raw = f"{server_url}|{username}|{normalize_remote_path(remote_root or '/')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _path_key(remote_path: str) -> str:
    return hashlib.sha256(normalize_remote_path(remote_path).encode("utf-8")).hexdigest()[:16]


def _cache_root(conn_key: str) -> Path:
    return get_data_dir() / "openlist_cache" / f"conn_{conn_key}"


def _entry_file(conn_key: str, remote_path: str) -> Path:
    return _cache_root(conn_key) / f"{_path_key(remote_path)}.json"


def _index_path(conn_key: str) -> Path:
    return _cache_root(conn_key) / "index.json"


def _read_index(conn_key: str) -> dict:
    path = _index_path(conn_key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("paths"), dict):
            return data["paths"]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


def _write_index(conn_key: str, paths: dict) -> None:
    root = _cache_root(conn_key)
    root.mkdir(parents=True, exist_ok=True)
    with DATA_WRITE_LOCK:
        write_json_atomic(_index_path(conn_key), {"version": 1, "paths": paths})


def _entry_to_whitelist(entry: OpenListEntry) -> dict:
    return {
        "name": entry.name,
        "is_dir": entry.is_dir,
        "size": entry.size,
        "modified": entry.modified,
        "remote_path": entry.remote_path,
    }


def _entries_from_cache(payload: dict) -> list[dict]:
    entries = payload.get("entries") or []
    result = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        result.append({key: item.get(key) for key in _ENTRY_FIELDS})
    return result


def content_hash(entries: list[dict]) -> str:
    """按白名单字段排序计算内容哈希，用于缓存命中判断与审计。"""
    lines = []
    for entry in sorted(entries, key=lambda item: str(item.get("remote_path") or "")):
        lines.append(
            "\t".join(
                (
                    str(entry.get("remote_path") or ""),
                    "d" if entry.get("is_dir") else "f",
                    str(entry.get("size") if entry.get("size") is not None else ""),
                    str(int(entry.get("modified")) if entry.get("modified") is not None else ""),
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _evict_locked(conn_key: str, index: dict) -> None:
    """容量淘汰：按 last_accessed_at 升序删除，直到低于路径数/字节上限。"""
    if len(index) <= MAX_CACHE_PATHS:
        total = sum(int(item.get("size_bytes") or 0) for item in index.values())
        if total <= MAX_CACHE_BYTES:
            return
    ordered = sorted(
        index.items(),
        key=lambda item: (item[1].get("last_accessed_at") or 0, item[0]),
    )
    for path_key, _meta in ordered:
        if len(index) <= MAX_CACHE_PATHS:
            total = sum(int(item.get("size_bytes") or 0) for item in index.values())
            if total <= MAX_CACHE_BYTES:
                break
        try:
            (_cache_root(conn_key) / f"{path_key}.json").unlink(missing_ok=True)
        except OSError:
            pass
        index.pop(path_key, None)


def read_cache(conn_key: str, remote_path: str, *, now: float | None = None) -> dict | None:
    """读取单层目录缓存。

    返回 ``{path, entries, fetched_at, expires_at, content_hash, entry_count, fresh}``；
    无缓存返回 None。``fresh = expires_at > now``。
    """
    now = now if now is not None else time.time()
    file_path = _entry_file(conn_key, remote_path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    entries = _entries_from_cache(payload)
    fetched_at = float(payload.get("fetched_at") or 0)
    expires_at = float(payload.get("expires_at") or 0)
    if fetched_at <= 0 or expires_at <= 0:
        return None
    with _cache_lock:
        index = _read_index(conn_key)
        path_key = _path_key(remote_path)
        meta = index.get(path_key)
        if meta is not None:
            meta["last_accessed_at"] = now
            _write_index(conn_key, index)
    return {
        "path": str(payload.get("path") or normalize_remote_path(remote_path)),
        "entries": entries,
        "fetched_at": fetched_at,
        "expires_at": expires_at,
        "content_hash": str(payload.get("content_hash") or ""),
        "entry_count": int(payload.get("entry_count") or len(entries)),
        "fresh": expires_at > now,
    }


def write_cache(
    conn_key: str,
    remote_path: str,
    entries: list[dict],
    ttl_minutes: int,
    *,
    now: float | None = None,
) -> dict:
    """写入单层目录缓存（白名单字段），并更新 LRU 索引与容量淘汰。"""
    now = now if now is not None else time.time()
    ttl_seconds = max(1, int(ttl_minutes or 1440)) * 60
    normalized = normalize_remote_path(remote_path)
    safe_entries = [
        {key: entry.get(key) for key in _ENTRY_FIELDS}
        for entry in entries
        if isinstance(entry, dict)
    ]
    payload = {
        "path": normalized,
        "entries": safe_entries,
        "fetched_at": now,
        "expires_at": now + ttl_seconds,
        "content_hash": content_hash(safe_entries),
        "entry_count": len(safe_entries),
    }
    path_key = _path_key(normalized)
    root = _cache_root(conn_key)
    root.mkdir(parents=True, exist_ok=True)
    file_path = root / f"{path_key}.json"
    with DATA_WRITE_LOCK:
        write_json_atomic(file_path, payload)
    with _cache_lock:
        index = _read_index(conn_key)
        index[path_key] = {
            "path": normalized,
            "fetched_at": now,
            "last_accessed_at": now,
            "size_bytes": file_path.stat().st_size if file_path.exists() else 0,
        }
        _evict_locked(conn_key, index)
        _write_index(conn_key, index)
    return payload


def clear_cache_for_connection(conn_key: str) -> None:
    """清空某个连接的浏览缓存（不影响已导入媒体库快照）。"""
    root = _cache_root(conn_key)
    with DATA_WRITE_LOCK:
        for path in list(root.glob("*.json")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
