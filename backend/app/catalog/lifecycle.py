# -*- coding: utf-8 -*-
"""Source Catalog 生命周期服务。

职责（整库删除与来源根重叠解析的统一实现，不再把 SQL 堆进
``app/library/delete.py`` 或 ``app/catalog/store.py``）：

- 来源根影响范围统计（library clear 预览用）；
- 来源根关联持久任务查询（读取 JSON payload 精确匹配，禁止 LIKE 模糊匹配）；
- 来源根事务化删除（整库删除步骤 D）；
- 来源根覆盖关系解析（create / reuse_exact / reuse_ancestor / promote_parent）；
- 子来源根向父来源根归并（promote_parent 的事务化执行）；
- 孤儿批次清理。

安全边界：
- 不删除 ``sources`` 连接配置记录（OpenList 服务器地址、路由、凭据保留）；
- 不触碰任何真实媒体路径，只操作 SQLite 中的 Source Catalog 事实；
- 归并不修改 ``import_revision_items.real_path`` / ``logical_locator``、
  已生成 ``.strm`` 内容、播放记录与收藏状态；
- 所有删除/归并都在单个 SQLite 事务内完成，任何失败整体回滚。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.catalog.models import SourceRootRecord
from app.db.database import get_connection
from app.db.transactions import transaction

#: 归并/删除前不允许存在的活跃任务状态（queued/running 都算）
_ACTIVE_JOB_STATUSES = ("queued", "running")


@dataclass
class CatalogCleanupPreview:
    """整库删除的 Source Catalog 影响范围统计（只读）。"""

    root_ids: list[str] = field(default_factory=list)
    root_count: int = 0
    batch_count: int = 0
    directory_count: int = 0
    node_count: int = 0
    unit_count: int = 0
    revision_count: int = 0
    revision_item_count: int = 0
    library_count: int = 0
    job_count: int = 0
    active_job_count: int = 0


@dataclass
class CatalogCleanupResult:
    """整库删除的 Source Catalog 清理结果。"""

    deleted_root_count: int = 0
    deleted_batch_count: int = 0
    deleted_directory_count: int = 0
    deleted_node_count: int = 0
    deleted_unit_count: int = 0
    deleted_revision_count: int = 0
    deleted_library_count: int = 0
    deleted_job_count: int = 0


@dataclass
class RootResolution:
    """一次导入请求的来源根解析结果。

    ``action`` 只允许：
    - ``create``：全新路径，创建新 SourceRoot；
    - ``reuse_exact``：完全相同路径，复用现有 root；
    - ``reuse_ancestor``：已有父目录覆盖新子目录，复用父 root；
    - ``promote_parent``：新父目录覆盖已有子目录，归并后复用新父 root。
    """

    action: str = "create"
    canonical_root_id: str = ""
    requested_locator: str = ""
    canonical_locator: str = ""
    covered_root_ids: list[str] = field(default_factory=list)


class CatalogCleanupBusyError(RuntimeError):
    """相关持久后台任务正在停止/运行，删除或归并必须中止（API 层转 409）。"""


#: 内部 action → API 对外展示值（方案第五节 JSON 规范）
RESOLUTION_API_LABELS = {
    "create": "created",
    "reuse_exact": "exact_reused",
    "reuse_ancestor": "covered_by_existing_root",
    "promote_parent": "promoted_to_parent",
}


def resolution_api_label(action: str) -> str:
    """内部 action 映射为 API 对外值；未知值原样返回。"""
    return RESOLUTION_API_LABELS.get(action or "", action or "")


# ============================================================
# 来源选择与影响范围统计
# ============================================================

def list_roots_for_library_clear(source: str) -> list[SourceRootRecord]:
    """整库删除时按来源选择 SourceRoot。

    - ``openlist``：仅 ``sources.source_type == 'openlist'`` 的来源根；
    - ``local``：仅本地路径来源根；
    - ``all``：所有由后台 Source Catalog 管理的来源根；
    - ``pan115`` / ``baidu``：目录树 TXT 来源没有 SourceRoot 管理，返回空。
    """
    value = (source or "all").strip().lower()
    conn = get_connection()
    if value in {"", "all"}:
        rows = conn.execute(
            "SELECT * FROM source_roots ORDER BY created_at"
        ).fetchall()
    elif value in {"openlist", "local"}:
        rows = conn.execute(
            """
            SELECT r.* FROM source_roots AS r
            JOIN sources AS s ON s.source_id = r.source_id
            WHERE s.source_type = ?
            ORDER BY r.created_at
            """,
            (value,),
        ).fetchall()
    else:
        # pan115 / baidu 旧 TXT 目录树来源不参与 Source Catalog 生命周期
        return []
    return [SourceRootRecord.from_row(row) for row in rows]


def _related_jobs(
    root_ids: list[str],
    unit_ids: list[str],
    revision_ids: list[str],
) -> list[dict]:
    """按持久 JSON payload 精确匹配关联任务（不用 LIKE 模糊匹配）。

    匹配字段：``root_id`` / ``unit_id`` / ``revision_id``。``resource_key``
    是连接级/资源级键（如 ``scan:conn:{source_id}``），不唯一对应 root，
    不作为删除依据。
    """
    root_set = set(root_ids)
    unit_set = set(unit_ids)
    revision_set = set(revision_ids)
    if not (root_set or unit_set or revision_set):
        return []
    rows = get_connection().execute("SELECT * FROM jobs").fetchall()
    matched: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if (
            str(payload.get("root_id") or "") in root_set
            or str(payload.get("unit_id") or "") in unit_set
            or str(payload.get("revision_id") or "") in revision_set
        ):
            matched.append(dict(row))
    return matched


def _unit_revision_ids(root_ids: list[str]) -> tuple[list[str], list[str]]:
    conn = get_connection()
    if not root_ids:
        return [], []
    marks = ",".join("?" for _ in root_ids)
    unit_rows = conn.execute(
        f"SELECT unit_id FROM media_units WHERE root_id IN ({marks})", root_ids
    ).fetchall()
    unit_ids = [str(row[0]) for row in unit_rows]
    if not unit_ids:
        return [], []
    unit_marks = ",".join("?" for _ in unit_ids)
    revision_rows = conn.execute(
        f"SELECT revision_id FROM import_revisions WHERE unit_id IN ({unit_marks})",
        unit_ids,
    ).fetchall()
    return unit_ids, [str(row[0]) for row in revision_rows]


def preview_catalog_cleanup(source: str) -> CatalogCleanupPreview:
    """统计整库删除会影响的 Source Catalog 数据（只读，不修改任何状态）。"""
    roots = list_roots_for_library_clear(source)
    root_ids = [root.root_id for root in roots]
    preview = CatalogCleanupPreview(
        root_ids=root_ids,
        root_count=len(roots),
    )
    if not root_ids:
        return preview
    conn = get_connection()
    marks = ",".join("?" for _ in root_ids)
    preview.batch_count = int(conn.execute(
        f"""
        SELECT COUNT(*) FROM import_batches
        WHERE batch_id IN (
            SELECT DISTINCT batch_id FROM import_batch_roots WHERE root_id IN ({marks})
        )
        """,
        root_ids,
    ).fetchone()[0])
    preview.directory_count = int(conn.execute(
        f"SELECT COUNT(*) FROM source_directories WHERE root_id IN ({marks})", root_ids
    ).fetchone()[0])
    preview.node_count = int(conn.execute(
        f"SELECT COUNT(*) FROM source_nodes WHERE root_id IN ({marks})", root_ids
    ).fetchone()[0])
    preview.unit_count = int(conn.execute(
        f"SELECT COUNT(*) FROM media_units WHERE root_id IN ({marks})", root_ids
    ).fetchone()[0])
    unit_ids, revision_ids = _unit_revision_ids(root_ids)
    if unit_ids:
        preview.revision_count = int(conn.execute(
            f"SELECT COUNT(*) FROM import_revisions WHERE unit_id IN ({','.join('?' for _ in unit_ids)})",
            unit_ids,
        ).fetchone()[0])
    if revision_ids:
        preview.revision_item_count = int(conn.execute(
            f"SELECT COUNT(*) FROM import_revision_items WHERE revision_id IN ({','.join('?' for _ in revision_ids)})",
            revision_ids,
        ).fetchone()[0])
    preview.library_count = int(conn.execute(
        f"SELECT COUNT(*) FROM media_libraries WHERE root_id IN ({marks})", root_ids
    ).fetchone()[0])
    jobs = _related_jobs(root_ids, unit_ids, revision_ids)
    preview.job_count = len(jobs)
    preview.active_job_count = sum(
        1 for job in jobs if job["status"] in _ACTIVE_JOB_STATUSES
    )
    return preview


def prepare_catalog_cleanup(source: str) -> dict[str, Any]:
    """整库删除步骤 B：处理关联持久任务，返回任务门控结果。

    - ``queued``：直接取消（终态 cancelled）；
    - ``running``：设置协作式取消（``cancel_requested=1``），并返回其 job_id，
      调用方必须中止本次删除（409），不能与可能写库的任务竞争。
    """
    roots = list_roots_for_library_clear(source)
    root_ids = [root.root_id for root in roots]
    unit_ids, revision_ids = _unit_revision_ids(root_ids)
    jobs = _related_jobs(root_ids, unit_ids, revision_ids)
    cancelled = 0
    running_ids: list[str] = []
    from app.jobs import store as job_store

    for job in jobs:
        job_id = str(job["job_id"])
        if job["status"] == "queued":
            if job_store.cancel_job(job_id):
                cancelled += 1
        elif job["status"] == "running":
            # 协作式取消：请求停止，但本次删除必须中止等待其真正停止
            job_store.cancel_job(job_id)
            running_ids.append(job_id)
    return {
        "cancelled_job_count": cancelled,
        "running_job_ids": running_ids,
    }


# ============================================================
# 事务化删除（整库删除步骤 D）
# ============================================================

def delete_catalog_for_clear(source: str) -> CatalogCleanupResult:
    """在单个 SQLite 事务内清理整库删除范围内的全部 Source Catalog 事实。

    调用方必须先通过 :func:`prepare_catalog_cleanup` 门控（无 running 任务）
    并完成镜像文件删除；任何 SQL 失败整体回滚。

    删除顺序按依赖排列；``sources`` 连接配置记录永不删除。
    """
    from app.catalog.store import now_iso

    roots = list_roots_for_library_clear(source)
    if not roots:
        return CatalogCleanupResult()
    root_ids = [root.root_id for root in roots]
    conn = get_connection()
    unit_ids, revision_ids = _unit_revision_ids(root_ids)
    binding_ids: list[str] = []
    if revision_ids:
        rev_marks = ",".join("?" for _ in revision_ids)
        binding_rows = conn.execute(
            f"SELECT binding_id FROM scrape_bindings WHERE revision_id IN ({rev_marks})",
            revision_ids,
        ).fetchall()
        binding_ids = [str(row[0]) for row in binding_rows]
    jobs = _related_jobs(root_ids, unit_ids, revision_ids)
    job_ids = [str(job["job_id"]) for job in jobs]
    # 扫描暂存按 run→root 归属精确清理：单来源删除也会清掉该 root 的暂存，
    # 不再依赖“覆盖全部 root 才清空”的特判，避免孤儿 stage 记录。

    def _marks(items: list[str]) -> str:
        return ",".join("?" for _ in items)

    # 删除前计数（用于提交后计算实际删除量）
    def _count(sql: str, params: list[str]) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    batch_ids = [
        str(row[0]) for row in conn.execute(
            f"""
            SELECT DISTINCT batch_id FROM import_batch_roots WHERE root_id IN ({_marks(root_ids)})
            """,
            root_ids,
        ).fetchall()
    ]
    before = {
        "directories": _count(
            f"SELECT COUNT(*) FROM source_directories WHERE root_id IN ({_marks(root_ids)})",
            root_ids,
        ),
        "nodes": _count(
            f"SELECT COUNT(*) FROM source_nodes WHERE root_id IN ({_marks(root_ids)})",
            root_ids,
        ),
        "libraries": _count(
            f"SELECT COUNT(*) FROM media_libraries WHERE root_id IN ({_marks(root_ids)})",
            root_ids,
        ),
        "batches": _count(
            f"SELECT COUNT(*) FROM import_batches WHERE batch_id IN ({_marks(batch_ids)})",
            batch_ids,
        ) if batch_ids else 0,
    }

    with transaction(conn) as tx:
        # 0. 事务内复查持久任务（BEGIN IMMEDIATE，关闭 prepare 与正式删除之间的
        #    竞态窗口）：running → 抛忙异常（整体回滚 → API 转 409）；queued →
        #    在事务内直接取消（终态 cancelled），绝不让可能写库的任务与删除竞争。
        recheck = _related_jobs(root_ids, unit_ids, revision_ids)
        recheck_by_status: dict[str, list[str]] = {}
        for job in recheck:
            recheck_by_status.setdefault(job["status"], []).append(str(job["job_id"]))
        running_ids = recheck_by_status.get("running", [])
        queued_ids = recheck_by_status.get("queued", [])
        if running_ids:
            raise CatalogCleanupBusyError(
                "相关后台任务正在停止，请稍后再次确认删除"
            )
        if queued_ids:
            tx.execute(
                f"""
                UPDATE jobs SET status = 'cancelled', version = version + 1, updated_at = ?
                WHERE job_id IN ({_marks(queued_ids)})
                """,
                [now_iso(), *queued_ids],
            )
            job_ids = [job_id for job_id in job_ids if job_id not in queued_ids]
        # 1. 刮削关联（binding 维度）
        if binding_ids:
            tx.execute(
                f"DELETE FROM scrape_reviews WHERE binding_id IN ({_marks(binding_ids)})",
                binding_ids,
            )
            tx.execute(
                f"DELETE FROM scrape_failures WHERE binding_id IN ({_marks(binding_ids)})",
                binding_ids,
            )
            tx.execute(
                f"DELETE FROM scrape_bindings WHERE revision_id IN ({_marks(revision_ids)})",
                revision_ids,
            )
        # 2. 产物记录（revision 维度）
        if revision_ids:
            tx.execute(
                f"DELETE FROM artifact_records WHERE revision_id IN ({_marks(revision_ids)})",
                revision_ids,
            )
        # 3. 媒体库投影（root 维度）
        tx.execute(
            f"DELETE FROM media_libraries WHERE root_id IN ({_marks(root_ids)})", root_ids
        )
        # 4. 识别修订与条目
        if revision_ids:
            tx.execute(
                f"DELETE FROM import_revision_items WHERE revision_id IN ({_marks(revision_ids)})",
                revision_ids,
            )
        if unit_ids:
            tx.execute(
                f"DELETE FROM import_revisions WHERE unit_id IN ({_marks(unit_ids)})", unit_ids
            )
        tx.execute(
            f"DELETE FROM media_units WHERE root_id IN ({_marks(root_ids)})", root_ids
        )
        # 5. 扫描暂存（按 run→root 归属精确清理）与扫描运行
        stage_run_ids = [
            str(row[0]) for row in tx.execute(
                f"SELECT run_id FROM source_stage_runs WHERE root_id IN ({_marks(root_ids)})",
                root_ids,
            ).fetchall()
        ]
        if stage_run_ids:
            tx.execute(
                f"DELETE FROM source_stage_entries WHERE run_id IN ({_marks(stage_run_ids)})",
                stage_run_ids,
            )
        tx.execute(
            f"DELETE FROM source_stage_runs WHERE root_id IN ({_marks(root_ids)})", root_ids
        )
        tx.execute(
            f"DELETE FROM scan_runs WHERE root_id IN ({_marks(root_ids)})", root_ids
        )
        # 6. 目录与节点事实
        tx.execute(
            f"DELETE FROM source_directories WHERE root_id IN ({_marks(root_ids)})", root_ids
        )
        tx.execute(
            f"DELETE FROM source_nodes WHERE root_id IN ({_marks(root_ids)})", root_ids
        )
        # 7. 持久任务（含 attempt 记录）
        if job_ids:
            tx.execute(
                f"DELETE FROM job_attempts WHERE job_id IN ({_marks(job_ids)})", job_ids
            )
            tx.execute(
                f"DELETE FROM jobs WHERE job_id IN ({_marks(job_ids)})", job_ids
            )
        # 8. 批次关系与孤儿批次
        tx.execute(
            f"DELETE FROM import_batch_roots WHERE root_id IN ({_marks(root_ids)})", root_ids
        )
        tx.execute(
            """
            DELETE FROM import_batches
            WHERE NOT EXISTS (
                SELECT 1 FROM import_batch_roots AS r WHERE r.batch_id = import_batches.batch_id
            )
            """
        )
        # 9. 来源根
        tx.execute(
            f"DELETE FROM source_roots WHERE root_id IN ({_marks(root_ids)})", root_ids
        )

        # 提交前残留检查：目标 root/unit/revision 必须全部清除
        for table, column, ids in (
            ("source_nodes", "root_id", root_ids),
            ("source_directories", "root_id", root_ids),
            ("media_units", "root_id", root_ids),
            ("media_libraries", "root_id", root_ids),
        ):
            if not ids:
                continue
            remaining = int(tx.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({_marks(ids)})", ids
            ).fetchone()[0])
            if remaining:
                raise RuntimeError(
                    f"Source Catalog 清理残留检查失败：{table} 仍有 {remaining} 条关联数据"
                )
        if unit_ids:
            remaining = int(tx.execute(
                f"SELECT COUNT(*) FROM import_revisions WHERE unit_id IN ({_marks(unit_ids)})",
                unit_ids,
            ).fetchone()[0])
            if remaining:
                raise RuntimeError(
                    f"Source Catalog 清理残留检查失败：import_revisions 仍有 {remaining} 条关联数据"
                )
        if revision_ids:
            remaining = int(tx.execute(
                f"SELECT COUNT(*) FROM import_revision_items WHERE revision_id IN ({_marks(revision_ids)})",
                revision_ids,
            ).fetchone()[0])
            if remaining:
                raise RuntimeError(
                    f"Source Catalog 清理残留检查失败：import_revision_items 仍有 {remaining} 条关联数据"
                )
        if job_ids:
            remaining = int(tx.execute(
                f"SELECT COUNT(*) FROM jobs WHERE job_id IN ({_marks(job_ids)})", job_ids
            ).fetchone()[0])
            if remaining:
                raise RuntimeError(
                    f"Source Catalog 清理残留检查失败：jobs 仍有 {remaining} 条关联数据"
                )

    # 提交后按差值统计实际删除量
    after = {
        "directories": _count(
            f"SELECT COUNT(*) FROM source_directories WHERE root_id IN ({_marks(root_ids)})",
            root_ids,
        ),
        "nodes": _count(
            f"SELECT COUNT(*) FROM source_nodes WHERE root_id IN ({_marks(root_ids)})",
            root_ids,
        ),
        "libraries": _count(
            f"SELECT COUNT(*) FROM media_libraries WHERE root_id IN ({_marks(root_ids)})",
            root_ids,
        ),
        "batches": _count(
            f"SELECT COUNT(*) FROM import_batches WHERE batch_id IN ({_marks(batch_ids)})",
            batch_ids,
        ) if batch_ids else 0,
    }
    return CatalogCleanupResult(
        deleted_root_count=len(root_ids),
        deleted_batch_count=before["batches"] - after["batches"],
        deleted_directory_count=before["directories"] - after["directories"],
        deleted_node_count=before["nodes"] - after["nodes"],
        deleted_unit_count=len(unit_ids),
        deleted_revision_count=len(revision_ids),
        deleted_library_count=before["libraries"] - after["libraries"],
        deleted_job_count=len(job_ids),
    )


# ============================================================
# 来源根覆盖解析与归并
# ============================================================

def _normalized(value: str) -> str:
    from app.catalog.store import normalize_locator

    return normalize_locator(value)


def _is_ancestor_or_self(ancestor: str, descendant: str) -> bool:
    """ancestor 是否为 descendant 的祖先（或相等）；路径段边界判断。"""
    left = _normalized(ancestor)
    right = _normalized(descendant)
    if left == right:
        return True
    if left == "/":
        return True
    return right.startswith(left + "/")


def resolve_root_for_import(
    source_id: str,
    requested_locator: str,
    *,
    import_family: str = "anime",
    import_scope: str = "",
    local_locator: str = "",
) -> RootResolution:
    """解析一次导入请求与既有来源根的覆盖关系。

    优先级：exact 复用 > 已有祖先覆盖（reuse_ancestor）>
    新父覆盖已有后代（promote_parent）> 全新路径（create）。
    不创建/不修改任何数据，只返回解析结果。
    """
    from app.catalog import store as catalog_store

    normalized = _normalized(requested_locator)
    existing = catalog_store.list_source_roots(source_id)

    exact = next(
        (root for root in existing if _normalized(root.remote_locator) == normalized),
        None,
    )
    if exact is not None:
        return RootResolution(
            action="reuse_exact",
            canonical_root_id=exact.root_id,
            requested_locator=requested_locator,
            canonical_locator=exact.remote_locator,
        )

    ancestors = [
        root for root in existing
        if root.remote_locator != "/" and _is_ancestor_or_self(root.remote_locator, requested_locator)
    ]
    if ancestors:
        best = max(ancestors, key=lambda root: len(_normalized(root.remote_locator)))
        return RootResolution(
            action="reuse_ancestor",
            canonical_root_id=best.root_id,
            requested_locator=requested_locator,
            canonical_locator=best.remote_locator,
        )

    descendants = [
        root for root in existing
        if _is_ancestor_or_self(requested_locator, root.remote_locator)
        and _normalized(root.remote_locator) != normalized
    ]
    if descendants:
        return RootResolution(
            action="promote_parent",
            canonical_root_id="",
            requested_locator=requested_locator,
            canonical_locator=requested_locator,
            covered_root_ids=[root.root_id for root in descendants],
        )

    return RootResolution(
        action="create",
        canonical_root_id="",
        requested_locator=requested_locator,
        canonical_locator=requested_locator,
    )


def promote_parent_root(
    source_id: str,
    requested_locator: str,
    *,
    local_locator: str,
    import_family: str = "anime",
    import_scope: str = "",
    child_root_ids: list[str],
) -> RootResolution:
    """事务化把多个子来源根归并到新的父来源根（promote_parent）。

    归并步骤（单事务）：
    1. 确认所有涉及的 durable jobs 均不处于 queued/running（事务内复查）；
    2. 创建新的父 SourceRoot；
    3. 只保留并重绑 ``media_units`` / ``media_libraries`` 到父根
       （保留原 unit_id / revision_id，作品与识别历史不失效）；
    4. **不迁移**子根的物理扫描事实（``source_nodes`` / ``source_directories``）：
       旧子根层级数据（parent_path/depth）在新父根下是错的，且迁移后父根
       frontier 没有父目录本身，会导致父目录下其他作品不被发现；
    5. 精确清理子根的扫描暂存与历史扫描任务；
    6. 删除子根的物理扫描事实、批次关系与来源根；
    7. 删除不再有任何 root 的孤儿批次；
    8. 提交。

    归并后由调用方对新父根执行一次 ``full`` 扫描：父根 frontier 从父目录
    本身重新开始（depth=0），新扫描通过相同 boundary 复用旧 unit，从而
    ``/父`` 下的兄弟作品也能被发现。归并本身不修改 ``real_path`` /
    ``logical_locator`` / 已生成镜像与播放状态。
    """
    from app.catalog.store import now_iso

    child_ids = list(dict.fromkeys(child_root_ids))
    if not child_ids:
        raise ValueError("父来源根归并缺少子来源根")
    conn = get_connection()
    normalized_parent = _normalized(requested_locator)
    with transaction(conn) as tx:
        parent_root_id = _promote_in_tx(
            tx,
            source_id,
            requested_locator,
            normalized_parent=normalized_parent,
            local_locator=local_locator,
            import_family=import_family,
            import_scope=import_scope,
            child_root_ids=child_ids,
            now_iso=now_iso,
        )

    return RootResolution(
        action="promote_parent",
        canonical_root_id=parent_root_id,
        requested_locator=requested_locator,
        canonical_locator=requested_locator,
        covered_root_ids=child_ids,
    )


def _promote_in_tx(
    tx,
    source_id: str,
    requested_locator: str,
    *,
    normalized_parent: str,
    local_locator: str,
    import_family: str,
    import_scope: str,
    child_root_ids: list[str],
    now_iso,
) -> str:
    """在已开启事务内执行归并（供 promote_parent_root 与 create_import_batch 共用）。

    - 事务内复查 durable jobs（running/queued 均拒绝归并）；
    - 创建/复用父 root，只重绑 media_units/media_libraries（保留 unit/revision）；
    - 不迁移子根物理扫描事实，精确清理子根暂存/任务后删除子根。
    调用方负责事务提交/回滚；返回父 root_id。
    """
    import uuid

    child_ids = list(dict.fromkeys(child_root_ids))
    marks = ",".join("?" for _ in child_ids)

    child_units = [str(row[0]) for row in tx.execute(
        f"SELECT unit_id FROM media_units WHERE root_id IN ({marks})", child_ids
    ).fetchall()]
    child_revisions: list[str] = []
    if child_units:
        unit_marks = ",".join("?" for _ in child_units)
        child_revisions = [str(row[0]) for row in tx.execute(
            f"SELECT revision_id FROM import_revisions WHERE unit_id IN ({unit_marks})",
            child_units,
        ).fetchall()]
    active = [
        str(row[0]) for row in tx.execute(
            "SELECT job_id FROM jobs WHERE status IN ('queued', 'running')"
        ).fetchall()
    ]
    if active:
        related = _related_jobs(child_ids, child_units, child_revisions)
        if any(str(job["job_id"]) in active for job in related):
            raise ValueError("相关后台任务正在运行，无法归并来源根，请稍后重试")

    existing_parent = tx.execute(
        "SELECT * FROM source_roots WHERE source_id = ? AND normalized_locator = ?",
        (source_id, normalized_parent),
    ).fetchone()
    if existing_parent is not None:
        parent_root_id = str(existing_parent["root_id"])
    else:
        parent_root_id = uuid.uuid4().hex
        timestamp = now_iso()
        tx.execute(
            """
            INSERT INTO source_roots (
                root_id, source_id, remote_locator, normalized_locator, local_locator,
                import_family, import_scope, scan_policy, active_generation,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'standard', 0, ?, ?)
            """,
            (
                parent_root_id, source_id, requested_locator, normalized_parent,
                local_locator or "", import_family or "anime", import_scope or "",
                timestamp, timestamp,
            ),
        )

    # 单元与媒体库投影归属父根（保留 unit_id / revision_id）
    tx.execute(
        f"UPDATE media_units SET root_id = ? WHERE root_id IN ({marks})",
        [parent_root_id, *child_ids],
    )
    tx.execute(
        f"UPDATE media_libraries SET root_id = ? WHERE root_id IN ({marks})",
        [parent_root_id, *child_ids],
    )
    # 精确清理子根扫描暂存与历史扫描任务
    run_ids = [str(row[0]) for row in tx.execute(
        f"SELECT run_id FROM source_stage_runs WHERE root_id IN ({marks})", child_ids
    ).fetchall()]
    if run_ids:
        run_marks = ",".join("?" for _ in run_ids)
        tx.execute(
            f"DELETE FROM source_stage_entries WHERE run_id IN ({run_marks})", run_ids
        )
    tx.execute(
        f"DELETE FROM source_stage_runs WHERE root_id IN ({marks})", child_ids
    )
    tx.execute(
        f"DELETE FROM scan_runs WHERE root_id IN ({marks})", child_ids
    )
    # 删除子根物理扫描事实（不迁移，由父根全扫后经相同 boundary 复用 unit）
    tx.execute(
        f"DELETE FROM source_directories WHERE root_id IN ({marks})", child_ids
    )
    tx.execute(
        f"DELETE FROM source_nodes WHERE root_id IN ({marks})", child_ids
    )
    # 清除子根批次关系与来源根
    tx.execute(
        f"DELETE FROM import_batch_roots WHERE root_id IN ({marks})", child_ids
    )
    tx.execute(
        f"DELETE FROM source_roots WHERE root_id IN ({marks})", child_ids
    )
    # 孤儿批次：不再关联任何 root 的批次
    tx.execute(
        """
        DELETE FROM import_batches
        WHERE NOT EXISTS (
            SELECT 1 FROM import_batch_roots AS r WHERE r.batch_id = import_batches.batch_id
        )
        """
    )
    return parent_root_id
