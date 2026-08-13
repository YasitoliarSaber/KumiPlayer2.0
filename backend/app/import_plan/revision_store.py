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


class RevisionStatusError(RuntimeError):
    """revision 状态不允许该操作（语义 409 冲突）。"""


#: import_revision_items 全语义列（INSERT/UPDATE 共用，保持单一事实来源）。
#: 除历史遗留列（logical_locator/real_path/override_json）外，与 ImportPlanItem 对齐。
REVISION_ITEM_COLUMNS: tuple[str, ...] = (
    "item_id", "source", "provider_id", "relative_path", "real_path",
    "logical_locator", "resource_type", "action", "work_id", "work_title",
    "original_title", "year", "media_type", "show_type", "series_group",
    "card_type", "belongs_to_series", "relation_type", "group_type",
    "season_number", "episode_number", "special_number", "title",
    "target_dir", "target_strm_path", "confidence", "needs_review",
    "override_json", "warnings_json", "reasons_json", "user_override_id",
    "availability",
)

#: 不含主键列（item_id），供 UPDATE SET 使用。
_ITEM_UPDATE_COLUMNS: tuple[str, ...] = tuple(
    col for col in REVISION_ITEM_COLUMNS if col != "item_id"
)


#: semantic hash 字段集（规划员 2026-08-12 指定）。
#: target_dir / target_filename / target_strm_path / warnings / reasons /
#: user_override_id 是执行或审计载荷，不参与结构判定。
_HASH_FIELDS: tuple[str, ...] = (
    "relative_path", "resource_type", "action", "work_id", "work_title",
    "original_title", "year", "media_type", "show_type", "series_group",
    "card_type", "belongs_to_series", "relation_type", "group_type",
    "season_number", "episode_number", "special_number", "title",
    "confidence", "needs_review", "availability",
)


def _hash_value(value: Any) -> str:
    return "" if value is None else str(value)


