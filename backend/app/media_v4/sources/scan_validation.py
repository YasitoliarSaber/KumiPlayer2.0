"""目录树扫描验证的 SQLite 持久化（v5 tree_scan_validation 表）。

验证事实进入事务约束的 SQLite，不再依赖可丢失的旁路 JSON：缺失、损坏、
过期、根变化或样本当前不可达都会让 confirm 失败关闭（fail-closed）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

TREE_VALIDATION_TTL = timedelta(hours=24)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def upsert_tree_scan_validation(
    database,
    *,
    scan_id: str,
    root_id: str,
    effective_root: str,
    ok: bool,
    hits: int,
    total: int,
    reason: str,
    samples: list[str],
    candidates: list[str],
) -> None:
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO tree_scan_validation(
                scan_id, root_id, effective_root, ok, hits, total, reason,
                samples_json, candidates_json, validated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id) DO UPDATE SET
                root_id = excluded.root_id,
                effective_root = excluded.effective_root,
                ok = excluded.ok,
                hits = excluded.hits,
                total = excluded.total,
                reason = excluded.reason,
                samples_json = excluded.samples_json,
                candidates_json = excluded.candidates_json,
                validated_at = excluded.validated_at
            """,
            (
                scan_id,
                root_id,
                effective_root,
                1 if ok else 0,
                hits,
                total,
                reason,
                json.dumps(samples, ensure_ascii=False),
                json.dumps(candidates, ensure_ascii=False),
                _now_iso(),
            ),
        )


def load_tree_scan_validation(database, scan_id: str) -> dict | None:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT * FROM tree_scan_validation WHERE scan_id = ?", (scan_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "scan_id": row["scan_id"],
        "root_id": row["root_id"],
        "effective_root": row["effective_root"],
        "ok": bool(row["ok"]),
        "hits": int(row["hits"]),
        "total": int(row["total"]),
        "reason": row["reason"],
        "samples": json.loads(row["samples_json"] or "[]"),
        "candidates": json.loads(row["candidates_json"] or "[]"),
        "validated_at": row["validated_at"],
    }


def is_tree_validation_expired(validation: dict) -> bool:
    try:
        validated_at = datetime.fromisoformat(validation.get("validated_at") or "")
    except (ValueError, TypeError):
        return True
    return datetime.now(UTC) - validated_at > TREE_VALIDATION_TTL


def samples_currently_reachable(validation: dict) -> bool:
    """按验证时保存的样本重新检查当前可达性；只读取文件元数据。"""

    root = str(validation.get("effective_root") or "").strip()
    samples = validation.get("samples") or []
    if not root or not samples:
        return False
    for relative in samples:
        target = Path(root)
        for part in PurePosixPath(str(relative)).parts:
            target /= part
        try:
            if not target.is_file():
                return False
        except OSError:
            return False
    return True


def delete_tree_scan_validation(database, scan_id: str) -> None:
    """确认成功后的非权威清理；调用方失败时不得让接口报错。"""

    with database.connect() as conn:
        conn.execute("DELETE FROM tree_scan_validation WHERE scan_id = ?", (scan_id,))
