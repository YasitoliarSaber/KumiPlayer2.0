"""library_rebuild durable handler：从 current revision 重建媒体库索引投影。

Module 5 收口后的唯一语义输入：
- ``media_units.current_revision_id`` → current revision（SQLite 事实）；
- scrape_bindings（SQLite）→ ScrapeMap 兼容投影；
- artifact_records（SQLite）→ MirrorScanResult 兼容投影；
- library_index.json 只是可重建 Projection，一次 rebuild 内一次性发布最终 Index。
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
    """刮削失败记录走 scrape_failures（成功事实只来自真实 target 的 effective upsert）。"""
    conn = get_connection()
    timestamp = revision_store.now_iso()
    if succeeded:  # pragma: no cover - Module 5 后成功路径不再调用（保留兼容）
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


def _upsert_library(revision: dict, items: list[dict], timestamp: str) -> list[str]:
    """按 effective canonical identity 分组 revision items，每个 canonical work
    写/更新一条 ``media_libraries`` 行（不再只取第一条 work_id）。

    - ``canonical_work_id`` 非空 → ``library_id = canonical_work_id``（V3 事实）；
    - 仅 legacy 数据缺 canonical 时允许 ``work_id`` fallback（保持旧库可重建）；
    - 同 revision 多个 standalone canonical → 每个 canonical 一行；
    - 全部无身份时保留 revision 级兜底行（旧行为，避免遗留库丢失投影）。

    root 事实正确化（Module 5 十八.2）：``root_id`` 必须是真实 SourceRoot.root_id
    （revision.unit_id → media_units.root_id → source_roots），不再把 unit_id 当
    root_id、不再 hard-code import_family/remote_locator。
    """
    conn = get_connection()
    unit = conn.execute(
        "SELECT root_id FROM media_units WHERE unit_id = ?",
        (str(revision.get("unit_id") or ""),),
    ).fetchone()
    root_id = str(unit["root_id"] or "") if unit else ""
    remote_locator = ""
    import_family = "anime"
    import_scope = ""
    if root_id:
        root = conn.execute(
            """
            SELECT remote_locator, import_family, import_scope
            FROM source_roots WHERE root_id = ?
            """,
            (root_id,),
        ).fetchone()
        if root is not None:
            remote_locator = str(root["remote_locator"] or "")
            import_family = str(root["import_family"] or "anime")
            import_scope = str(root["import_scope"] or "")

    # effective canonical identity 分组：canonical 优先，legacy 缺 canonical 才用 work_id
    grouped: dict[str, str] = {}
    for item in items:
        identity = str(item.get("canonical_work_id") or "") or str(item.get("work_id") or "")
        if not identity:
            continue
        title = str(item.get("work_title") or "")
        if identity not in grouped:
            grouped[identity] = title
        elif not grouped[identity] and title:
            grouped[identity] = title

    if not grouped:
        # 全无身份：保留 revision 级兜底行（旧行为）
        grouped[f"rev-{revision['revision_id']}"] = revision["revision_id"]

    for library_id, name in grouped.items():
        conn.execute(
            """
            INSERT INTO media_libraries (
                library_id, name, root_id, remote_locator, import_family, import_scope,
                current_revision_id, lifecycle_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            ON CONFLICT (library_id) DO UPDATE SET
                name = excluded.name,
                root_id = excluded.root_id,
                remote_locator = excluded.remote_locator,
                import_family = excluded.import_family,
                import_scope = excluded.import_scope,
                current_revision_id = excluded.current_revision_id,
                updated_at = excluded.updated_at
            """,
            (
                library_id, name, root_id, remote_locator, import_family, import_scope,
                revision["revision_id"], timestamp, timestamp,
            ),
        )
    return list(grouped.keys())

def handle_library_rebuild(payload: dict, progress_callback=None, should_cancel=None) -> dict:
    """从 SQLite current state 一次性重建 LibraryIndex 投影。

    - 语义输入唯一：``media_units.current_revision_id``（superseded/draft 不投影）；
    - 逐 current plan 构建 fragment → merge → 一次 ``save_library_index()``
      （不再逐 plan 多次 read-modify-write legacy publishing）；
    - 清理 stale ``media_libraries`` 行（projection registry，不是语义历史）：
      1. current 指针已失效的行删除；
      2. current revision 下**不再属于 desired canonical identity** 的旧行删除
         （自愈旧版本按 raw work_id 写入的错误 projection，如 ``w → rev-A``
         在新 canonical ``unit:A:main → rev-A`` 发布后收敛）。
    """
    from app.library.index import build_library_index, rebuild_related_works_for_plan
    from app.library.models import LibraryIndex
    from app.library.projection import build_scan_result_projection, load_scrape_map_projection
    from app.library.service import _deduplicate_library_works, _refresh_source_summary_from_works
    from app.library.store import save_library_index

    revisions = revision_store.list_current_revisions()
    conn = get_connection()
    timestamp = revision_store.now_iso()
    updated = 0
    fragments: list = []
    current_revision_ids: set[str] = set()
    # desired identity：current revision → 本轮 upsert 的全部 library_id
    # （canonical_work_id 优先；legacy 缺 canonical 时为 work_id/rev 兜底）
    desired_by_revision: dict[str, set[str]] = {}
    # 同一 source 的全部 current plans 都参与关系重建（不能只保留第一个 plan，
    # 否则后续 unit 的 related_works 会被第一个 plan 覆盖重写为空）
    source_plans: dict[str, list] = {}

    for revision in revisions:
        items = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM import_revision_items WHERE revision_id = ?",
                (revision["revision_id"],),
            ).fetchall()
        ]
        if not items:
            continue
        current_revision_ids.add(revision["revision_id"])
        desired_by_revision[revision["revision_id"]] = set(
            _upsert_library(revision, items, timestamp)
        )
        updated += 1
        plan = revision_store.load_plan(revision["revision_id"])
        if plan is None:
            continue
        # 投影坏数据不 fallback 旧 revision：跳过该 fragment 并保留 media_libraries 事实
        try:
            scrape_map = load_scrape_map_projection(revision["revision_id"])
            scan_result = build_scan_result_projection(plan, revision["revision_id"])
            fragment = build_library_index(plan, scrape_map, scan_result)
        except Exception:
            continue
        if fragment.works:
            fragments.append((plan, fragment))
            source_plans.setdefault(plan.source, []).append(plan)

    # 合并 → 一次发布最终 Index
    merged = LibraryIndex()
    for _plan, fragment in fragments:
        merged.works.extend(fragment.works)
    merged.works = _deduplicate_library_works(merged.works)
    for source, plans in source_plans.items():
        source_works = [work for work in merged.works if work.source == source]
        if len(plans) == 1:
            relation_plan = plans[0]
        else:
            # 关系投影专用聚合视图：同 source 全部 current plans 的 items 并集，
            # 保证每个 current unit 的系列/剧场版关系都被保留
            from app.import_plan.models import ImportPlan

            relation_plan = ImportPlan(
                plan_id=f"aggregate:{source}",
                source=source,
                status="confirmed",
                items=[item for plan in plans for item in plan.items],
            )
        rebuild_related_works_for_plan(source_works, relation_plan)
    for source in source_plans:
        merged.source_summary = _refresh_source_summary_from_works(
            merged.source_summary, source, merged.works
        )
    merged.generated_at = timestamp
    save_library_index(merged)

    # stale media_libraries 收敛（projection registry 可从 current facts 重建）：
    # 1. current 指针已不属于任何当前 unit 的行删除；
    # 2. current revision 下不再属于 desired identity 的旧行删除（自愈旧版本
    #    按 raw work_id 写入的错误 projection，如 ``w → rev-A`` 在新 canonical
    #    ``unit:A:main → rev-A`` 发布后收敛，避免新旧行并存）。
    stale_removed = 0
    for row in conn.execute(
        "SELECT library_id, current_revision_id FROM media_libraries"
    ).fetchall():
        revision_id = str(row["current_revision_id"] or "")
        if not revision_id:
            continue
        if revision_id not in current_revision_ids:
            conn.execute(
                "DELETE FROM media_libraries WHERE library_id = ?",
                (row["library_id"],),
            )
            stale_removed += 1
            continue
        desired = desired_by_revision.get(revision_id)
        if desired is not None and row["library_id"] not in desired:
            conn.execute(
                "DELETE FROM media_libraries WHERE library_id = ?",
                (row["library_id"],),
            )
            stale_removed += 1
    conn.commit()

    return {
        "status": "succeeded",
        "libraries_updated": updated,
        "library_index_works": len(merged.works),
        "stale_libraries_removed": stale_removed,
    }


def register_library_handler() -> None:
    register("library_rebuild", handle_library_rebuild)
