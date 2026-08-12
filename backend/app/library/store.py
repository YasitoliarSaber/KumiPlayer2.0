# -*- coding: utf-8 -*-
"""LibraryIndex JSON 存储"""

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from app.library.models import LibraryIndex

_INDEX_CACHE: LibraryIndex | None = None
_INDEX_CACHE_MTIME_NS: int | None = None


def _get_cache_dir() -> Path:
    from app.core.paths import get_cache_dir
    return get_cache_dir()


def _get_index_path() -> Path:
    return _get_cache_dir() / "library_index.json"


def get_library_index_signature() -> str:
    """Return a cheap signature for library and visibility-state caches."""
    from app.core.paths import get_data_dir

    paths = [
        _get_index_path(),
        get_data_dir() / "scrape" / "review_queue.json",
        get_data_dir() / "scrape" / "failed_cases.json",
    ]
    parts = [
        f"{os.path.abspath(path)}:{path.stat().st_mtime_ns if path.exists() else 0}"
        for path in paths
    ]
    try:
        from app.tracking.store import list_tracking_bindings

        tracking_revision = max(
            (binding.updated_at for binding in list_tracking_bindings()),
            default="",
        )
    except Exception:
        tracking_revision = ""
    parts.append(tracking_revision)
    return "|".join(parts)


def invalidate_library_index_cache() -> None:
    """Clear the in-process LibraryIndex cache."""
    global _INDEX_CACHE, _INDEX_CACHE_MTIME_NS
    _INDEX_CACHE = None
    _INDEX_CACHE_MTIME_NS = None


def load_library_index() -> Optional[LibraryIndex]:
    """加载 library_index.json"""
    global _INDEX_CACHE, _INDEX_CACHE_MTIME_NS

    path = _get_index_path()
    if not path.exists():
        _INDEX_CACHE = None
        _INDEX_CACHE_MTIME_NS = None
        return None

    mtime_ns = path.stat().st_mtime_ns
    if _INDEX_CACHE is not None and _INDEX_CACHE_MTIME_NS == mtime_ns:
        return _INDEX_CACHE

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        index = _dict_to_library_index(data)
        _INDEX_CACHE = index
        _INDEX_CACHE_MTIME_NS = mtime_ns
        return index
    except (json.JSONDecodeError, KeyError):
        _INDEX_CACHE = None
        _INDEX_CACHE_MTIME_NS = None
        return None


def save_library_index(index: LibraryIndex) -> str:
    """保存 library_index.json"""
    global _INDEX_CACHE, _INDEX_CACHE_MTIME_NS

    from app.library.deleted_works import filter_deleted_works

    filter_deleted_works(index)

    path = _get_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(index), ensure_ascii=False, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    _INDEX_CACHE = index
    _INDEX_CACHE_MTIME_NS = path.stat().st_mtime_ns
    return str(path)


def _dict_to_library_index(data: dict) -> LibraryIndex:
    """从 dict 还原 LibraryIndex"""
    from app.library.models import EpisodeIndex, RelatedWork, SeasonIndex, WorkIndex

    works = []
    for w in data.get("works", []):
        seasons = [SeasonIndex(**s) for s in w.get("seasons", [])]
        episodes = [EpisodeIndex(**e) for e in w.get("episodes", [])]
        related = [RelatedWork(**r) for r in w.get("related_works", [])]
        works.append(WorkIndex(
            work_id=w.get("work_id", ""),
            title=w.get("title", ""),
            original_title=w.get("original_title", ""),
            year=w.get("year"),
            rating=w.get("rating", 0.0),
            plot=w.get("plot", ""),
            genres=w.get("genres", []),
            studios=w.get("studios", []),
            show_type=w.get("show_type", ""),
            media_type=w.get("media_type", ""),
            source=w.get("source", ""),
            sources=w.get("sources", [w.get("source", "")] if w.get("source") else []),
            provider_id=w.get("provider_id", ""),
            ingest_method=w.get("ingest_method", ""),
            source_route_id=w.get("source_route_id", ""),
            import_scope=w.get("import_scope", ""),
            card_type=w.get("card_type", ""),
            poster_path=w.get("poster_path", ""),
            fanart_path=w.get("fanart_path", ""),
            clearlogo_path=w.get("clearlogo_path", ""),
            dir_path=w.get("dir_path", ""),
            source_locations=w.get("source_locations", {}),
            source_episode_counts=w.get("source_episode_counts", {}),
            seasons=seasons,
            episodes=episodes,
            related_works=related,
            cast=w.get("cast", []),
            tags=w.get("tags", []),
            last_played=w.get("last_played"),
            tracking=w.get("tracking"),
            metadata_state=w.get("metadata_state", "ready"),
            certification=w.get("certification", ""),
            certification_country=w.get("certification_country", ""),
            artwork_provenance=w.get("artwork_provenance", {}),
        ))

    return LibraryIndex(
        version=data.get("version", 1),
        works=works,
        source_summary=data.get("source_summary", {}),
        generated_at=data.get("generated_at", ""),
    )
