"""不可变 Import Revision 存储（SQLite）。

- revision hash 未变化时不创建新 revision；
- 变化时以当前 confirmed revision 为 parent；
- 缺失条目保留身份并标记 unavailable；
- ImportPlan dataclass 对外保留，由本模块装载（镜像/刮削调用方无需同时重写）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.database import get_connection
from app.db.transactions import transaction

REVISION_STATUSES = ("draft", "confirmed", "executed", "superseded", "failed")


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def items_hash(items: list[dict]) -> str:
    """按条目规范字段排序计算 revision hash（结构变化才产生新 revision）。"""
    lines = []
    for item in sorted(items, key=lambda entry: str(entry.get("relative_path") or "")):
        lines.append(
            "\t".join(
                (
                    str(item.get("relative_path") or ""),
                    str(item.get("resource_type") or ""),
                    str(item.get("work_id") or ""),
                    str(item.get("series_group") or ""),
                    str(item.get("group_type") or ""),
                    str(item.get("season_number") or ""),
                    str(item.get("episode_number") or ""),
                    str(item.get("availability") or "available"),
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def create_revision(
    *,
    unit_id: str,
    source_generation: int,
    items: list[dict],
    parent_revision_id: str = "",
    status: str = "draft",
    confirm_method: str = "",
) -> dict:
    """创建不可变 revision（含 items）；同 unit 同 hash 的 confirmed revision 不重复创建。"""
    conn = get_connection()
    revision_hash = items_hash(items)
    existing = conn.execute(
        """
        SELECT revision_id FROM import_revisions
        WHERE unit_id = ? AND hash = ? AND status IN ('confirmed', 'executed')
        LIMIT 1
        """,
        (unit_id, revision_hash),
    ).fetchone()
    if existing is not None:
        return load_revision(existing["revision_id"])  # type: ignore[return-value]

    timestamp = now_iso()
    revision_id = uuid.uuid4().hex
    with transaction(conn) as tx:
        tx.execute(
            """
            INSERT INTO import_revisions (
                revision_id, unit_id, parent_revision_id, source, provider_id,
                source_generation, status, hash, confirm_method, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (revision_id, unit_id, parent_revision_id,
             str(items[0].get("source") or "openlist") if items else "openlist",
             str(items[0].get("provider_id") or "") if items else "",
             source_generation, status, revision_hash, confirm_method, timestamp, timestamp),
        )
        for item in items:
            tx.execute(
                """
                INSERT INTO import_revision_items (
                    revision_id, item_id, source, provider_id, relative_path,
                    real_path, logical_locator, resource_type, action, work_id,
                    work_title, series_group, card_type, group_type, season_number,
                    episode_number, title, target_dir, target_strm_path,
                    confidence, needs_review, override_json, availability
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id, str(item.get("id") or uuid.uuid4().hex),
                    str(item.get("source") or ""), str(item.get("provider_id") or ""),
                    str(item.get("relative_path") or ""), str(item.get("real_path") or ""),
                    str(item.get("logical_locator") or ""), str(item.get("resource_type") or "other"),
                    str(item.get("action") or "ignore"), str(item.get("work_id") or ""),
                    str(item.get("work_title") or ""), str(item.get("series_group") or ""),
                    str(item.get("card_type") or ""), str(item.get("group_type") or ""),
                    item.get("season_number"),
                    item.get("episode_number"), str(item.get("title") or ""),
                    str(item.get("target_dir") or ""), str(item.get("target_strm_path") or ""),
                    str(item.get("confidence") or "medium"), int(bool(item.get("needs_review"))),
                    json.dumps(item.get("override") or {}, ensure_ascii=False),
                    str(item.get("availability") or "available"),
                ),
            )
    return load_revision(revision_id)  # type: ignore[return-value]


def _row_to_plan(revision: dict, items: list[dict]) -> Any:
    """装载为 ImportPlan dataclass（对外兼容；镜像/刮削调用方不变）。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem

    plan_items = []
    for item in items:
        plan_items.append(
            ImportPlanItem(
                id=item["item_id"],
                plan_id=revision["revision_id"],
                source=item["source"],
                provider_id=item["provider_id"],
                relative_path=item["relative_path"],
                real_path=item["real_path"],
                resource_type=item["resource_type"],
                action=item["action"],
                work_id=item["work_id"],
                work_title=item["work_title"],
                series_group=item["series_group"],
                card_type=item.get("card_type") or "",
                group_type=item["group_type"],
                season_number=item["season_number"],
                episode_number=item["episode_number"],
                title=item["title"],
                target_dir=item["target_dir"],
                target_strm_path=item["target_strm_path"],
                confidence=item["confidence"],
                needs_review=bool(item["needs_review"]),
                availability=item["availability"],
            )
        )
    return ImportPlan(
        plan_id=revision["revision_id"],
        source=str(revision.get("source") or ""),
        provider_id=str(revision.get("provider_id") or ""),
        source_snapshot_id=revision["unit_id"],
        status=revision["status"],
        items=plan_items,
        created_at=revision["created_at"],
        updated_at=revision["updated_at"],
    )


def load_revision(revision_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM import_revisions WHERE revision_id = ?", (revision_id,)
    ).fetchone()
    if row is None:
        return None
    revision = dict(row)
    items = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM import_revision_items WHERE revision_id = ? ORDER BY relative_path",
            (revision_id,),
        ).fetchall()
    ]
    revision["items"] = items
    return revision


