"""library_rebuild durable handler：从 confirmed revision 重建媒体库索引。

- 读取全部 confirmed/executed revision → 按 work 聚合 → upsert media_libraries；
- scrape 结果写入 scrape_bindings / scrape_failures（SQLite 单写，不再依赖 JSON ScrapeMap）；
- 一个 revision 一个 work（单元语义），library_id 稳定 = work_id 或 revision 派生值。
"""

from __future__ import annotations

import uuid

from app.db.database import get_connection
from app.import_plan import revision_store
from app.jobs.registry import register


def record_scrape_outcome(
    *,
    revision_id: str,
    work_id: str,
    work_title: str = "",
    source: str = "",
    provider_id: str = "",
    tmdb_id: int | None = None,
    tmdb_type: str = "",
    bangumi_id: int | None = None,
    succeeded: bool,
    error: str = "",
) -> None:
    """刮削结果落 SQLite：成功 upsert scrape_bindings，失败写 scrape_failures。"""
    conn = get_connection()
    timestamp = revision_store.now_iso()
    if succeeded:
        conn.execute(
            """
            INSERT INTO scrape_bindings (
                binding_id, revision_id, work_id, source, provider_id,
                tmdb_id, tmdb_type, bangumi_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?)
            ON CONFLICT (binding_id) DO UPDATE SET
                status = 'success', tmdb_id = excluded.tmdb_id,
                tmdb_type = excluded.tmdb_type, updated_at = excluded.updated_at
            """,
            (
                uuid.uuid4().hex, revision_id, work_id, source, provider_id,
                tmdb_id, tmdb_type, bangumi_id, timestamp, timestamp,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO scrape_failures (
                binding_id, error, stage, retryable, timestamp
            ) VALUES ('', ?, 'auto_scrape', 1, ?)
            """,
            (error[:2000], timestamp),
        )
    conn.commit()


def _upsert_library(revision: dict, items: list[dict], timestamp: str) -> str:
    """按 work 聚合 revision items，写/更新 media_libraries 行。"""
    conn = get_connection()
    work_ids = [item.get("work_id") or "" for item in items if item.get("work_id")]
    work_titles = [item.get("work_title") or "" for item in items if item.get("work_title")]
    library_id = work_ids[0] if work_ids else f"rev-{revision['revision_id']}"
    name = work_titles[0] if work_titles else (library_id if work_ids else revision["revision_id"])
    conn.execute(
        """
        INSERT INTO media_libraries (
            library_id, name, root_id, remote_locator, import_family, import_scope,
            current_revision_id, lifecycle_status, created_at, updated_at
        ) VALUES (?, ?, ?, '', 'anime', '', ?, 'draft', ?, ?)
        ON CONFLICT (library_id) DO UPDATE SET
            name = excluded.name,
            current_revision_id = excluded.current_revision_id,
            updated_at = excluded.updated_at
        """,
        (
            library_id, name, str(revision.get("unit_id") or ""),
            revision["revision_id"], timestamp, timestamp,
        ),
    )
    return library_id


def handle_library_rebuild(payload: dict, progress_callback=None, should_cancel=None) -> dict:
    """重建媒体库索引：全部 confirmed/executed revision → 前端可见 JSON LibraryIndex。

    - media_libraries 表（SQLite 事实）；
    - publish_import_plan_to_library 发布到 library_index.json（前端/播放读取路径），
      复用现有镜像发布链路，保证“镜像完成 → 前端可见”闭环。
    """
    from app.library.service import publish_import_plan_to_library

    revisions = revision_store.list_revisions()
    conn = get_connection()
    timestamp = revision_store.now_iso()
    updated = 0
    published = 0
    for revision in revisions:
        if revision.get("status") not in ("confirmed", "executed"):
            continue
        items = conn.execute(
            "SELECT * FROM import_revision_items WHERE revision_id = ?",
            (revision["revision_id"],),
        ).fetchall()
        if not items:
            continue
        _upsert_library(revision, [dict(row) for row in items], timestamp)
        updated += 1
        # 前端可见：复用现有 JSON LibraryIndex 发布链路
        try:
            plan = revision_store.load_plan(revision["revision_id"])
            if plan is not None:
                publish_import_plan_to_library(plan)
                published += 1
        except Exception:
            # 局部发布失败不阻塞整体重建（保留 media_libraries 事实）
            continue
    return {
        "status": "succeeded",
        "libraries_updated": updated,
        "library_index_published": published,
    }


def register_library_handler() -> None:
    register("library_rebuild", handle_library_rebuild)
