"""持久化用户明确删除的作品卡片，防止可重建索引把它们自动恢复。"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK


def _path() -> Path:
    from app.core.paths import get_data_dir

    return get_data_dir() / "library" / "deleted_works.json"


def load_deleted_work_ids() -> set[str]:
    path = _path()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        str(item.get("work_id") or "")
        for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("work_id")
    }


def mark_work_deleted(work_id: str) -> None:
    if not work_id:
        raise ValueError("work_id 不能为空")
    with DATA_WRITE_LOCK:
        path = _path()
        items = []
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                items = [item for item in payload.get("items", []) if isinstance(item, dict)]
            except (json.JSONDecodeError, OSError):
                items = []
        items = [item for item in items if item.get("work_id") != work_id]
        items.append({
            "work_id": work_id,
            "deleted_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        })
        write_json_atomic(path, {"version": 1, "items": items})


def filter_deleted_works(index) -> None:
    deleted = load_deleted_work_ids()
    if not deleted:
        return
    removed_works = [work for work in index.works if work.work_id in deleted]
    index.works = [work for work in index.works if work.work_id not in deleted]
    for work in index.works:
        work.related_works = [item for item in work.related_works if item.work_id not in deleted]
    refresh_source_summary(index, removed_works=removed_works)


def refresh_source_summary(index, removed_works=None) -> None:
    """墓碑过滤后按当前可见来源贡献重算统计，避免重扫结果继续计入已删卡。"""
    sources = set(index.source_summary)
    for work in index.works:
        sources.update(work.sources or ([work.source] if work.source else []))
        sources.update(episode.source for episode in work.episodes if episode.source)

    updated = dict(index.source_summary)
    for source in sources:
        source_works = [
            work for work in index.works
            if source in set(work.sources or ([work.source] if work.source else []))
            or source in (work.source_locations or {})
            or any((episode.source or work.source) == source for episode in work.episodes)
        ]
        source_episodes = [
            episode
            for work in source_works
            for episode in work.episodes
            if (episode.source or work.source) == source
        ]
        summary = dict(updated.get(source, {}))
        episode_count = sum(
            _source_episode_contribution(work, source)
            for work in source_works
        )
        strm_count = episode_count
        summary.update({
            "work_count": len(source_works),
            "episode_count": episode_count,
            "strm_count": strm_count,
            "missing_strm_count": sum(
                bool(episode.strm_path) and not Path(episode.strm_path).exists()
                for episode in source_episodes
            ),
            "poster_count": sum(bool(work.poster_path) for work in source_works),
            "fanart_count": sum(bool(work.fanart_path) for work in source_works),
            "clearlogo_count": sum(bool(work.clearlogo_path) for work in source_works),
            "scraped_work_count": sum(
                any(season.scraped for season in work.seasons)
                for work in source_works
            ),
        })
        summary.setdefault("warnings", [])
        updated[source] = summary
    index.source_summary = updated


def _source_episode_contribution(work, source: str) -> int:
    persisted = int((work.source_episode_counts or {}).get(source, 0) or 0)
    if persisted:
        return persisted
    visible = sum((episode.source or work.source) == source for episode in work.episodes)
    if visible:
        return visible
    if source in (work.source_locations or {}):
        return sum(bool(episode.strm_path) for episode in work.episodes)
    return 0
