"""Source Catalog 存储（SQLite）。

- source_roots 唯一（source_id + normalized_locator）；同来源重叠 root 拒绝；
- 目录分页暂存 → 完整分页后单事务原子提交（node 新增/更新/tombstone +
  directory checkpoint）；未完成分页不得产生 tombstone；
- 所有提交带 generation fence（旧 generation 不得覆盖新 generation）；
- 每批最多 500 条写入，支持取消检查与背压。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.catalog.models import SourceNodeInput, SourceRootRecord
from app.db.database import get_connection
from app.db.transactions import transaction

#: 单事务批量写入上限
BATCH_WRITE_LIMIT = 500
#: 目录深度保护线（防环/异常；不再是 12 层产品限制）
MAX_DIRECTORY_DEPTH = 128
#: 无原生 delta 的来源完成一次目录验证后，24 小时后进入滚动完整校验候选。
VERIFY_INTERVAL = timedelta(hours=24)
#: HYB-5：单轮 rolling verification 预算（命名常量，非官方配额）——
#: snapshot-only 未远端验证的目录每轮只取有限数量入队，避免一轮把
#: TXT bootstrap 留下的全部目录一次性扫完（变相全扫）。后续 HYB-6
#: 可把该值可视化/配置化，这里初版用保守固定预算。
BASELINE_VERIFY_BUDGET = 50
#: HYB-5：滚动验证到期时间抖动窗口（0 ~ 24h），叠加在 VERIFY_INTERVAL 上，
#: 用 stable hash(remote_path) 确定性分散，避免 TXT 导入次日全部目录
#: 同时到期（“滚动验证其实又是一次全扫”）。
ROLLING_JITTER_WINDOW = timedelta(hours=24)


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def normalize_locator(locator: str) -> str:
    """规范化远端/本地定位（小写去尾斜杠；Windows 盘符保留大小写不敏感比较）。"""
    value = (locator or "").strip()
    if not value:
        return ""
    if ":" in value and ("\\" in value or "/" in value):
        return value.replace("\\", "/").rstrip("/").lower()
    return value.rstrip("/") or "/"


# ============================================================
# 来源与根
# ============================================================

def create_source(*, source_id: str, source_type: str, provider_id: str = "", ingest_method: str = "", connection_key: str = "", display_name: str = "") -> None:
    conn = get_connection()
    timestamp = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO sources (
            source_id, source_type, provider_id, ingest_method, connection_key,
            capabilities_json, display_name, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, '{}', ?, 'active', ?, ?)
        """,
        (source_id, source_type, provider_id, ingest_method, connection_key, display_name, timestamp, timestamp),
    )
    conn.commit()