def load_plan(revision_id: str) -> Any | None:
    """按 revision 装载 ImportPlan（dataclass 兼容）。"""
    revision = load_revision(revision_id)
    if revision is None:
        return None
    return _row_to_plan(revision, revision.get("items") or [])


def update_revision_status(revision_id: str, status: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE import_revisions SET status = ?, updated_at = ? WHERE revision_id = ?",
        (status, now_iso(), revision_id),
    )
    conn.commit()


def try_auto_confirm_revision(revision_id: str) -> tuple[bool, str]:
    """以与人工确认相同的安全规则确认 SQLite revision。

    这是渐进发现器唯一允许自动进入镜像的门槛：只读 SQLite revision，
    不写回旧 JSON ImportPlan；发现需要复核或错误时保留 draft，并把单元标为
    ``needs_review``。返回 ``(是否确认, 原因)``，供调用方决定是否入队。
    """
    revision = load_revision(revision_id)
    if revision is None:
        return False, "revision 不存在"
    if revision["status"] != "draft":
        return revision["status"] in ("confirmed", "executed"), "revision 已处理"
    plan = load_plan(revision_id)
    if plan is None:
        return False, "无法装载 revision"

    from app.import_plan.service import build_preview

    preview = build_preview(plan)
    blockers = [
        issue for issue in preview.issues
        if issue.code == "needs_review" or issue.level == "error"
    ]
    if blockers:
        details = "; ".join(issue.code or issue.message for issue in blockers)
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status = 'needs_review', updated_at = ? WHERE unit_id = ?",
            (now_iso(), revision["unit_id"]),
        )
        conn.commit()
        return False, details

    timestamp = now_iso()
    conn = get_connection()
    with transaction(conn) as tx:
        tx.execute(
            """
            UPDATE import_revisions
            SET status = 'confirmed', confirm_method = 'automatic', updated_at = ?
            WHERE revision_id = ? AND status = 'draft'
            """,
            (timestamp, revision_id),
        )
        tx.execute(
            """
            UPDATE media_units
            SET status = 'confirmed', current_revision_id = ?, updated_at = ?
            WHERE unit_id = ?
            """,
            (revision_id, timestamp, revision["unit_id"]),
        )
    return True, ""


def persist_execution_fields(plan: Any) -> None:
    """回写镜像阶段生成的投影定位字段。

    target_dir / target_strm_path 在镜像根和冲突规则确定后才可得，但它们必须
    随 revision 持久化；后续 scrape、LibraryIndex 重建只能从 revision 读取。
    """
    revision_id = str(getattr(plan, "plan_id", "") or "")
    if not revision_id:
        raise ValueError("缺少 revision_id，不能保存镜像目标")
    conn = get_connection()
    with transaction(conn) as tx:
        for item in getattr(plan, "items", []):
            tx.execute(
                """
                UPDATE import_revision_items
                SET target_dir = ?, target_strm_path = ?
                WHERE revision_id = ? AND item_id = ?
                """,
                (
                    str(getattr(item, "target_dir", "") or ""),
                    str(getattr(item, "target_strm_path", "") or ""),
                    revision_id,
                    str(getattr(item, "id", "") or ""),
                ),
            )


def list_revisions(unit_id: str = "") -> list[dict]:
    conn = get_connection()
    if unit_id:
        rows = conn.execute(
            "SELECT * FROM import_revisions WHERE unit_id = ? ORDER BY created_at DESC", (unit_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM import_revisions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def latest_confirmed_revision(unit_id: str) -> dict | None:
    row = get_connection().execute(
        """
        SELECT * FROM import_revisions
        WHERE unit_id = ? AND status IN ('confirmed', 'executed')
        ORDER BY created_at DESC LIMIT 1
        """,
        (unit_id,),
    ).fetchone()
    return dict(row) if row else None


def mark_missing_items(revision_id: str, missing_relative_paths: set[str]) -> None:
    """新 revision 相对 parent：缺失条目标记 unavailable（保留身份）。"""
    conn = get_connection()
    with transaction(conn) as tx:
        for relative in missing_relative_paths:
            tx.execute(
                """
                UPDATE import_revision_items
                SET availability = 'unavailable'
                WHERE revision_id = ? AND relative_path = ?
                """,
                (revision_id, relative),
            )
