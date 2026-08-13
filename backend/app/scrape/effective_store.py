"""Effective Scrape Store：V3（SQLite scrape_bindings）与 legacy（JSON ScrapeMap）分流。

Module 5 规划员拍板：
- plan_id 是 V3 SQLite revision → 读写 ``scrape_bindings``
  （binding_id = scrape_target_id，ON CONFLICT(binding_id) DO UPDATE 稳定幂等，
  完整 ScrapeMapItem 存 metadata_json，跨 revision 同语义 target 复用同一行）；
- 否则（legacy pan115 / baidu / local 旧 plan）→ 继续走 ``scrape_map.json``。
"""

from __future__ import annotations

import json
from dataclasses import asdict

from app.db.database import get_connection
from app.import_plan import revision_store
from app.scrape.models import ScrapeMap, ScrapeMapItem
from app.scrape.store import load_scrape_map, upsert_scrape_map_item


def is_v3_revision(plan_id: str) -> bool:
    """plan_id 是否是 V3 SQLite revision（import_revisions 里真实存在）。"""
    if not plan_id:
        return False
    row = get_connection().execute(
        "SELECT 1 FROM import_revisions WHERE revision_id = ?", (plan_id,)
    ).fetchone()
    return row is not None


def upsert_effective_scrape_map_item(item: ScrapeMapItem) -> None:
    """按 plan 代次分流写入：V3 → SQLite 稳定 binding；legacy → JSON ScrapeMap。"""
    if is_v3_revision(item.import_plan_id):
        _upsert_binding(item)
    else:
        upsert_scrape_map_item(item)


def load_effective_scrape_map(plan_id: str = "") -> ScrapeMap:
    """按 plan 代次分流读取：V3 → SQLite bindings 投影；legacy → JSON ScrapeMap。"""
    if is_v3_revision(plan_id):
        return _scrape_map_from_bindings(plan_id)
    return load_scrape_map()


def _upsert_binding(item: ScrapeMapItem) -> None:
    """V3 稳定 binding 写入：binding_id = scrape_target_id，同 target 幂等更新。

    完整 ScrapeMapItem 序列化进 metadata_json（供 LibraryIndex 投影还原）；
    结构化字段单独落列供诊断/过滤；bangumi_id 不属于 ScrapeMapItem 语义，
    不在刮削写入时触碰（Bangumi 同步另行维护）。
    """
    binding_id = item.scrape_target_id or item.work_id
    if not binding_id:
        raise ValueError("V3 scrape binding 缺少稳定 target id")
    conn = get_connection()
    timestamp = revision_store.now_iso()
    metadata = json.dumps(asdict(item), ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO scrape_bindings (
            binding_id, revision_id, work_id, source, provider_id,
            tmdb_id, tmdb_type, bangumi_id, status,
            nfo_path, poster_path, fanart_path, clearlogo_path,
            metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'success', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (binding_id) DO UPDATE SET
            revision_id = excluded.revision_id,
            work_id = excluded.work_id,
            source = excluded.source,
            provider_id = excluded.provider_id,
            tmdb_id = excluded.tmdb_id,
            tmdb_type = excluded.tmdb_type,
            status = excluded.status,
            nfo_path = excluded.nfo_path,
            poster_path = excluded.poster_path,
            fanart_path = excluded.fanart_path,
            clearlogo_path = excluded.clearlogo_path,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            binding_id, item.import_plan_id, item.work_id, item.source,
            str(getattr(item, "provider_id", "") or ""),
            item.tmdb_id, item.tmdb_type,
            item.nfo_path, item.poster_path, item.fanart_path, item.clearlogo_path,
            metadata, timestamp, timestamp,
        ),
    )
    conn.commit()


def _scrape_map_from_bindings(plan_id: str) -> ScrapeMap:
    """SQLite scrape_bindings → ScrapeMap 兼容投影。

    metadata_json 存在时还原完整 ScrapeMapItem；否则（历史行）用结构化列
    还原最小兼容条目，保证 target_already_scraped 等兼容判断可用。
    """
    rows = get_connection().execute(
        "SELECT * FROM scrape_bindings WHERE revision_id = ?", (plan_id,)
    ).fetchall()
    items: list[ScrapeMapItem] = []
    for row in rows:
        raw = json.loads(row["metadata_json"] or "{}")
        if raw:
            items.append(ScrapeMapItem(**raw))
        else:
            items.append(
                ScrapeMapItem(
                    scrape_target_id=row["binding_id"],
                    work_id=row["work_id"],
                    source=row["source"],
                    import_plan_id=row["revision_id"],
                    tmdb_id=row["tmdb_id"],
                    tmdb_type=row["tmdb_type"] or "",
                    nfo_path=row["nfo_path"] or "",
                    poster_path=row["poster_path"] or "",
                    fanart_path=row["fanart_path"] or "",
                    clearlogo_path=row["clearlogo_path"] or "",
                )
            )
    return ScrapeMap(items=items)
