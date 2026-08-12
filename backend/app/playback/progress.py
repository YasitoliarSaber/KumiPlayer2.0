# -*- coding: utf-8 -*-
"""Playback progress store.

This store is intentionally small and JSON-backed.  Playback history answers
"what was opened"; progress answers "how far did the user get".
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK

COMPLETE_THRESHOLD = 0.95


@dataclass
class PlaybackProgressItem:
    work_id: str = ""
    episode_id: str = ""
    position: float = 0
    duration: float = 0
    ratio: float = 0
    completed: bool = False
    updated_at: str = ""
    bangumi_synced: bool = False
    bangumi_error: str = ""
    manually_unwatched: bool = False


def progress_path() -> Path:
    from app.core.paths import get_data_dir

    path = get_data_dir() / "playback" / "progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_progress() -> list[PlaybackProgressItem]:
    path = progress_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [PlaybackProgressItem(**item) for item in data if isinstance(item, dict)]


def list_progress(work_id: Optional[str] = None) -> list[PlaybackProgressItem]:
    items = load_progress()
    if work_id:
        items = [item for item in items if item.work_id == work_id]
    return sorted(items, key=lambda item: item.updated_at, reverse=True)


def get_completed_episode_ids(work_id: str) -> set[str]:
    return {item.episode_id for item in list_progress(work_id) if item.completed}


def save_progress(
    work_id: str,
    episode_id: str,
    position: float,
    duration: float,
    *,
    threshold: float = COMPLETE_THRESHOLD,
    sync_bangumi: bool = True,
) -> PlaybackProgressItem:
    ratio = 0.0
    if duration > 0:
        ratio = max(0.0, min(1.0, float(position) / float(duration)))
    completed = duration > 0 and ratio >= threshold
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()

    with DATA_WRITE_LOCK:
        items = load_progress()
        existing = next((item for item in items if item.work_id == work_id and item.episode_id == episode_id), None)
        if existing is None:
            existing = PlaybackProgressItem(work_id=work_id, episode_id=episode_id)
            items.append(existing)
        was_completed = existing.completed

        existing.position = float(position)
        existing.duration = float(duration)
        existing.ratio = round(ratio, 4)
        existing.completed = bool(existing.completed or completed)
        if completed:
            existing.manually_unwatched = False
        existing.updated_at = now
        if not existing.bangumi_synced:
            existing.bangumi_error = ""
        _write_progress(items)
    if sync_bangumi and existing.completed and not was_completed:
        synced, error = _try_sync_bangumi_episode(work_id, episode_id)
        existing = _store_bangumi_result(work_id, episode_id, synced, error)
    return existing


def mark_episode_completed(
    work_id: str,
    episode_id: str,
    completed: bool,
) -> PlaybackProgressItem:
    """Set the local completed flag from KumiPlayer UI controls."""
    with DATA_WRITE_LOCK:
        items = load_progress()
        existing = next((item for item in items if item.work_id == work_id and item.episode_id == episode_id), None)
        if existing is None:
            existing = PlaybackProgressItem(work_id=work_id, episode_id=episode_id)
            items.append(existing)

        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        if completed:
            existing.position = 1
            existing.duration = 1
            existing.ratio = 1
            existing.completed = True
            existing.manually_unwatched = False
        else:
            existing.position = 0
            existing.duration = 0
            existing.ratio = 0
            existing.completed = False
            existing.bangumi_synced = False
            existing.bangumi_error = ""
            existing.manually_unwatched = True
        existing.updated_at = now
        _write_progress(items)
    if completed and not existing.bangumi_synced:
        synced, error = _try_sync_bangumi_episode(work_id, episode_id)
        existing = _store_bangumi_result(work_id, episode_id, synced, error)
    return existing


def sync_completed_episodes(work_id: str, season_number: Optional[int]) -> dict[str, int]:
    """补同步确认 Bangumi 匹配前已经看完的本地剧集。"""
    items = load_progress()
    synced_count = 0
    failed_count = 0
    for item in items:
        if item.work_id != work_id or not item.completed or item.bangumi_synced:
            continue
        try:
            from app.integrations.bangumi import resolve_episode

            _, episode = resolve_episode(item.episode_id, work_id)
        except (LookupError, ValueError):
            continue
        if episode.season_number != season_number:
            continue
        synced, error = _try_sync_bangumi_episode(work_id, item.episode_id)
        _store_bangumi_result(work_id, item.episode_id, synced, error)
        if synced:
            synced_count += 1
        else:
            failed_count += 1
    return {"synced": synced_count, "failed": failed_count}


def import_remote_progress(
    work_id: str,
    episode_ids: set[str],
    season_number: Optional[int] = None,
) -> int:
    """批量导入远端已看进度到本地。

    要求：
    - 使用现有 DATA_WRITE_LOCK 和原子 JSON 写入。
    - 网站已看剧集应落为 ``completed=True``。
    - 远端已确认的剧集设置 ``bangumi_synced=True``，清空旧错误。
    - 已存在的真实播放位置和时长尽量保留，只更新完成状态。
    - 如果本地没有记录，可以建立 ``position=1、duration=1、ratio=1`` 的完成记录。
    - 不覆盖其他季度、其他作品或无关字段。
    - 网络请求期间不要长时间持有 DATA_WRITE_LOCK。

    返回新标记为已看的剧集数量。
    """
    if not episode_ids:
        return 0

    with DATA_WRITE_LOCK:
        items = load_progress()
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        imported = 0
        for episode_id in sorted(episode_ids):
            existing = next(
                (item for item in items if item.work_id == work_id and item.episode_id == episode_id),
                None,
            )
            if existing is None:
                items.append(PlaybackProgressItem(
                    work_id=work_id,
                    episode_id=episode_id,
                    position=1,
                    duration=1,
                    ratio=1.0,
                    completed=True,
                    bangumi_synced=True,
                    updated_at=now,
                ))
                imported += 1
            elif existing.manually_unwatched:
                # 用户明确取消已看的，暂不重新标记
                continue
            elif not existing.completed:
                existing.position = existing.position or 1
                existing.duration = existing.duration or 1
                existing.ratio = 1.0
                existing.completed = True
                existing.bangumi_synced = True
                existing.bangumi_error = ""
                existing.updated_at = now
                imported += 1
            else:
                # 已经完成但可能同步失败，只更新同步状态
                existing.bangumi_synced = True
                existing.bangumi_error = ""
                existing.updated_at = now

        _write_progress(items)

    return imported


def _write_progress(items: list[PlaybackProgressItem]) -> None:
    write_json_atomic(progress_path(), [asdict(item) for item in items])


def _store_bangumi_result(
    work_id: str,
    episode_id: str,
    synced: bool,
    error: str,
) -> PlaybackProgressItem:
    """只合并同步字段，避免网络等待后覆盖较新的播放进度。"""
    with DATA_WRITE_LOCK:
        items = load_progress()
        item = next(
            current
            for current in items
            if current.work_id == work_id and current.episode_id == episode_id
        )
        item.bangumi_synced = synced
        item.bangumi_error = error
        _write_progress(items)
        return item


def sync_episode_completion(work_id: str, episode_id: str) -> PlaybackProgressItem:
    """同步单集完成状态，并只合并同步结果字段。"""
    synced, error = _try_sync_bangumi_episode(work_id, episode_id)
    return _store_bangumi_result(work_id, episode_id, synced, error)


def _try_sync_bangumi_episode(work_id: str, episode_id: str) -> tuple[bool, str]:
    try:
        from app.integrations.bangumi import (
            BangumiClient,
            BangumiEpisodeSync,
            EPISODE_COLLECTION_DONE,
            get_match,
            record_episode_sync,
            resolve_bangumi_episode_id,
            resolve_episode,
        )

        work, episode = resolve_episode(episode_id, work_id)
        season_number = episode.season_number
        match = get_match(work.work_id, season_number)
        if match is None:
            return False, ""
        client = BangumiClient()
        bangumi_episode_id = resolve_bangumi_episode_id(client, match, episode, None)
        client.set_episode_collection(bangumi_episode_id, EPISODE_COLLECTION_DONE)
        record_episode_sync(BangumiEpisodeSync(
            local_episode_id=episode.episode_id,
            bangumi_episode_id=bangumi_episode_id,
            work_id=work.work_id,
            season_number=season_number,
            subject_id=match.subject_id,
            type=EPISODE_COLLECTION_DONE,
        ))
        return True, ""
    except Exception as exc:
        return False, str(exc)
