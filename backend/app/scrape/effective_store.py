"""Effective Scrape Store：V3（SQLite scrape_bindings）与 legacy（JSON ScrapeMap）分流。

Module 5 规划员拍板：
- plan_id 是 V3 SQLite revision → 读写 ``scrape_bindings``
  （binding_id = scrape_target_id，ON CONFLICT(binding_id) DO UPDATE 稳定幂等，
  完整 ScrapeMapItem 存 metadata_json，跨 revision 同语义 target 复用同一行）；
- 否则（legacy pan115 / baidu / local 旧 plan）→ 继续走 ``scrape_map.json``。
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace

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


def register_scrape_artifacts(item: ScrapeMapItem, episode_nfo_paths: list[str] | None = None) -> None:
    """V3 刮削成功后登记实际产生的本地产物（nfo/poster/fanart/clearlogo/episode nfo）。

    - 远程 artwork URL（http/https）不是本地 materialized artifact，不登记
      （仍保留在 scrape_bindings 的 path 字段供 LibraryIndex 展示）；
    - 本地文件以 Path.exists() 为"实际物化"证据。
    legacy（非 V3 revision）不进入 artifact_records。
    """
    if not is_v3_revision(item.import_plan_id):
        return
    from pathlib import Path

    from app.pipeline.artifacts import upsert_artifact

    revision_id = item.import_plan_id
    work_id = item.work_id
    for kind, path_value in (
        ("nfo", item.nfo_path),
        ("poster", item.poster_path),
        ("fanart", item.fanart_path),
        ("clearlogo", item.clearlogo_path),
    ):
        value = str(path_value or "").strip()
        if not value or value.startswith(("http://", "https://")):
            continue
        if not Path(value).exists():
            continue
        # Review Fix 2：V3 事务级 current fence
        upsert_artifact(
            kind=kind, path=value, revision_id=revision_id, work_id=work_id,
            require_current=True,
        )
    for path_value in episode_nfo_paths or []:
        value = str(path_value or "").strip()
        if value and Path(value).exists():
            upsert_artifact(
                kind="nfo", path=value, revision_id=revision_id, work_id=work_id,
                require_current=True,
            )


def load_all_bindings_scrape_map() -> ScrapeMap:
    """全部 V3 SQLite bindings 的 ScrapeMap 兼容投影（跨 plan 复用判定用）。"""
    return _scrape_map_from_rows(
        get_connection().execute("SELECT * FROM scrape_bindings ORDER BY updated_at").fetchall()
    )


def load_current_bindings_scrape_map() -> ScrapeMap:
    """只含所属 revision 仍为 current 的 bindings（V3 可执行 target 恢复用）。

    superseded/悬空 revision 的历史 binding 不得恢复成当前可执行 target
    （Review Fix 2 Blocker 1.3）。
    """
    rows = get_connection().execute(
        """
        SELECT b.* FROM scrape_bindings b
        JOIN media_units u ON u.current_revision_id = b.revision_id
        JOIN import_revisions r
          ON r.revision_id = b.revision_id AND r.unit_id = u.unit_id
        WHERE r.status IN ('confirmed', 'executed')
        ORDER BY b.updated_at
        """
    ).fetchall()
    return _scrape_map_from_rows(rows)


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
    """V3 稳定 binding 写入：binding_id 严格等于 scrape_target_id，同 target 幂等更新。

    - scrape_target_id 为空 → ValueError（绝不 fallback work_id，多季 target 可能碰撞）；
    - provider_id 从当前 import_revisions.provider_id 事实填入（ScrapeMapItem
      无该字段，不得默认空）；
    - 完整 ScrapeMapItem 序列化进 metadata_json（供 LibraryIndex 投影还原）；
    - bangumi_id 不属于 ScrapeMapItem 语义，不在刮削写入时触碰。
    """
    binding_id = item.scrape_target_id
    if not binding_id:
        raise ValueError("V3 scrape binding 缺少稳定 scrape_target_id")
    from app.db.transactions import transaction
    from app.pipeline.artifacts import StaleRevisionError

    conn = get_connection()
    timestamp = revision_store.now_iso()
    with transaction(conn) as tx:
        # Review Fix 2：current 检查与 binding upsert 在同一 BEGIN IMMEDIATE
        # 写事务内——执行中途 current 已切换到新 revision 的 stale worker
        # 必须被拒，不能把同一 stable binding 抢回旧 revision。
        if not revision_store.is_current_revision(item.import_plan_id):
            raise StaleRevisionError(
                f"binding 写入被拒：revision {item.import_plan_id} 已不再是 current"
            )
        provider_row = tx.execute(
            "SELECT provider_id FROM import_revisions WHERE revision_id = ?",
            (item.import_plan_id,),
        ).fetchone()
        provider_id = str(provider_row["provider_id"] or "") if provider_row else ""
        metadata = json.dumps(asdict(item), ensure_ascii=False)
        tx.execute(
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
                provider_id,
                item.tmdb_id, item.tmdb_type,
                item.nfo_path, item.poster_path, item.fanart_path, item.clearlogo_path,
                metadata, timestamp, timestamp,
            ),
        )


def adopt_binding_to_revision(item: ScrapeMapItem, new_plan_id: str) -> None:
    """跨 revision 复用：把旧 binding 重新归属到当前 revision（不重刮）。

    同一 scrape_target_id 的绑定行保持唯一，仅 revision_id 与
    metadata_json.import_plan_id 更新为当前 revision。
    """
    adopted = replace(item, import_plan_id=new_plan_id)
    _upsert_binding(adopted)


def _scrape_map_from_bindings(plan_id: str) -> ScrapeMap:
    """SQLite scrape_bindings → ScrapeMap 兼容投影。"""
    rows = get_connection().execute(
        "SELECT * FROM scrape_bindings WHERE revision_id = ?", (plan_id,)
    ).fetchall()
    return _scrape_map_from_rows(rows)


def _scrape_map_from_rows(rows) -> ScrapeMap:
    """binding 行 → ScrapeMap 兼容投影。

    metadata_json 存在时还原完整 ScrapeMapItem；否则（历史行）用结构化列
    还原最小兼容条目，保证 target_already_scraped 等兼容判断可用。
    """
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
