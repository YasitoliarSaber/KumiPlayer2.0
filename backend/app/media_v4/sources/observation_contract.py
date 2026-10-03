"""文本结束不代表来源完整；只在同一来源内继承已确认事实。"""

from __future__ import annotations

import json

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.sources.incremental import _clone_for_scan


def merge_text_snapshot(
    database: V4Database, root_id: str, scan_id: str, current: list[SourceEvidence],
) -> list[SourceEvidence]:
    """未知导出范围只能增加/更新观察，不能证明未列出的旧条目已消失。"""
    if any(item.root_id != root_id or item.scan_id != scan_id for item in current):
        raise ValueError("文本证据与所选来源或扫描不一致")
    paths = {item.relative_path for item in current}
    inherited = [
        _clone_for_scan(item, scan_id)
        for item in V4Repository(database).list_confirmed_source_evidence(root_id)
        if item.relative_path not in paths
    ]
    coverage = json.dumps({
        "kind": "snapshot_unknown", "can_mark_missing": False,
        "observed_count": len(current), "inherited_count": len(inherited),
    }, ensure_ascii=False, sort_keys=True)
    with database.connect() as conn:
        conn.execute("UPDATE source_scans SET coverage_json=? WHERE scan_id=? AND root_id=?",
                     (coverage, scan_id, root_id))
    return [*current, *inherited]