def get_source(source_id: str) -> dict | None:
    """按 source_id 读取来源记录（含 source_type / provider_id / connection_key）。"""
    row = get_connection().execute(
        "SELECT * FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    return dict(row) if row else None


def set_source_capabilities(source_id: str, capabilities: dict) -> None:
    """更新来源能力声明（capabilities_json），只负责 JSON 序列化写入。

    模块4：OpenList 等来源在创建/复用时显式声明能力（native_delta /
    directory_verification / rolling_reconciliation），滚动策略据此决策；
    不加表、不 bump schema，来源不存在时静默（create_source 保证存在）。
    """
    import json

    get_connection().execute(
        "UPDATE sources SET capabilities_json = ?, updated_at = ? WHERE source_id = ?",
        (json.dumps(capabilities or {}, ensure_ascii=False), now_iso(), source_id),
    )
    get_connection().commit()


def _roots_with_prefix(source_id: str, normalized: str) -> list[SourceRootRecord]:
    rows = get_connection().execute(
        "SELECT * FROM source_roots WHERE source_id = ? ORDER BY normalized_locator",
        (source_id,),
    ).fetchall()
    return [SourceRootRecord.from_row(row) for row in rows]


def _locators_overlap(left: str, right: str) -> bool:
    """两个规范化定位是否互为祖先，兼容 POSIX 与 Windows 分隔符。"""
    left_value = (left or "").replace("\\", "/").rstrip("/") or "/"
    right_value = (right or "").replace("\\", "/").rstrip("/") or "/"
    if left_value == right_value:
        return True
    if left_value == "/" or right_value == "/":
        return True
    return left_value.startswith(right_value + "/") or right_value.startswith(left_value + "/")


def create_source_root(
    *,
    source_id: str,
    remote_locator: str,
    local_locator: str = "",
    import_family: str = "anime",
    import_scope: str = "",
    scan_policy: str = "standard",
) -> SourceRootRecord:
    """创建来源根；拒绝同来源中互相重叠的重复 root。"""
    normalized = normalize_locator(remote_locator)
    if not normalized:
        raise ValueError("来源根定位不能为空")
    for existing in _roots_with_prefix(source_id, normalized):
        if _locators_overlap(normalized, existing.normalized_locator):
            raise ValueError(
                f"来源根与既有根重叠: {normalized} 与 {existing.normalized_locator}"
            )
    conn = get_connection()
    timestamp = now_iso()
    root_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO source_roots (
            root_id, source_id, remote_locator, normalized_locator, local_locator,
            import_family, import_scope, scan_policy, active_generation,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (root_id, source_id, remote_locator, normalized, local_locator,
         import_family, import_scope, scan_policy, timestamp, timestamp),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM source_roots WHERE root_id = ?", (root_id,)).fetchone()
    return SourceRootRecord.from_row(row)


def get_source_root(root_id: str) -> SourceRootRecord | None:
    row = get_connection().execute(
        "SELECT * FROM source_roots WHERE root_id = ?", (root_id,)
    ).fetchone()
    return SourceRootRecord.from_row(row) if row else None


def list_source_roots(source_id: str = "") -> list[SourceRootRecord]:
    if source_id:
        rows = get_connection().execute(
            "SELECT * FROM source_roots WHERE source_id = ? ORDER BY created_at", (source_id,)
        ).fetchall()
    else:
        rows = get_connection().execute(
            "SELECT * FROM source_roots ORDER BY created_at"
        ).fetchall()
    return [SourceRootRecord.from_row(row) for row in rows]


# ============================================================
# 导入批次
# ============================================================

def _batch_root_rows(batch_id: str) -> list[dict]:
    rows = get_connection().execute(
        """
        SELECT batch_root.batch_id, batch_root.root_id, batch_root.sort_order,
               batch_root.status, batch_root.generation, batch_root.error_kind,
               root.source_id, root.remote_locator, root.normalized_locator,
               root.local_locator, root.import_family, root.import_scope, root.scan_policy
        FROM import_batch_roots AS batch_root
        JOIN source_roots AS root ON root.root_id = batch_root.root_id
        WHERE batch_root.batch_id = ?
        ORDER BY batch_root.sort_order, batch_root.root_id
        """,
        (batch_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def create_import_batch(
    *,
    source_id: str,
    roots: list[dict[str, Any]],
    import_family: str = "anime",
    mode: str = "auto_safe",
) -> dict:
    """原子创建一个导入批次及其多个不重叠来源根。

    批次、root 与关系表必须作为单个事实写入：任一个 locator 非法、来源不存在
    或与同来源既有/本批 root 重叠时，事务整体回滚，不留下半成品批次。

    重叠导入语义（由 ``app.catalog.lifecycle.resolve_root_for_import`` 解析）：
    - ``reuse_exact``：完全相同路径 → 复用既有 root（幂等增量）；
    - ``reuse_ancestor``：已有父目录覆盖新子目录 → 复用父 root，不创建子 root；
    - ``promote_parent``：新父目录覆盖已有子目录 → 先事务化归并子根到新父根，
      再复用父 root（unit/revision 保留）；
    - ``create``：全新路径 → 创建新 root。

    返回的 batch ``roots`` 每项附加 ``resolution`` / ``canonical_locator`` /
    ``requested_locator`` / ``covered_root_ids``，供 API 层向用户展示解析结果。
    """
    if not source_id:
        raise ValueError("source_id 不能为空")
    if not roots:
        raise ValueError("导入批次至少需要一个来源根")

    # 1. 只读解析每个请求（不执行任何写操作）：重叠语义由
    #    app.catalog.lifecycle.resolve_root_for_import 决策。promote_parent 的
    #    归并在下面同一个事务内执行，保证归并与批次创建原子（失败整体回滚，
    #    不会留下已替换子根却无批次的半成品）。
    from app.catalog.lifecycle import _promote_in_tx, resolve_root_for_import

    resolutions: list[Any] = []
    for raw_root in roots:
        remote_locator = str(raw_root.get("remote_locator") or "").strip()
        normalized = normalize_locator(remote_locator)
        if not normalized:
            raise ValueError("来源根定位不能为空")
        resolution = resolve_root_for_import(source_id, remote_locator)
        resolutions.append(resolution)

    conn = get_connection()
    timestamp = now_iso()
    batch_id = uuid.uuid4().hex
    prepared: list[dict[str, str]] = []
    prepared_resolutions: list[Any] = []

    with transaction(conn) as tx:
        if tx.execute(
            "SELECT 1 FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone() is None:
            raise ValueError("source 不存在")

        # 2. 在事务内执行 promote_parent 归并（建/复用父 root、重绑 unit/revision、
        #    删子根物理事实，回调前已在事务内复查 durable jobs）。
        #    同一批次 roots 互不为父子（_validate_batch_paths 已剔除），因此
        #    promote 至多触发一次；防御性按归一化 locator 去重。
        promote_seen: set[str] = set()
        for index, resolution in enumerate(resolutions):
            if resolution.action != "promote_parent":
                continue
            raw_root = roots[index]
            requested = normalize_locator(resolution.requested_locator)
            if requested in promote_seen:
                continue
            promote_seen.add(requested)
            parent_root_id = _promote_in_tx(
                tx,
                source_id,
                resolution.requested_locator,
                normalized_parent=requested,
                local_locator=str(raw_root.get("local_locator") or "").strip(),
                import_family=str(raw_root.get("import_family") or import_family or "anime").strip(),
                import_scope=str(raw_root.get("import_scope") or "").strip(),
                child_root_ids=resolution.covered_root_ids,
                now_iso=now_iso,
            )
            resolution.canonical_root_id = parent_root_id
            resolution.canonical_locator = resolution.requested_locator

        existing = [
            SourceRootRecord.from_row(row)
            for row in tx.execute(
                "SELECT * FROM source_roots WHERE source_id = ?", (source_id,)
            ).fetchall()
        ]

        # 3. 构建 prepared（按 canonical root 去重）：两个请求映射到同一个既有
        #    父根时只保留一个规范 root，避免 import_batch_roots 主键重复。
        seen_canonical: set[str] = set()
        for index, resolution in enumerate(resolutions):
            raw_root = roots[index]
            remote_locator = str(raw_root.get("remote_locator") or "").strip()
            normalized = normalize_locator(remote_locator)

            if resolution.action == "create":
                # 全新路径：防御性复查重叠（resolver 已排除，双保险）
                for prior in [*existing, *prepared]:
                    prior_locator = (
                        prior.normalized_locator
                        if isinstance(prior, SourceRootRecord)
                        else prior["normalized_locator"]
                    )
                    if _locators_overlap(normalized, prior_locator):
                        raise ValueError(
                            f"来源根与既有根重叠: {normalized} 与 {prior_locator}"
                        )
                root_id = uuid.uuid4().hex
                prepared.append(
                    {
                        "root_id": root_id,
                        "remote_locator": remote_locator,
                        "normalized_locator": normalized,
                        "local_locator": str(raw_root.get("local_locator") or "").strip(),
                        "import_family": str(raw_root.get("import_family") or import_family or "anime").strip(),
                        "import_scope": str(raw_root.get("import_scope") or "").strip(),
                        "scan_policy": str(raw_root.get("scan_policy") or "standard").strip(),
                        "reused": False,
                    }
                )
                prepared_resolutions.append(resolution)
                continue

            # reuse_exact / reuse_ancestor / promote_parent：复用解析出的规范 root
            canonical_id = resolution.canonical_root_id
            if canonical_id in seen_canonical:
                # 同一规范 root 已被本批次入表：去重，不再重复写 import_batch_roots
                continue
            canonical = next(
                (p for p in existing if p.root_id == canonical_id), None
            )
            if canonical is None:
                raise ValueError(
                    f"来源根解析失败: {remote_locator} 未找到规范来源根"
                )
            seen_canonical.add(canonical_id)
            prepared.append(
                {
                    "root_id": canonical.root_id,
                    "remote_locator": canonical.remote_locator,
                    "normalized_locator": canonical.normalized_locator,
                    "local_locator": canonical.local_locator
                    or str(raw_root.get("local_locator") or "").strip(),
                    "import_family": canonical.import_family
                    or str(raw_root.get("import_family") or import_family or "anime").strip(),
                    "import_scope": canonical.import_scope
                    or str(raw_root.get("import_scope") or "").strip(),
                    "scan_policy": canonical.scan_policy or "standard",
                    "reused": True,
                }
            )
            prepared_resolutions.append(resolution)

        # 4. 建批次 + 落 root/batch_roots
        tx.execute(
            """
            INSERT INTO import_batches (
                batch_id, status, mode, import_family, created_at, updated_at
            ) VALUES (?, 'pending', ?, ?, ?, ?)
            """,
            (batch_id, mode, import_family or "anime", timestamp, timestamp),
        )
        for sort_order, root in enumerate(prepared):
            if not root.get("reused"):
                tx.execute(
                    """
                    INSERT INTO source_roots (
                        root_id, source_id, remote_locator, normalized_locator, local_locator,
                        import_family, import_scope, scan_policy, active_generation,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        root["root_id"], source_id, root["remote_locator"],
                        root["normalized_locator"], root["local_locator"],
                        root["import_family"], root["import_scope"], root["scan_policy"],
                        timestamp, timestamp,
                    ),
                )
            tx.execute(
                """
                INSERT INTO import_batch_roots (
                    batch_id, root_id, sort_order, status, generation, error_kind
                ) VALUES (?, ?, ?, 'pending', 0, '')
                """,
                (batch_id, root["root_id"], sort_order),
            )

    batch = get_import_batch(batch_id) or {}
    from app.catalog.lifecycle import resolution_api_label

    for root_item, resolution in zip(
        batch.get("roots", []), prepared_resolutions, strict=False
    ):
        root_item["resolution"] = resolution_api_label(resolution.action)
        root_item["requested_locator"] = resolution.requested_locator
        root_item["canonical_locator"] = resolution.canonical_locator
        root_item["covered_root_ids"] = list(resolution.covered_root_ids)
    return batch


def get_import_batch(batch_id: str) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM import_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        return None
    batch = dict(row)
    batch["roots"] = _batch_root_rows(batch_id)
    return batch


def list_import_batches(*, limit: int = 100) -> list[dict]:
    rows = get_connection().execute(
        "SELECT * FROM import_batches ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [get_import_batch(str(row["batch_id"])) for row in rows]


def list_import_batch_roots(batch_id: str) -> list[dict]:
    return _batch_root_rows(batch_id)


def update_import_batch(batch_id: str, *, status: str) -> None:
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE import_batches SET status = ?, updated_at = ? WHERE batch_id = ?",
        (status, now_iso(), batch_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise KeyError(f"导入批次不存在: {batch_id}")


def update_import_batch_root(
    batch_id: str,
    root_id: str,
    *,
    status: str | None = None,
    generation: int | None = None,
    error_kind: str | None = None,
) -> None:
    fields: dict[str, Any] = {}
    if status is not None:
        fields["status"] = status
    if generation is not None:
        fields["generation"] = generation
    if error_kind is not None:
        fields["error_kind"] = error_kind
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = [*fields.values(), batch_id, root_id]
    conn = get_connection()
    cursor = conn.execute(
        f"UPDATE import_batch_roots SET {assignments} WHERE batch_id = ? AND root_id = ?",
        values,
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise KeyError(f"导入批次根不存在: {batch_id}/{root_id}")


def bump_generation(root_id: str) -> int:
    """递增 root 的 active_generation，返回新值。"""
    conn = get_connection()
    conn.execute(
        "UPDATE source_roots SET active_generation = active_generation + 1, updated_at = ? WHERE root_id = ?",
        (now_iso(), root_id),
    )
    conn.commit()
    row = conn.execute("SELECT active_generation FROM source_roots WHERE root_id = ?", (root_id,)).fetchone()
    return int(row[0]) if row else 0


def update_root_metadata(root_id: str, *, import_family: str = "", import_scope: str = "") -> None:
    """更新来源根的 family/scope 元数据（不改变 locator，供复用既有根时对齐语义）。"""
    if not root_id:
        return
    get_connection().execute(
        """
        UPDATE source_roots
        SET import_family = CASE WHEN ? != '' THEN ? ELSE import_family END,
            import_scope = CASE WHEN ? IS NOT NULL THEN ? ELSE import_scope END,
            updated_at = ?
        WHERE root_id = ?
        """,
        (import_family, import_family, import_scope, import_scope, now_iso(), root_id),
    )
    get_connection().commit()

def bind_root_to_openlist(
    root_id: str,
    *,
    openlist_conn_hash: str,
    openlist_remote_locator: str,
) -> None:
    """RWK-3：把 Provider root 绑定到可选 OpenList 增量通道。

    只写 binding 元数据，不改变 root identity / source / media_units；
    后续对该 root 的 scan 使用 scan_channel=openlist 时，同一 root 复用。
    """
    if not root_id or not openlist_conn_hash:
        return
    get_connection().execute(
        """
        UPDATE source_roots
        SET openlist_conn_hash = ?, openlist_remote_locator = ?, updated_at = ?
        WHERE root_id = ?
        """,
        (openlist_conn_hash, openlist_remote_locator or "", now_iso(), root_id),
    )
    get_connection().commit()


def touch_successful_scan(root_id: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE source_roots SET last_successful_scan_at = ?, updated_at = ? WHERE root_id = ?",
        (now_iso(), now_iso(), root_id),
    )
    conn.commit()


# ============================================================
# scan_runs
# ============================================================

def create_scan_run(root_id: str, generation: int, mode: str = "full") -> str:
    conn = get_connection()
    run_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO scan_runs (run_id, root_id, generation, mode, status, started_at)
        VALUES (?, ?, ?, ?, 'queued', ?)
        """,
        (run_id, root_id, generation, mode, now_iso()),
    )
    conn.commit()
    return run_id


def update_scan_run(run_id: str, status: str, error: str = "") -> None:
    conn = get_connection()
    conn.execute(
        """
        UPDATE scan_runs SET status = ?, finished_at = ?, error = ? WHERE run_id = ?
        """,
        (status, now_iso() if status in ("succeeded", "failed", "cancelled") else "", error, run_id),
    )
    conn.commit()


# ============================================================
# 目录检查点
# ============================================================

def upsert_directory(root_id: str, remote_path: str, *, parent_path: str = "", depth: int = 0) -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO source_directories (
            root_id, remote_path, parent_path, depth, state
        ) VALUES (?, ?, ?, ?, 'queued')
        """,
        (root_id, remote_path, parent_path, depth),
    )
    conn.commit()


def get_directory(root_id: str, remote_path: str) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM source_directories WHERE root_id = ? AND remote_path = ?",
        (root_id, remote_path),
    ).fetchone()
    return dict(row) if row else None


def update_directory(root_id: str, remote_path: str, **fields: Any) -> None:
    allowed = {
        "state", "accepted_generation", "entry_count", "member_hash",
        "last_verified_at", "last_remote_verified_at", "next_verify_at",
        "retry_count", "last_error_kind",
    }
    assignments = [f"{key} = ?" for key in fields if key in allowed]
    if not assignments:
        return
    values = [fields[key] for key in fields if key in allowed]
    values.extend((root_id, remote_path))
    get_connection().execute(
        f"UPDATE source_directories SET {', '.join(assignments)} WHERE root_id = ? AND remote_path = ?",
        values,
    )
    get_connection().commit()


def list_pending_directories(root_id: str, *, limit: int = 200) -> list[dict]:
    """frontier 扫描：待扫描（queued）目录按深度优先。

    不领取 failed 目录：单次扫描中失败目录由调用方记录后跳过，避免同一轮
    内无限重试卡死队列；下次任务触发时由 ``prepare_scan`` 统一恢复为
    queued 再重试。
    """
    rows = get_connection().execute(
        """
        SELECT * FROM source_directories
        WHERE root_id = ? AND state = 'queued'
        ORDER BY depth ASC, remote_path ASC
        LIMIT ?
        """,
        (root_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_all_directories(root_id: str) -> list[dict]:
    rows = get_connection().execute(
        "SELECT * FROM source_directories WHERE root_id = ? ORDER BY depth, remote_path",
        (root_id,),
    ).fetchall()
    return [dict(row) for row in rows]
def remote_baseline_coverage(root_id: str) -> dict:
    """HYB-3：远端基线覆盖率统计。

    返回 {total_directories, remote_verified_count, coverage}：
    - total_directories：该 root 已知目录数（含 queued/complete）；
    - remote_verified_count：OpenList 真正 list 验证过的目录数
      （last_remote_verified_at 非空）；
    - coverage：0.0~1.0 比例（无目录时为 1.0）。
    """
    conn = get_connection()
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ?",
            (root_id,),
        ).fetchone()["c"]
    )
    if total == 0:
        return {"total_directories": 0, "remote_verified_count": 0, "coverage": 1.0}
    verified = int(
        conn.execute(
            """
            SELECT COUNT(*) AS c FROM source_directories
            WHERE root_id = ? AND last_remote_verified_at != ''
            """,
            (root_id,),
        ).fetchone()["c"]
    )
    return {
        "total_directories": total,
        "remote_verified_count": verified,
        "coverage": round(verified / total, 4),
    }
def prepare_scan(root_id: str, *, generation: int, mode: str = "incremental") -> None:
    """把需要验证的目录写入持久 frontier。

    ``incremental`` 只重新验证根目录、失败目录和到期目录；``full`` 才将整棵
    已知目录树重新排队。这样 OpenList 的日常更新不会退化成每次全量递归扫描。
    """
    if mode not in {"incremental", "full"}:
        raise ValueError(f"未知扫描模式: {mode}")
    root = get_source_root(root_id)
    if root is None:
        raise ValueError("source root 不存在")
    if generation < root.active_generation:
        return
    timestamp = now_iso()
    conn = get_connection()
    with transaction(conn) as tx:
        existing = tx.execute(
            "SELECT COUNT(*) AS count FROM source_directories WHERE root_id = ?",
            (root_id,),
        ).fetchone()
        if not existing or int(existing["count"] or 0) == 0:
            tx.execute(
                """
                INSERT OR IGNORE INTO source_directories (
                    root_id, remote_path, parent_path, depth, state
                ) VALUES (?, ?, '', 0, 'queued')
                """,
                (root_id, root.remote_locator),
            )
            return
        if mode == "full":
            tx.execute(
                """
                UPDATE source_directories SET state = 'queued'
                WHERE root_id = ? AND state != 'scanning'
                """,
                (root_id,),
            )
            return
        # 日常更新：根目录发现新增/移除直属成员；失败与到期目录负责滚动收敛。
        tx.execute(
            "UPDATE source_directories SET state = 'queued' WHERE root_id = ? AND remote_path = ?",
            (root_id, root.remote_locator),
        )
        tx.execute(
            """
            UPDATE source_directories SET state = 'queued'
            WHERE root_id = ? AND state = 'failed'
            """,
            (root_id,),
        )
        tx.execute(
            """
            UPDATE source_directories SET state = 'queued'
            WHERE root_id = ? AND state = 'complete'
              AND next_verify_at != '' AND next_verify_at <= ?
            """,
            (root_id, timestamp),
        )
        # HYB-5：rolling baseline learning 预算——从未被 OpenList 验证过
        # （last_remote_verified_at=''）且**没有未来到期安排**（next_verify_at
        # 为空或已到期）的 complete 目录每轮只取有限数量入队，选“最久未
        # 验证 + stable hash jitter”而非全部同时到期，避免一轮变相全扫。
        # 已有未来到期安排的目录（next_verify_at 在未来）不被 baseline
        # 提前拉取，保持其既定滚动节奏。
        tx.execute(
            """
            UPDATE source_directories SET state = 'queued'
            WHERE root_id = ? AND state = 'complete'
              AND last_remote_verified_at = ''
              AND (next_verify_at = '' OR next_verify_at <= ?)
              AND remote_path IN (
                  SELECT remote_path FROM source_directories
                  WHERE root_id = ? AND state = 'complete'
                    AND last_remote_verified_at = ''
                    AND (next_verify_at = '' OR next_verify_at <= ?)
                  ORDER BY last_verified_at ASC, remote_path ASC
                  LIMIT ?
              )
            """,
            (root_id, timestamp, root_id, timestamp, BASELINE_VERIFY_BUDGET),
        )


def recover_interrupted_directories(root_id: str) -> int:
    """重启恢复：遗留 scanning 恢复为 queued（complete 目录不重扫）。"""
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE source_directories SET state = 'queued', last_error_kind = 'interrupted' WHERE root_id = ? AND state = 'scanning'",
        (root_id,),
    )
    conn.commit()
    return cursor.rowcount


# ============================================================
# 分页暂存
# ============================================================

def new_stage_run() -> str:
    return uuid.uuid4().hex


def register_stage_run(run_id: str, root_id: str) -> None:
    """记录一次分页扫描 run 的来源根归属（用于删除/归并时精确清理暂存）。"""
    conn = get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO source_stage_runs (run_id, root_id, created_at)
        VALUES (?, ?, ?)
        """,
        (run_id, root_id, now_iso()),
    )
    conn.commit()


def clear_stage(run_id: str) -> None:
    """清空一次 run 的分页暂存及其归属记录（完成/重试/取消均调用，避免孤儿）。"""
    conn = get_connection()
    with transaction(conn) as tx:
        tx.execute("DELETE FROM source_stage_entries WHERE run_id = ?", (run_id,))
        tx.execute("DELETE FROM source_stage_runs WHERE run_id = ?", (run_id,))


def stage_run_ids_for_roots(root_ids: list[str]) -> list[str]:
    """查出一组来源根此前登记的所有分页 run_id（用于精确清理）."""
    if not root_ids:
        return []
    marks = ",".join("?" for _ in root_ids)
    rows = get_connection().execute(
        f"SELECT run_id FROM source_stage_runs WHERE root_id IN ({marks})", root_ids
    ).fetchall()
    return [str(row[0]) for row in rows]


def delete_stage_for_roots(root_ids: list[str]) -> None:
    """按来源根精确清理其分页暂存（run 归属 + 暂存条目），无孤儿残留。"""
    if not root_ids:
        return
    marks = ",".join("?" for _ in root_ids)
    conn = get_connection()
    with transaction(conn) as tx:
        tx.execute(
            f"DELETE FROM source_stage_entries WHERE run_id IN (SELECT run_id FROM source_stage_runs WHERE root_id IN ({marks}))",
            root_ids,
        )
        tx.execute(
            f"DELETE FROM source_stage_runs WHERE root_id IN ({marks})", root_ids
        )


def add_stage_page(run_id: str, directory_path: str, page: int, entries: list[SourceNodeInput]) -> None:
    conn = get_connection()
    with transaction(conn) as tx:
        tx.executemany(
            """
            INSERT OR REPLACE INTO source_stage_entries (
                run_id, directory_path, remote_path, page, name, kind, size, mtime, logical_locator
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id, directory_path, item.remote_path, page,
                    item.name, item.kind, item.size, item.mtime,
                    getattr(item, "logical_locator", "") or "",
                )
                for item in entries
                if item.remote_path
            ],
        )


def get_stage_entries(run_id: str) -> list[dict]:
    rows = get_connection().execute(
        "SELECT * FROM source_stage_entries WHERE run_id = ? ORDER BY page, remote_path",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def stage_member_hash(run_id: str) -> str:
    """排序后的直属成员 hash（目录检查点用）。"""
    lines = []
    for row in sorted(get_stage_entries(run_id), key=lambda item: item["remote_path"]):
        lines.append(
            "\t".join(
                (
                    row["remote_path"],
                    row["kind"],
                    str(row["size"] if row["size"] is not None else ""),
                    str(int(row["mtime"]) if row["mtime"] is not None else ""),
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ============================================================
# 目录原子提交
# ============================================================

def commit_directory(
    root_id: str,
    remote_path: str,
    run_id: str,
    generation: int,
    *,
    max_batch: int = BATCH_WRITE_LIMIT,
    remote_verified: bool = False,
) -> dict:
    """把完整分页读取的暂存区原子合并到 source_nodes 并落 directory checkpoint。

    - 单事务：node 新增/更新 + 消失项 tombstone + checkpoint；
    - 未完成分页（调用方保证）不得调用本函数，否则不产生 tombstone；
    - 返回 diff 统计 {added, updated, missing, unchanged}。
    """
    conn = get_connection()
    stage = get_stage_entries(run_id)
    timestamp = now_iso()
    member_hash = stage_member_hash(run_id)
    stats = {"added": 0, "updated": 0, "missing": 0, "unchanged": 0}

    with transaction(conn) as tx:
        # active_generation 提交 fence：新 generation 开始后，旧 generation 的
        # 迟到提交不得写入（否则会混入旧代事实、破坏增量语义）。
        root_row = tx.execute(
            "SELECT active_generation FROM source_roots WHERE root_id = ?", (root_id,)
        ).fetchone()
        if root_row is not None and int(root_row["active_generation"]) > generation:
            tx.execute("DELETE FROM source_stage_entries WHERE run_id = ?", (run_id,))
            tx.execute("DELETE FROM source_stage_runs WHERE run_id = ?", (run_id,))
            return stats  # 丢弃旧代提交（不抛错，调用方按正常返回处理）

        # 只读取本目录的直属成员（parent_path 精确匹配）：
        # 若用 remote_path LIKE prefix% 会拉入深层子目录节点，
        # 父目录提交时会把尚未扫描的深层内容误 tombstone。
        current_rows = tx.execute(
            "SELECT * FROM source_nodes WHERE root_id = ? AND parent_path = ?",
            (root_id, remote_path),
        ).fetchall()
        current = {row["remote_path"]: row for row in current_rows}
        stage_paths = set()
        directory_row = tx.execute(
            "SELECT depth FROM source_directories WHERE root_id = ? AND remote_path = ?",
            (root_id, remote_path),
        ).fetchone()
        child_depth = int(directory_row["depth"] if directory_row else 0) + 1

        for offset in range(0, len(stage), max_batch):
            chunk = stage[offset:offset + max_batch]
            for row in chunk:
                remote = row["remote_path"]
                stage_paths.add(remote)
                existing = current.get(remote)
                shape = (
                    existing["kind"] == row["kind"]
                    and existing["size"] == row["size"]
                ) if existing else False
                if existing is None:
                    stats["added"] += 1
                    tx.execute(
                        """
                        INSERT INTO source_nodes (
                            root_id, remote_path, parent_path, name, kind, size, mtime,
                            logical_locator,
                            first_seen_generation, last_seen_generation, tombstone
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                        """,
                        (root_id, remote, row["directory_path"], row["name"], row["kind"],
                         row["size"], row["mtime"], row.get("logical_locator") or "",
                         generation, generation),
                    )
                elif shape and existing["mtime"] == row["mtime"]:
                    stats["unchanged"] += 1
                    tx.execute(
                        """
                        UPDATE source_nodes
                        SET last_seen_generation = ?, tombstone = '',
                            logical_locator = COALESCE(?, logical_locator)
                        WHERE root_id = ? AND remote_path = ?
                        """,
                        (generation, row.get("logical_locator") or "", root_id, remote),
                    )
                else:
                    stats["updated"] += 1
                    tx.execute(
                        """
                        UPDATE source_nodes
                        SET kind = ?, size = ?, mtime = ?, last_seen_generation = ?, tombstone = '',
                            logical_locator = COALESCE(?, logical_locator)
                        WHERE root_id = ? AND remote_path = ?
                        """,
                        (row["kind"], row["size"], row["mtime"], generation,
                         row.get("logical_locator") or "", root_id, remote),
                    )

                # 目录 frontier 是持久状态：父目录一次完整提交后，把每个直属
                # 子目录写成 queued。进程中断后 worker 从这里继续，不重建内存树。
                if row["kind"] == "dir":
                    # HYB-4（mtime 三态下钻）：区分
                    #   SAME    —— 双方均非 None 且相等 → 不下钻，等 rolling verify
                    #   CHANGED —— 双方均非 None 且不等 → 立即 requeue 下钻
                    #   UNKNOWN —— old=None（TXT bootstrap 遗留，无可信远端基线）
                    #              → 记录 new mtime（node 已更新），但**不**立即
                    #              全树展开，交给 baseline learning 分批验证；
                    #              否则第一次 OpenList 增量会退化成全树扫描，
                    #              吃掉 TXT bootstrap 的收益。
                    # UPDATE 未命中（目录曾消失、checkpoint 已被级联删除）时
                    # 回退 INSERT 重建 queued（新目录 → 当轮发现并扫描）。
                    requeued = False
                    old_mtime = existing["mtime"] if existing is not None else None
                    new_mtime = row["mtime"]
                    changed = (
                        old_mtime is not None
                        and new_mtime is not None
                        and old_mtime != new_mtime
                    )
                    unknown = (
                        old_mtime is None
                        and new_mtime is not None
                        and existing is not None
                        and existing["kind"] == "dir"
                    )
                    if changed:
                        cursor = tx.execute(
                            """
                            UPDATE source_directories
                            SET state = 'queued'
                            WHERE root_id = ? AND remote_path = ?
                              AND state != 'scanning'
                            """,
                            (root_id, remote),
                        )
                        requeued = cursor.rowcount > 0
                    if not requeued and not unknown:
                        tx.execute(
                            """
                            INSERT OR IGNORE INTO source_directories (
                                root_id, remote_path, parent_path, depth, state
                            ) VALUES (?, ?, ?, ?, 'queued')
                            """,
                            (root_id, remote, remote_path, child_depth),
                        )

        # 完整目录读取后：旧条目未出现且未 tombstone → missing
        for remote, row in current.items():
            if row["tombstone"]:
                continue
            if remote in stage_paths:
                continue
            if int(row["last_seen_generation"]) < generation:
                stats["missing"] += 1
                tx.execute(
                    "UPDATE source_nodes SET tombstone = ? WHERE root_id = ? AND remote_path = ?",
                    (timestamp, root_id, remote),
                )
                # 目录消失 → 级联整棵物理子树（同一事务）：
                # - 所有 child/ 后代的 source_nodes → tombstone（保留历史，供回溯）
                # - child 与所有后代的 source_directories checkpoint → 直接 DELETE
                #   （node tombstone 已保留历史、frontier 不再需要该子树；目录重新
                #   出现时父目录提交会自然重建 queued checkpoint，避免 full scan
                #   把 removed checkpoint 又 queue）；
                # - 用 substr 前缀匹配（非 LIKE），避免 remote_path 中的 % _ \
                #   被当作通配符误伤其他目录。
                if row["kind"] == "dir":
                    prefix = remote + "/"
                    cursor = tx.execute(
                        """
                        UPDATE source_nodes SET tombstone = ?
                        WHERE root_id = ? AND tombstone = ''
                          AND substr(remote_path, 1, length(?)) = ?
                          AND length(remote_path) > length(?)
                        """,
                        (timestamp, root_id, prefix, prefix, prefix),
                    )
                    stats["missing"] += cursor.rowcount
                    tx.execute(
                        """
                        DELETE FROM source_directories
                        WHERE root_id = ?
                          AND (remote_path = ? OR (
                              substr(remote_path, 1, length(?)) = ?
                              AND length(remote_path) > length(?)
                          ))
                        """,
                        (root_id, remote, prefix, prefix, prefix),
                    )

        tx.execute("DELETE FROM source_stage_entries WHERE run_id = ?", (run_id,))
        tx.execute("DELETE FROM source_stage_runs WHERE run_id = ?", (run_id,))
        # HYB-5：滚动验证到期时间按 stable hash(remote_path) 确定性抖动
        # （0~24h 叠加在 24h 上），把目录分散到不同到期时刻——避免 TXT
        # bootstrap 导入的目录在同一时刻全部到期（“滚动验证又是全扫”）。
        jitter_seconds = int(hashlib.md5(str(remote_path).encode("utf-8")).hexdigest(), 16) % int(ROLLING_JITTER_WINDOW.total_seconds())
        next_verify_at = (
            datetime.now(timezone(timedelta(hours=8)))
            + VERIFY_INTERVAL
            + timedelta(seconds=jitter_seconds)
        ).isoformat()
        # HYB-3：区分「TXT 快照见过」与「OpenList 真正 list 验证过」。
        # OpenList 通道提交成功 → last_remote_verified_at=now；
        # snapshot 通道（TXT）提交 → 置空（远端未验证）。
        verified_at = timestamp if remote_verified else ""
        tx.execute(
            """
            UPDATE source_directories
            SET state = 'complete', accepted_generation = ?, entry_count = ?,
                member_hash = ?, last_verified_at = ?, next_verify_at = ?,
                last_remote_verified_at = ?, last_error_kind = ''
            WHERE root_id = ? AND remote_path = ?
            """,
            (generation, len(stage), member_hash, timestamp, next_verify_at,
             verified_at, root_id, remote_path),
        )
    return stats


def update_node_provider(root_id: str, remote_path: str, provider_id: str, route_id: str) -> None:
    """回填节点的提供商事实（目录级路由匹配结果，随入库持久化）。"""
    get_connection().execute(
        """
        UPDATE source_nodes
        SET provider_id = ?, route_id = ?, logical_locator = COALESCE(logical_locator, '')
        WHERE root_id = ? AND remote_path = ?
        """,
        (provider_id, route_id, root_id, remote_path),
    )
    get_connection().commit()


def list_nodes(root_id: str, *, include_tombstone: bool = False) -> list[dict]:
    if include_tombstone:
        rows = get_connection().execute(
            "SELECT * FROM source_nodes WHERE root_id = ? ORDER BY remote_path", (root_id,)
        ).fetchall()
    else:
        rows = get_connection().execute(
            "SELECT * FROM source_nodes WHERE root_id = ? AND tombstone = '' ORDER BY remote_path",
            (root_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_current_children(root_id: str, parent_path: str) -> list[dict]:
    """读取一个目录的当前直接子项，供发现器做候选判断。

    这是 ``source_nodes(root_id, parent_path)`` 索引查询，避免发现器在每个
    frontier 目录上把整棵来源树加载到 Python 后再过滤。
    """
    rows = get_connection().execute(
        """
        SELECT * FROM source_nodes
        WHERE root_id = ? AND parent_path = ? AND tombstone = ''
        ORDER BY remote_path
        """,
        (root_id, parent_path),
    ).fetchall()
    return [dict(row) for row in rows]


def list_current_nodes_in_boundary(root_id: str, boundary_path: str) -> list[dict]:
    """读取一个作品边界下的当前节点，供识别/修订生成使用。"""
    prefix = boundary_path.rstrip("/") or "/"
    if prefix == "/":
        rows = get_connection().execute(
            """
            SELECT * FROM source_nodes
            WHERE root_id = ? AND tombstone = ''
            ORDER BY remote_path
            """,
            (root_id,),
        ).fetchall()
    else:
        rows = get_connection().execute(
            """
            SELECT * FROM source_nodes
            WHERE root_id = ? AND tombstone = ''
              AND (remote_path = ? OR substr(remote_path, 1, length(?)) = ?)
            ORDER BY remote_path
            """,
            (root_id, prefix, prefix + "/", prefix + "/"),
        ).fetchall()
    return [dict(row) for row in rows]
