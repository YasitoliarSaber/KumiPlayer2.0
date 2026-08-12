# -*- coding: utf-8 -*-
"""刮削 Review Queue 存储

自动刮削低置信度条目进入此队列，等待人工确认。
存储路径：data/scrape/review_queue.json
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from app.scrape.models import ScrapeCandidate, ScrapeTarget
from app.scrape.store import build_failed_case
from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK


@dataclass
class ReviewQueueItem:
    """待人工确认的刮削条目"""

    scrape_target_id: str = ""
    source: str = ""
    import_plan_id: str = ""
    series_group: str = ""
    local_title: str = ""
    scrape_title: str = ""
    scrape_year: Optional[int] = None
    scrape_type: str = ""
    local_season_number: Optional[int] = None
    reason: str = ""
    candidates: List[dict] = field(default_factory=list)
    added_at: str = ""
    status: str = "pending"  # pending / resolved / skipped


@dataclass
class ReviewQueue:
    """Review Queue 文件结构"""

    version: int = 1
    items: List[ReviewQueueItem] = field(default_factory=list)


def _get_queue_path() -> Path:
    from app.core.paths import get_data_dir
    scrape_dir = get_data_dir() / "scrape"
    scrape_dir.mkdir(parents=True, exist_ok=True)
    return scrape_dir / "review_queue.json"


def load_review_queue() -> ReviewQueue:
    """加载 review_queue.json"""
    path = _get_queue_path()
    if not path.exists():
        return ReviewQueue()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = [ReviewQueueItem(**item) for item in data.get("items", [])]
        return ReviewQueue(version=data.get("version", 1), items=items)
    except (json.JSONDecodeError, KeyError):
        return ReviewQueue()


def save_review_queue(queue: ReviewQueue) -> None:
    """保存 review_queue.json"""
    with DATA_WRITE_LOCK:
        write_json_atomic(_get_queue_path(), asdict(queue))


def add_to_review_queue(
    target: ScrapeTarget,
    reason: str,
    candidates: List[ScrapeCandidate],
) -> None:
    with DATA_WRITE_LOCK:
        _add_to_review_queue_unlocked(target, reason, candidates)


def _add_to_review_queue_unlocked(
    target: ScrapeTarget,
    reason: str,
    candidates: List[ScrapeCandidate],
) -> None:
    """添加条目到 review queue

    如果已存在相同 scrape_target_id，更新而非重复添加。
    """
    queue = load_review_queue()
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()

    # 移除已有的同 target 条目
    queue.items = [
        item for item in queue.items
        if item.scrape_target_id != target.scrape_target_id
    ]

    # 候选只保留关键字段
    candidate_summaries = [
        {
            "candidate_id": c.candidate_id,
            "provider": c.provider,
            "tmdb_id": c.tmdb_id,
            "tmdb_type": c.tmdb_type,
            "title": c.title,
            "original_title": c.original_title,
            "year": c.year,
            "poster_path": c.poster_path,
            "score": c.score,
            "reasons": c.reasons,
            "popularity": c.popularity,
            "vote_average": c.vote_average,
            "raw": c.raw,
        }
        for c in candidates[:5]  # 最多保留 5 个候选
    ]

    item = ReviewQueueItem(
        scrape_target_id=target.scrape_target_id,
        source=target.source,
        import_plan_id=target.import_plan_id,
        series_group=target.series_group,
        local_title=target.local_title,
        scrape_title=target.scrape_title,
        scrape_year=target.scrape_year,
        scrape_type=target.scrape_type,
        local_season_number=target.local_season_number,
        reason=reason,
        candidates=candidate_summaries,
        added_at=now,
        status="pending",
    )

    queue.items.append(item)
    save_review_queue(queue)
    _save_review_item_to_db(item)
    _save_review_to_error_log(item, target)


def _save_review_to_error_log(item: ReviewQueueItem, target: ScrapeTarget) -> None:
    """将 review queue 条目同步写入统一错误日志"""
    try:
        from app.core.error_log import log_error
        context = build_failed_case(
            target=target,
            candidates=[ScrapeCandidate(**c) for c in item.candidates],
            stage="review_queue",
            extra={
                "reason": item.reason,
                "candidates_count": len(item.candidates),
            },
        )
        context.pop("traceback", None)
        log_error(
            stage="scrape",
            category="review_queue",
            message=f"需要人工确认: {item.local_title or item.scrape_title or target.scrape_target_id} — {item.reason}",
            level="warning",
            source=item.source or target.source,
            context=context,
        )
    except Exception:
        pass


def resolve_review_item(
    scrape_target_id: str,
    status: str = "resolved",
) -> bool:
    with DATA_WRITE_LOCK:
        return _resolve_review_item_unlocked(scrape_target_id, status)


def _resolve_review_item_unlocked(
    scrape_target_id: str,
    status: str = "resolved",
) -> bool:
    """标记 review queue 条目为已处理

    返回 True 表示找到并更新。
    """
    queue = load_review_queue()
    found = False

    for item in queue.items:
        if item.scrape_target_id == scrape_target_id:
            item.status = status
            found = True
            break

    if found:
        save_review_queue(queue)
        _update_review_item_status_in_db(scrape_target_id, status)

    return found


def get_pending_review_items(source: Optional[str] = None) -> List[ReviewQueueItem]:
    """获取待处理的 review 条目，可按来源过滤。"""
    queue = load_review_queue()
    return [
        item for item in queue.items
        if item.status == "pending" and (source is None or item.source == source)
    ]


def prune_pending_review_items(
    valid_target_ids: set[str],
    source: Optional[str] = None,
) -> int:
    with DATA_WRITE_LOCK:
        return _prune_pending_review_items_unlocked(valid_target_ids, source)


def _prune_pending_review_items_unlocked(
    valid_target_ids: set[str],
    source: Optional[str] = None,
) -> int:
    """把不再属于当前 ImportPlan 的 pending 条目标记为 stale。

    Review Queue 是持久状态，用户清空某来源或重新导入后，旧 target_id
    可能仍在 JSON 中。这里保留历史记录但不再显示，避免旧 115/百度条目
    污染当前人工确认列表。
    """
    queue = load_review_queue()
    stale_ids: list[str] = []

    for item in queue.items:
        if item.status != "pending":
            continue
        if source is not None and item.source != source:
            continue
        if item.scrape_target_id in valid_target_ids:
            continue
        item.status = "stale"
        stale_ids.append(item.scrape_target_id)

    if not stale_ids:
        return 0

    save_review_queue(queue)
    _mark_review_items_removed_in_db(stale_ids)
    return len(stale_ids)


def clear_pending_review_items(source: Optional[str] = None) -> int:
    with DATA_WRITE_LOCK:
        return _clear_pending_review_items_unlocked(source)


def _clear_pending_review_items_unlocked(source: Optional[str] = None) -> int:
    """清理旧的 pending review 条目。

    自动刮削重新运行前调用，避免旧策略留下的待确认项一直显示。
    返回清理数量。
    """
    queue = load_review_queue()
    kept = []
    removed_ids = []

    for item in queue.items:
        if item.status == "pending" and (source is None or item.source == source):
            removed_ids.append(item.scrape_target_id)
            continue
        kept.append(item)

    if not removed_ids:
        return 0

    queue.items = kept
    save_review_queue(queue)
    _mark_review_items_removed_in_db(removed_ids)
    return len(removed_ids)


def _save_review_item_to_db(item: ReviewQueueItem) -> None:
    """将 review item 双写到 SQLite；失败不影响 JSON 主流程。"""
    try:
        from app.db.database import close_connection, get_connection, init_db
        init_db()
        conn = get_connection()
        conn.execute("""
            INSERT OR REPLACE INTO scrape_review_queue
            (scrape_target_id, source, series_group, local_title, scrape_title,
             scrape_year, scrape_type, local_season_number, reason, candidates,
             added_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.scrape_target_id, item.source, item.series_group,
            item.local_title, item.scrape_title, item.scrape_year,
            item.scrape_type, item.local_season_number, item.reason,
            json.dumps(item.candidates, ensure_ascii=False),
            item.added_at, item.status,
        ))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            from app.db.database import close_connection
            close_connection()
        except Exception:
            pass


def _update_review_item_status_in_db(scrape_target_id: str, status: str) -> None:
    try:
        from app.db.database import close_connection, get_connection, init_db
        init_db()
        conn = get_connection()
        conn.execute(
            "UPDATE scrape_review_queue SET status = ? WHERE scrape_target_id = ?",
            (status, scrape_target_id),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            from app.db.database import close_connection
            close_connection()
        except Exception:
            pass


def _mark_review_items_removed_in_db(scrape_target_ids: List[str]) -> None:
    try:
        from app.db.database import close_connection, get_connection, init_db
        init_db()
        conn = get_connection()
        conn.executemany(
            "UPDATE scrape_review_queue SET status = ? WHERE scrape_target_id = ?",
            [("stale", target_id) for target_id in scrape_target_ids],
        )
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            from app.db.database import close_connection
            close_connection()
        except Exception:
            pass
