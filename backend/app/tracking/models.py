from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


TRACKING_STATES = {"tracking", "paused", "completed", "archived"}
ATTENTION_STATES = {"ready", "waiting_metadata", "waiting_review", "source_unavailable"}
LOGICAL_SOURCES = {"local", "pan115", "baidu"}


def tracking_attention_from_scrape_result(result: dict) -> str:
    """把刮削批次结果转换为追更关注状态。"""
    review_queued = int(result.get("review_queued") or 0)
    failed = int(result.get("failed") or 0)
    completed = int(result.get("auto_scraped") or 0) + int(result.get("skipped_existing") or 0)
    total = int(result.get("total_targets") or 0)
    if review_queued:
        return "waiting_review"
    if failed:
        return "waiting_metadata"
    if completed > 0 and completed >= total:
        return "ready"
    return "waiting_metadata"


@dataclass
class TrackingBinding:
    binding_id: str = ""
    work_id: str = ""
    display_title: str = ""
    logical_source: str = "local"
    root_path: str = ""
    import_family: str = "anime"
    season_number: Optional[int] = None
    series_group: str = ""
    tracking_state: str = "tracking"
    attention_state: str = "ready"
    last_snapshot_id: str = ""
    baseline_plan_id: str = ""
    last_scan_at: str = ""
    last_successful_scan_at: str = ""
    last_result: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