def items_hash(items: list[dict]) -> str:
    """按条目规范字段排序计算 revision hash（语义变化才产生新 revision）。

    仅包含识别/分类语义字段；镜像定位（target_*）、人工审计（warnings/
    reasons/user_override_id）变化不产生新 revision。
    """
    lines = []
    for item in sorted(items, key=lambda entry: str(entry.get("relative_path") or "")):
        lines.append(
            "\t".join(_hash_value(item.get(field)) for field in _HASH_FIELDS)
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _item_to_dict(item: Any, original: dict | None = None) -> dict:
    """ImportPlanItem dataclass → revision item dict（patch 后重算 hash 用）。

    original 为对应 SQLite 行（可选）：dataclass 未建模的历史遗留列
    （logical_locator / override_json）从原行保留，避免全列 UPDATE 时被清空。
    """
    result = {
        "id": str(getattr(item, "id", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "provider_id": str(getattr(item, "provider_id", "") or ""),
        "relative_path": str(getattr(item, "relative_path", "") or ""),
        "real_path": str(getattr(item, "real_path", "") or ""),
        "logical_locator": str(getattr(item, "logical_locator", "") or ""),
        "resource_type": str(getattr(item, "resource_type", "") or "other"),
        "action": str(getattr(item, "action", "") or "ignore"),
        "work_id": str(getattr(item, "work_id", "") or ""),
        "work_title": str(getattr(item, "work_title", "") or ""),
        "original_title": str(getattr(item, "original_title", "") or ""),
        "year": getattr(item, "year", None),
        "media_type": str(getattr(item, "media_type", "") or ""),
        "show_type": str(getattr(item, "show_type", "") or ""),
        "series_group": str(getattr(item, "series_group", "") or ""),
        "card_type": str(getattr(item, "card_type", "") or ""),
        "belongs_to_series": str(getattr(item, "belongs_to_series", "") or ""),
        "relation_type": str(getattr(item, "relation_type", "") or ""),
        "group_type": str(getattr(item, "group_type", "") or ""),
        "season_number": getattr(item, "season_number", None),
        "episode_number": getattr(item, "episode_number", None),
        "special_number": getattr(item, "special_number", None),
        "title": str(getattr(item, "title", "") or ""),
        "target_dir": str(getattr(item, "target_dir", "") or ""),
        "target_strm_path": str(getattr(item, "target_strm_path", "") or ""),
        "confidence": str(getattr(item, "confidence", "") or "medium"),
        "needs_review": bool(getattr(item, "needs_review", False)),
        "availability": str(getattr(item, "availability", "") or "available"),
        "warnings": list(getattr(item, "warnings", []) or []),
        "reasons": list(getattr(item, "reasons", []) or []),
        "user_override_id": str(getattr(item, "user_override_id", "") or ""),
        "override": {},
    }
    if original is not None:
        # dataclass 未建模的历史遗留列：从原行保留，不做任何改写
        result["logical_locator"] = original.get("logical_locator") or ""
        try:
            result["override"] = json.loads(original.get("override_json") or "{}")
        except (TypeError, ValueError):
            result["override"] = {}
    return result


def _item_row_values(revision_id: str, item: dict) -> tuple:
    """revision item dict → 数据库行值（与 REVISION_ITEM_COLUMNS 对齐）。"""
    return (
        revision_id,
        str(item.get("item_id") or item.get("id") or uuid.uuid4().hex),
        str(item.get("source") or ""),
        str(item.get("provider_id") or ""),
        str(item.get("relative_path") or ""),
        str(item.get("real_path") or ""),
        str(item.get("logical_locator") or ""),
        str(item.get("resource_type") or "other"),
        str(item.get("action") or "ignore"),
        str(item.get("work_id") or ""),
        str(item.get("work_title") or ""),
        str(item.get("original_title") or ""),
        item.get("year"),
        str(item.get("media_type") or ""),
        str(item.get("show_type") or ""),
        str(item.get("series_group") or ""),
        str(item.get("card_type") or ""),
        str(item.get("belongs_to_series") or ""),
        str(item.get("relation_type") or ""),
        str(item.get("group_type") or ""),
        item.get("season_number"),
        item.get("episode_number"),
        item.get("special_number"),
        str(item.get("title") or ""),
        str(item.get("target_dir") or ""),
        str(item.get("target_strm_path") or ""),
        str(item.get("confidence") or "medium"),
        int(bool(item.get("needs_review"))),
        json.dumps(item.get("override") or {}, ensure_ascii=False),
        json.dumps(list(item.get("warnings") or []), ensure_ascii=False),
        json.dumps(list(item.get("reasons") or []), ensure_ascii=False),
        str(item.get("user_override_id") or ""),
        str(item.get("availability") or "available"),
    )


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
            columns_sql = ", ".join(("revision_id", *REVISION_ITEM_COLUMNS))
            placeholders = ", ".join("?" for _ in range(len(REVISION_ITEM_COLUMNS) + 1))
            tx.execute(
                f"INSERT INTO import_revision_items ({columns_sql}) VALUES ({placeholders})",
                _item_row_values(revision_id, item),
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
                original_title=item.get("original_title") or "",
                year=item.get("year"),
                media_type=item.get("media_type") or "",
                show_type=item.get("show_type") or "",
                series_group=item["series_group"],
                card_type=item.get("card_type") or "",
                belongs_to_series=item.get("belongs_to_series") or "",
                relation_type=item.get("relation_type") or "",
                group_type=item["group_type"],
                season_number=item["season_number"],
                episode_number=item["episode_number"],
                special_number=item.get("special_number"),
                title=item["title"],
                target_dir=item["target_dir"],
                target_strm_path=item["target_strm_path"],
                confidence=item["confidence"],
                needs_review=bool(item["needs_review"]),
                availability=item["availability"],
                warnings=_load_json_list(item.get("warnings_json")),
                reasons=_load_json_list(item.get("reasons_json")),
                user_override_id=item.get("user_override_id") or None,
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


def _load_json_list(value: Any) -> list:
    """宽容解析 JSON 列表列（None/空字符串 → []）。"""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


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


def patch_draft_revision_item(revision_id: str, item_id: str, patch: dict) -> dict:
    """对 draft revision 的条目做人工语义修正（V3 SQLite 唯一路径）。

    - revision 不存在 → ValueError；
    - revision 不是 draft（confirmed/executed/superseded/failed）→
      RevisionStatusError（409 语义）；
    - 复用 patch_plan_item 的纯 patch/validation 规则
      （白名单/值约束/normalize/归位派生/归位质检，见 service.apply_patch_rules）；
    - 事务内更新 import_revision_items 全语义列、重算 semantic_hash、
      刷新 import_revisions.updated_at；
    - 不回写旧 JSON save_import_plan（V3 以 SQLite 为唯一事实）。

    返回重新装载的 revision（含 items）。
    """
    from app.import_plan.service import apply_patch_rules

    revision = load_revision(revision_id)
    if revision is None:
        raise ValueError(f"revision 不存在: {revision_id}")
    if revision["status"] != "draft":
        raise RevisionStatusError(
            f"revision 状态为 {revision['status']}，仅 draft 可人工修正"
        )
    # optimistic fence 基准：事务外读取时的 updated_at（load plan 前捕获）
    expected_updated_at = revision["updated_at"]

    plan = load_plan(revision_id)
    if plan is None:
        raise ValueError(f"无法装载 revision: {revision_id}")

    item, error_msg = apply_patch_rules(plan, item_id, patch)
    if error_msg is not None:
        raise ValueError(error_msg)
    if item is None:
        raise ValueError(f"未找到 item_id={item_id}")

    raw_items = {row["item_id"]: row for row in (revision.get("items") or [])}
    items = [
        _item_to_dict(entry, raw_items.get(entry.id))
        for entry in plan.items
    ]
    new_hash = items_hash(items)
    timestamp = now_iso()
    conn = get_connection()
    set_sql = ", ".join(f"{col} = ?" for col in _ITEM_UPDATE_COLUMNS)
    with transaction(conn) as tx:
        # optimistic fence：BEGIN IMMEDIATE 已独占写锁，事务内重查确认仍为
        # draft 且未被并发请求修改（防 PATCH/confirm TOCTOU 与双 PATCH lost-update）
        fresh = tx.execute(
            "SELECT status, updated_at FROM import_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if fresh is None:
            raise ValueError(f"revision 不存在: {revision_id}")
        if fresh["status"] != "draft":
            raise RevisionStatusError(
                f"revision 状态为 {fresh['status']}，仅 draft 可人工修正"
            )
        if fresh["updated_at"] != expected_updated_at:
            raise RevisionStatusError("revision 已被其他请求修改，请刷新后重试")
        for entry in items:
            row = _item_row_values(revision_id, entry)
            # row = (revision_id, item_id, *values)
            tx.execute(
                f"UPDATE import_revision_items SET {set_sql} "
                "WHERE revision_id = ? AND item_id = ?",
                (*row[2:], revision_id, entry["id"]),
            )
        tx.execute(
            "UPDATE import_revisions SET hash = ?, updated_at = ? WHERE revision_id = ?",
            (new_hash, timestamp, revision_id),
        )
    return load_revision(revision_id)  # type: ignore[return-value]


def confirm_revision_state(
    revision_id: str, *, method: str, expected_updated_at: str | None = None
) -> dict:
    """唯一 SQLite 确认状态转换（同一事务内原子执行，V3 唯一 confirm 入口）。

    - 仅 draft 可确认；已 confirmed/executed → transitioned=False（幂等，
      不重复转换，也不误 supersede 其他 revision）；
    - expected_updated_at（optimistic fence，由调用方传入其验证所用版本）：
      draft 但 updated_at 已变化 → RevisionStatusError（不能用旧 preview/
      旧验证结果确认新语义，调用方须重新加载重新验证）；
    - 同一 unit 内旧 confirmed/executed revision（parent_revision_id 链与
      media_units.current_revision_id 指向的旧值）→ status='superseded'；
      绝不 supersede 其他 unit / 无关 revision（查询都带 unit_id 约束）；
    - 当前 revision → status='confirmed', confirm_method=method,
      confirmed_at=now, updated_at=now；
    - media_unit → status='confirmed', current_revision_id=revision_id,
      updated_at=now。

    返回:
        {
            "transitioned": bool,
            "revision_id": str,
            "unit_id": str,
            "status": str,
            "confirm_method": str,
            "confirmed_at": str,
            "superseded": list[str],
        }
    """
    conn = get_connection()
    timestamp = now_iso()
    with transaction(conn) as tx:
        row = tx.execute(
            "SELECT * FROM import_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"revision 不存在: {revision_id}")
        revision = dict(row)
        unit = tx.execute(
            "SELECT * FROM media_units WHERE unit_id = ?", (revision["unit_id"],)
        ).fetchone()
        if unit is None:
            raise ValueError(f"media_unit 不存在: {revision['unit_id']}")

        if revision["status"] != "draft":
            # 幂等：不重复转换（已 confirmed/executed 重复 confirm）
            return {
                "transitioned": False,
                "revision_id": revision_id,
                "unit_id": revision["unit_id"],
                "status": revision["status"],
                "confirm_method": revision.get("confirm_method") or "",
                "confirmed_at": revision.get("confirmed_at") or "",
                "superseded": [],
            }

        # optimistic fence：确认前 updated_at 不得变化（防止用旧 preview/
        # 旧验证结果确认已被并发 PATCH 修改的新语义）
        if (
            expected_updated_at is not None
            and revision["updated_at"] != expected_updated_at
        ):
            raise RevisionStatusError("revision 已被其他请求修改，请重新加载后确认")

        superseded: list[str] = []

        # 1) media_units.current_revision_id 指向的旧值（同一 unit 内）
        old_current = unit["current_revision_id"] or ""
        if old_current and old_current != revision_id:
            old_row = tx.execute(
                "SELECT status FROM import_revisions "
                "WHERE revision_id = ? AND unit_id = ?",
                (old_current, revision["unit_id"]),
            ).fetchone()
            if old_row and old_row["status"] in ("confirmed", "executed"):
                tx.execute(
                    "UPDATE import_revisions SET status = 'superseded', updated_at = ? "
                    "WHERE revision_id = ? AND unit_id = ? "
                    "AND status IN ('confirmed', 'executed')",
                    (timestamp, old_current, revision["unit_id"]),
                )
                superseded.append(old_current)

        # 2) parent_revision_id 链上仍未 superseded 的 confirmed/executed
        #    （沿链回溯，每一步都限定同一 unit_id，不误伤其他 unit）
        cursor = revision.get("parent_revision_id") or ""
        visited: set[str] = set()
        while cursor and cursor not in visited and cursor != revision_id:
            visited.add(cursor)
            parent_row = tx.execute(
                "SELECT status, parent_revision_id FROM import_revisions "
                "WHERE revision_id = ? AND unit_id = ?",
                (cursor, revision["unit_id"]),
            ).fetchone()
            if parent_row is None:
                break
            if parent_row["status"] in ("confirmed", "executed"):
                tx.execute(
                    "UPDATE import_revisions SET status = 'superseded', updated_at = ? "
                    "WHERE revision_id = ? AND status IN ('confirmed', 'executed')",
                    (timestamp, cursor),
                )
                superseded.append(cursor)
            cursor = parent_row["parent_revision_id"] or ""

        # 3) 当前 revision → confirmed
        tx.execute(
            """
            UPDATE import_revisions
            SET status = 'confirmed', confirm_method = ?, confirmed_at = ?, updated_at = ?
            WHERE revision_id = ? AND status = 'draft'
            """,
            (method, timestamp, timestamp, revision_id),
        )
        # 4) media_unit 切换指针
        tx.execute(
            """
            UPDATE media_units
            SET status = 'confirmed', current_revision_id = ?, updated_at = ?
            WHERE unit_id = ?
            """,
            (revision_id, timestamp, revision["unit_id"]),
        )

    return {
        "transitioned": True,
        "revision_id": revision_id,
        "unit_id": revision["unit_id"],
        "status": "confirmed",
        "confirm_method": method,
        "confirmed_at": timestamp,
        "superseded": superseded,
    }


def confirm_revision_manually(revision_id: str, force: bool = False) -> dict:
    """V3 人工确认：共享验证 → SQLite 唯一确认事务。

    - 复用 service.validate_confirmation（与 legacy confirm_plan 同一套规则，
      不写第三套 validation）；
    - 验证失败 → RevisionStatusError（400/409 语义）；
    - 成功 → confirm_revision_state(method='manual')，返回含 transitioned。
    """
    revision = load_revision(revision_id)
    if revision is None:
        raise RevisionStatusError(f"revision 不存在: {revision_id}")
    plan = load_plan(revision_id)
    if plan is None:
        raise RevisionStatusError(f"无法装载 revision: {revision_id}")

    from app.import_plan.service import validate_confirmation

    ok, _preview, error = validate_confirmation(plan, force=force)
    if not ok:
        raise RevisionStatusError(error or "确认校验未通过")
    # optimistic fence：验证基于的 updated_at 在确认事务内不得变化
    return confirm_revision_state(
        revision_id, method="manual", expected_updated_at=revision["updated_at"]
    )


def try_auto_confirm_revision(revision_id: str) -> tuple[bool, str]:
    """以与人工确认相同的安全规则确认 SQLite revision。

    这是渐进发现器唯一允许自动进入镜像的门槛：只读 SQLite revision，
    不写回旧 JSON ImportPlan；发现需要复核或错误时保留 draft，并把单元标为
    ``needs_review``。通过 auto gate 后调用唯一确认事务
    ``confirm_revision_state(method='auto')``（修复缺 confirmed_at /
    旧 current 未 supersede 的问题）。返回 ``(是否确认, 原因)``，供调用方
    决定是否入队。
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

    result = confirm_revision_state(
        revision_id, method="auto", expected_updated_at=revision["updated_at"]
    )
    return result["transitioned"], ""


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


def is_current_revision(revision_id: str) -> bool:
    """当前语义事实门禁：revision 必须同时满足——

    - 真实存在；
    - status ∈ confirmed/executed；
    - revision.unit_id 与对应 media_unit 一致；
    - media_units.current_revision_id == revision_id。

    superseded/draft/悬空指针一律 False。durable mirror/scrape handler 在任何
    外部或文件副作用之前用它拦截 stale job（no-op 正常结束，不标业务失败）。
    """
    if not revision_id:
        return False
    row = get_connection().execute(
        """
        SELECT 1 FROM media_units u
        JOIN import_revisions r
          ON r.revision_id = u.current_revision_id
         AND r.unit_id = u.unit_id
        WHERE r.revision_id = ?
          AND r.status IN ('confirmed', 'executed')
        """,
        (revision_id,),
    ).fetchone()
    return row is not None


def list_current_revisions(source: str = "") -> list[dict]:
    """当前语义事实：``media_units.current_revision_id`` 指向的 confirmed/executed revision。

    - 绝不按 ``created_at DESC`` 猜最新，绝不用其他 confirmed revision 顶上；
    - fail closed：current 指针为空 / 指向不存在 revision / ``revision.unit_id``
      与 unit 不一致 / revision 仍是 draft —— 都直接跳过（JOIN 与状态过滤天然排除），
      不修复事实、不 fallback。
    """
    conn = get_connection()
    sql = """
        SELECT r.* FROM media_units u
        JOIN import_revisions r
          ON r.revision_id = u.current_revision_id
         AND r.unit_id = u.unit_id
        WHERE u.current_revision_id != ''
          AND r.status IN ('confirmed', 'executed')
    """
    params: list = []
    if source:
        sql += " AND r.source = ?"
        params.append(source)
    sql += " ORDER BY u.root_id, u.boundary"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def list_current_plans(source: str = "") -> list[Any]:
    """当前语义事实的 ImportPlan 视图（镜像/刮削/媒体库投影统一入口）。"""
    plans: list[Any] = []
    for revision in list_current_revisions(source):
        plan = load_plan(revision["revision_id"])
        if plan is not None:
            plans.append(plan)
    return plans


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
