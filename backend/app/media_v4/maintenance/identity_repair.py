"""V4 作品身份恢复：预览、事务应用与幂等继续。

恢复只移动关系，不改写 ``SourceEvidence`` / ``ParsedFacts``。旧 confirmed
revision 保留为审计快照并标记 superseded，新的 confirmed revision 按 Asset
重新建立 Work / Season / Episode 关系，因此同号剧集不会再共享一个逻辑
Episode。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.media_v4.persistence.database import V4Database

_INDEPENDENT_RELATIONS = frozenset({
    "spin_off",
    "movie",
    "remake",
    "prequel",
    "sequel",
})


class IdentityRepairBlockedError(RuntimeError):
    """身份恢复缺少唯一映射，或旧任务仍在运行。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_title(value: str) -> str:
    text = (value or "").casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" ._-·:：/\\()（）【】[]{}<>《》「」『』\"'")


def _is_independent(card_type: str, relation_type: str, media_type: str) -> bool:
    return (
        media_type == "movie"
        or card_type in {"standalone", "movie"}
        or relation_type in _INDEPENDENT_RELATIONS
    )


def _target_key(facts: sqlite3.Row) -> tuple[str, str, str, str]:
    media_type = str(facts["media_type"] or "tv").casefold()
    independent = _is_independent(
        str(facts["card_type"] or ""),
        str(facts["relation_type"] or ""),
        media_type,
    )
    title = str(facts["work_title"] or facts["original_title"] or "").strip()
    normalized_title = _normalize_title(title)
    if not normalized_title:
        normalized_title = _normalize_title(str(facts["series_group"] or ""))
    if not normalized_title:
        raise IdentityRepairBlockedError("存在没有作品标题的 Asset，无法安全恢复")
    if independent:
        key = f"work:{normalized_title}:{media_type}"
        card_type = "standalone" if media_type != "movie" else "movie"
        return key, title or normalized_title, media_type, card_type
    series_title = _normalize_title(str(facts["series_group"] or "")) or normalized_title
    key = f"series:{series_title}:{media_type}"
    return key, title or series_title, media_type, "main_series"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: dict) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _latest_scrape_metadata(conn: sqlite3.Connection, work_id: str, provider: str, provider_id: str) -> dict:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM scrape_bindings
        WHERE work_id = ? AND provider = ? AND provider_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (work_id, provider, provider_id),
    ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(str(row["metadata_json"] or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _collect_preview(conn: sqlite3.Connection, work_id: str) -> dict:
    work = conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
    if work is None:
        raise KeyError(work_id)

    revision_rows = conn.execute(
        """
        SELECT DISTINCT ir.revision_id, ir.root_id, ir.scan_id, ir.status,
               ir.confirmed_at, ir.created_at
        FROM import_revisions ir
        JOIN revision_bindings rb ON rb.revision_id = ir.revision_id
        WHERE rb.work_id = ? AND ir.status = 'confirmed'
        ORDER BY ir.confirmed_at DESC, ir.created_at DESC
        """,
        (work_id,),
    ).fetchall()
    if not revision_rows:
        raise IdentityRepairBlockedError("没有可恢复的 confirmed revision")

    asset_rows = conn.execute(
        """
        SELECT rb.asset_id, rb.evidence_id, rb.episode_id, rb.season_id,
               rb.revision_id, ir.root_id, se.relative_path,
               pf.media_type, pf.work_title, pf.original_title, pf.series_group,
               pf.card_type, pf.relation_type,
               s.local_season_number, e.local_episode_number,
               e.absolute_episode_number, e.special_number, e.episode_kind,
               e.display_title
        FROM revision_bindings rb
        JOIN import_revisions ir ON ir.revision_id = rb.revision_id
        JOIN source_evidence se ON se.evidence_id = rb.evidence_id
        JOIN parsed_facts pf ON pf.evidence_id = rb.evidence_id
        LEFT JOIN episodes e ON e.episode_id = rb.episode_id
        LEFT JOIN seasons s ON s.season_id = rb.season_id
        WHERE rb.work_id = ? AND rb.asset_id IS NOT NULL
          AND ir.status IN ('confirmed', 'superseded')
        ORDER BY CASE WHEN ir.status = 'confirmed' THEN 0 ELSE 1 END,
                 ir.confirmed_at DESC, rb.asset_id
        """,
        (work_id,),
    ).fetchall()
    by_asset: dict[str, sqlite3.Row] = {}
    for row in asset_rows:
        by_asset.setdefault(str(row["asset_id"]), row)
    if not by_asset:
        raise IdentityRepairBlockedError("旧 Work 没有可按 Asset 恢复的媒体关系")

    items: list[dict] = []
    target_works: dict[str, dict] = {}
    blocked_reasons: list[dict[str, str]] = []
    for asset_id, row in by_asset.items():
        try:
            key, title, media_type, card_type = _target_key(row)
        except IdentityRepairBlockedError as exc:
            blocked_reasons.append({"asset_id": asset_id, "reason_code": "work_identity_missing", "reason": str(exc)})
            continue
        target_works.setdefault(
            key,
            {
                "work_key": key,
                "title": title,
                "media_type": media_type,
                "work_type": "movie" if media_type == "movie" else "series",
                "card_type": card_type,
                "relation_type": "movie" if media_type == "movie" else (
                    "spin_off" if card_type == "standalone" else "main"
                ),
            },
        )
        items.append(
            {
                "asset_id": asset_id,
                "evidence_id": str(row["evidence_id"]),
                "relative_path": str(row["relative_path"] or ""),
                "root_id": str(row["root_id"]),
                "revision_id": str(row["revision_id"]),
                "old_episode_id": str(row["episode_id"] or ""),
                "old_season_id": str(row["season_id"] or ""),
                "target_work_key": key,
                "local_season_number": row["local_season_number"],
                "season_kind": "special" if int(row["local_season_number"] or 0) == 0 else "regular",
                "local_episode_number": row["local_episode_number"],
                "absolute_episode_number": row["absolute_episode_number"],
                "special_number": row["special_number"],
                "episode_kind": str(row["episode_kind"] or "regular"),
                "display_title": str(row["display_title"] or ""),
            }
        )

    provider_rows = conn.execute(
        "SELECT provider, media_type, provider_id FROM provider_bindings WHERE work_id = ? "
        "ORDER BY provider, media_type, provider_id",
        (work_id,),
    ).fetchall()
    provider_keys = {
        (str(row["provider"]), str(row["media_type"]), str(row["provider_id"]))
        for row in provider_rows
    }
    # provider_bindings 只保存当前唯一身份；旧 revision 里仍可能保留一个
    # 更准确的历史 Provider（例如主系列曾经的 TMDB ID）。把这些身份也纳入
    # 预览，避免恢复时静默丢掉可复用的资料快照。
    historical_provider_rows = conn.execute(
        """
        SELECT provider, provider_id
        FROM scrape_bindings
        WHERE work_id = ? AND provider != 'local' AND provider_id != ''
        GROUP BY provider, provider_id
        ORDER BY provider, provider_id
        """,
        (work_id,),
    ).fetchall()
    inferred_media_type = "tv" if str(work["work_type"] or "") == "series" else "movie"
    provider_rows = list(provider_rows)
    for row in historical_provider_rows:
        provider_key = (str(row["provider"]), inferred_media_type, str(row["provider_id"]))
        if provider_key not in provider_keys:
            provider_rows.append({
                "provider": provider_key[0],
                "media_type": provider_key[1],
                "provider_id": provider_key[2],
            })
    provider_assignments: list[dict] = []
    target_values = list(target_works.values())
    for row in provider_rows:
        provider = str(row["provider"])
        provider_id = str(row["provider_id"])
        metadata = _latest_scrape_metadata(conn, work_id, provider, provider_id)
        metadata_title = _normalize_title(str(metadata.get("title") or metadata.get("original_title") or ""))
        matches = [
            target["work_key"]
            for target in target_values
            if metadata_title and metadata_title in {
                _normalize_title(str(target["title"])),
                # 仅去掉固定前缀与类型后缀，名称内部的冒号不是字段边界。
                _normalize_title(str(target["work_key"]).partition(":")[2].rsplit(":", 1)[0]),
            }
        ]
        if not matches:
            # 复用识别阶段已有的核验路径身份。它是按具体文件路径给出的
            # 高置信证据，能把本地英文目录与 Provider 的中文/日文标题对齐，
            # 但只有唯一目标时才允许自动归属。
            from app.recognition.verified_titles import match_verified_tmdb_binding

            verified_scores = {
                target["work_key"]: sum(
                    1
                    for item in items
                    if item["target_work_key"] == target["work_key"]
                    and (binding := match_verified_tmdb_binding(item["relative_path"])) is not None
                    and str(binding.tmdb_id) == provider_id
                    and binding.tmdb_type == str(row["media_type"])
                )
                for target in target_values
            }
            ranked_verified = sorted(
                verified_scores.items(), key=lambda value: (-value[1], value[0])
            )
            if ranked_verified and ranked_verified[0][1] > 0 and (
                len(ranked_verified) == 1 or ranked_verified[0][1] > ranked_verified[1][1]
            ):
                matches = [ranked_verified[0][0]]
        if not matches:
            mapped_episode_ids = {
                str(mapping.get("episode_id") or "")
                for mapping in metadata.get("episode_mappings") or []
                if isinstance(mapping, dict) and str(mapping.get("episode_id") or "")
            }
            if mapped_episode_ids:
                scores = {
                    target["work_key"]: sum(
                        1
                        for item in items
                        if item["target_work_key"] == target["work_key"]
                        and item["old_episode_id"] in mapped_episode_ids
                    )
                    for target in target_values
                }
                ranked = sorted(scores.items(), key=lambda value: (-value[1], value[0]))
                if ranked and ranked[0][1] > 0 and (
                    len(ranked) == 1 or ranked[0][1] > ranked[1][1]
                ):
                    matches = [ranked[0][0]]
        if not matches and provider == "local":
            matches = [target["work_key"] for target in target_values if target["media_type"] == "tv"][:1]
        if not matches and len(target_values) == 1:
            matches = [target_values[0]["work_key"]]
        if len(matches) != 1:
            blocked_reasons.append({
                "provider": provider,
                "provider_id": provider_id,
                "reason_code": "provider_identity_ambiguous",
                "reason": "Provider 身份无法唯一归属到恢复后的作品",
            })
            continue
        provider_assignments.append(
            {
                "provider": provider,
                "media_type": str(row["media_type"]),
                "provider_id": provider_id,
                "target_work_key": matches[0],
                "metadata": metadata,
            }
        )

    roots = sorted({item["root_id"] for item in items})
    active_revisions = [
        {
            "revision_id": str(row["revision_id"]),
            "root_id": str(row["root_id"]),
            "scan_id": str(row["scan_id"]),
        }
        for row in revision_rows
    ]
    snapshot = {
        "work_id": work_id,
        "work_identity_key": str(work["identity_key"]),
        "active_revisions": active_revisions,
        "roots": roots,
        "target_works": sorted(target_works.values(), key=lambda value: value["work_key"]),
        "items": sorted(items, key=lambda value: value["asset_id"]),
        "provider_assignments": sorted(
            provider_assignments,
            key=lambda value: (value["provider"], value["media_type"], value["provider_id"]),
        ),
        "blocked_reasons": blocked_reasons,
    }
    return {
        "work_id": work_id,
        "old_work": {
            "work_id": work_id,
            "identity_key": str(work["identity_key"]),
            "title": str(work["preferred_title"] or ""),
        },
        **snapshot,
        "blocked": bool(blocked_reasons),
        "digest": _digest(snapshot),
    }


def build_identity_repair_preview(database: V4Database, *, work_id: str) -> dict:
    """只读生成并持久化一份身份恢复预览。"""

    operation_id = "identity-repair-" + uuid.uuid4().hex
    now = _now()
    expires_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
    with database.connect() as conn:
        preview = _collect_preview(conn, work_id)
        preview.update({
            "preview_id": operation_id,
            "created_at": now,
            "expires_at": expires_at,
        })
        conn.execute(
            """
            INSERT INTO maintenance_operations(
                operation_id, scope_provider, status, preview_json, result_json,
                digest, root_ids_json, mirror_root_identity, expires_at, created_at, updated_at
            ) VALUES (?, 'identity_repair', 'preview', ?, '{}', ?, ?, '', ?, ?, ?)
            """,
            (
                operation_id,
                _canonical_json(preview),
                preview["digest"],
                json.dumps(preview["roots"], ensure_ascii=False),
                expires_at,
                now,
                now,
            ),
        )
    return preview


def _load_operation(conn: sqlite3.Connection, operation_id: str) -> tuple[str, dict, str]:
    row = conn.execute(
        "SELECT status, preview_json, result_json, digest FROM maintenance_operations "
        "WHERE operation_id = ? AND scope_provider = 'identity_repair'",
        (operation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(operation_id)
    try:
        preview = json.loads(str(row["preview_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise IdentityRepairBlockedError("身份恢复预览已损坏") from exc
    return str(row["status"]), preview, str(row["result_json"] or "{}")


def _ensure_work(conn: sqlite3.Connection, target: dict, old_work_id: str, now: str) -> str:
    row = conn.execute(
        "SELECT work_id FROM works WHERE identity_key = ? AND status = 'active' AND work_id != ?",
        (target["work_key"], old_work_id),
    ).fetchone()
    if row is not None:
        return str(row["work_id"])
    work_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO works(
            work_id, identity_key, work_type, preferred_title, year, status,
            created_at, updated_at, show_type, card_type
        ) VALUES (?, ?, ?, ?, NULL, 'active', ?, ?, '', ?)
        """,
        (work_id, target["work_key"], target["work_type"], target["title"], now, now, target["card_type"]),
    )
    alias = _normalize_title(str(target["title"]))
    if alias:
        conn.execute(
            "INSERT OR IGNORE INTO work_aliases(work_id, normalized_title, alias_type) VALUES (?, ?, 'repair')",
            (work_id, alias),
        )
    return work_id


def _ensure_season(conn: sqlite3.Connection, work_id: str, season_number: int, season_kind: str) -> str:
    row = conn.execute(
        "SELECT season_id FROM seasons WHERE work_id = ? AND local_season_number = ? AND season_kind = ?",
        (work_id, season_number, season_kind),
    ).fetchone()
    if row is not None:
        return str(row["season_id"])
    season_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO seasons(season_id, work_id, local_season_number, season_kind) VALUES (?, ?, ?, ?)",
        (season_id, work_id, season_number, season_kind),
    )
    return season_id


def _ensure_episode(conn: sqlite3.Connection, item: dict, work_id: str, season_id: str) -> str:
    number = item["local_episode_number"]
    special = item["special_number"]
    row = conn.execute(
        """
        SELECT episode_id FROM episodes
        WHERE work_id = ? AND season_id = ?
          AND ((local_episode_number IS NULL AND ? IS NULL) OR local_episode_number = ?)
          AND ((special_number IS NULL AND ? IS NULL) OR special_number = ?)
          AND episode_kind = ?
        """,
        (work_id, season_id, number, number, special, special, item["episode_kind"]),
    ).fetchone()
    if row is not None:
        return str(row["episode_id"])
    episode_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO episodes(
            episode_id, work_id, season_id, local_episode_number,
            absolute_episode_number, special_number, episode_kind, display_title
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            episode_id,
            work_id,
            season_id,
            number,
            item["absolute_episode_number"],
            special,
            item["episode_kind"],
            item["display_title"],
        ),
    )
    return episode_id


def _cancel_or_block_old_jobs(conn: sqlite3.Connection, work_id: str) -> None:
    active = conn.execute(
        "SELECT job_id FROM jobs WHERE work_id = ? AND status IN ('queued', 'running')",
        (work_id,),
    ).fetchall()
    if active:
        raise IdentityRepairBlockedError("旧 Work 仍有运行中的任务，暂不能应用身份恢复")


def _insert_job(conn: sqlite3.Connection, job_type: str, revision_id: str, work_id: str, now: str) -> None:
    job_id = str(uuid.uuid4())
    key = f"{job_type}:{revision_id}:{work_id}"
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs(
            job_id, job_type, revision_id, work_id, idempotency_key,
            status, attempts, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'queued', 0, '', ?, ?)
        """,
        (job_id, job_type, revision_id, work_id, key, now, now),
    )


def _apply_transaction(conn: sqlite3.Connection, preview: dict, operation_id: str) -> dict:
    old_work_id = str(preview["work_id"])
    now = _now()
    _cancel_or_block_old_jobs(conn, old_work_id)

    conn.execute(
        "UPDATE works SET identity_key = ?, status = 'superseded', updated_at = ? WHERE work_id = ?",
        (f"legacy:{old_work_id}", now, old_work_id),
    )
    target_ids: dict[str, str] = {}
    for target in preview["target_works"]:
        target_ids[target["work_key"]] = _ensure_work(conn, target, old_work_id, now)

    season_ids: dict[tuple[str, int, str], str] = {}
    episode_ids: dict[tuple[str, int, str, Any, Any, str], str] = {}
    item_by_asset: dict[str, dict] = {}
    target_roots: dict[str, set[str]] = {}
    for item in preview["items"]:
        target_work_id = target_ids[item["target_work_key"]]
        target_roots.setdefault(item["target_work_key"], set()).add(str(item["root_id"]))
        season_number = int(item["local_season_number"] or 0)
        season_kind = str(item["season_kind"])
        season_key = (target_work_id, season_number, season_kind)
        season_ids.setdefault(
            season_key,
            _ensure_season(conn, target_work_id, season_number, season_kind),
        )
        episode_key = (
            item["target_work_key"],
            season_number,
            season_kind,
            item["local_episode_number"],
            item["special_number"],
            item["episode_kind"],
        )
        episode_ids.setdefault(
            episode_key,
            _ensure_episode(conn, item, target_work_id, season_ids[season_key]),
        )
        item_copy = dict(item)
        item_copy["new_work_id"] = target_work_id
        item_copy["new_season_id"] = season_ids[season_key]
        item_copy["new_episode_id"] = episode_ids[episode_key]
        item_by_asset[item["asset_id"]] = item_copy
        conn.execute(
            "INSERT OR IGNORE INTO episode_assets(episode_id, asset_id, role, preference_rank) VALUES (?, ?, 'source', 0)",
            (item_copy["new_episode_id"], item["asset_id"]),
        )

    # 将修复后的规范身份写回来源结构索引。这样后续导入会复用新 Work，
    # 同时不会继续把旧 Work 的跨边界历史绑定当成当前事实。
    for work_key, work_id in target_ids.items():
        for root_id in sorted(target_roots.get(work_key, set())):
            conn.execute(
                """
                INSERT OR IGNORE INTO work_source_bindings(
                    work_id, root_id, structural_key, confidence, binding_source
                ) VALUES (?, ?, ?, 'high', 'identity_repair')
                """,
                (work_id, root_id, work_key),
            )

    for assignment in preview["provider_assignments"]:
        provider = assignment["provider"]
        provider_id = assignment["provider_id"]
        target_work_id = target_ids[assignment["target_work_key"]]
        if provider == "local":
            provider_id = target_work_id
        existing_binding = conn.execute(
            "SELECT provider_id FROM provider_bindings "
            "WHERE work_id = ? AND provider = ? AND media_type = ?",
            (target_work_id, provider, assignment["media_type"]),
        ).fetchone()
        # 目标 Work 可能是媒体库里已经存在的正确作品。身份修复只负责迁移资产，
        # 不能用待修复 Work 携带的污染身份覆盖它已经确认的 Provider 槽位。
        if existing_binding is not None:
            continue
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) VALUES (?, ?, ?, ?)",
            (target_work_id, provider, assignment["media_type"], provider_id),
        )

    new_revision_ids: list[str] = []
    for active in preview["active_revisions"]:
        old_revision_id = active["revision_id"]
        new_revision_id = f"identity-repair-{operation_id}-{active['root_id'][-8:]}"
        new_revision_id = new_revision_id[:180]
        conn.execute(
            "UPDATE import_revisions SET status = 'superseded' WHERE revision_id = ? AND status = 'confirmed'",
            (old_revision_id,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO import_revisions(
                revision_id, root_id, scan_id, resolver_version, status,
                graph_digest, created_at, confirmed_at
            ) VALUES (?, ?, ?, 'v4-identity-repair', 'confirmed', ?, ?, ?)
            """,
            (new_revision_id, active["root_id"], active["scan_id"], preview["digest"], now, now),
        )
        evidence_rows = conn.execute(
            "SELECT evidence_id, parsed_fact_id FROM revision_evidence WHERE revision_id = ?",
            (old_revision_id,),
        ).fetchall()
        for row in evidence_rows:
            conn.execute(
                "INSERT OR IGNORE INTO revision_evidence(revision_id, evidence_id, parsed_fact_id) VALUES (?, ?, ?)",
                (new_revision_id, row["evidence_id"], row["parsed_fact_id"]),
            )
        binding_rows = conn.execute(
            "SELECT * FROM revision_bindings WHERE revision_id = ?",
            (old_revision_id,),
        ).fetchall()
        for row in binding_rows:
            mapped = item_by_asset.get(str(row["asset_id"] or "")) if str(row["work_id"]) == old_work_id else None
            if str(row["work_id"]) == old_work_id and mapped is None:
                raise IdentityRepairBlockedError("存在无法按 Asset 映射的旧 revision 关系")
            binding_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO revision_bindings(
                    binding_id, revision_id, evidence_id, work_id, season_id,
                    episode_id, edition_id, asset_id, confidence, decision_source,
                    reasons_json, override_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    new_revision_id,
                    row["evidence_id"],
                    mapped["new_work_id"] if mapped else row["work_id"],
                    mapped["new_season_id"] if mapped else row["season_id"],
                    mapped["new_episode_id"] if mapped else row["episode_id"],
                    None if mapped else row["edition_id"],
                    row["asset_id"],
                    row["confidence"],
                    "identity_repair" if mapped else row["decision_source"],
                    _canonical_json({"previous": json.loads(row["reasons_json"] or "[]"), "repair": True})
                    if mapped else row["reasons_json"],
                    row["override_json"],
                ),
            )
        scrape_rows = conn.execute(
            "SELECT * FROM scrape_bindings WHERE revision_id = ?",
            (old_revision_id,),
        ).fetchall()
        for row in scrape_rows:
            if str(row["work_id"]) == old_work_id:
                target = next(
                    (
                        item for item in preview["provider_assignments"]
                        if item["provider"] == row["provider"]
                        and item["provider_id"] == row["provider_id"]
                    ),
                    None,
                )
                if target is None:
                    continue
                new_work_id = target_ids[target["target_work_key"]]
            else:
                new_work_id = str(row["work_id"])
            conn.execute(
                """
                INSERT OR IGNORE INTO scrape_bindings(
                    binding_id, revision_id, work_id, provider, provider_id,
                    metadata_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), new_revision_id, new_work_id, row["provider"],
                    row["provider_id"], row["metadata_json"], row["status"], now, now,
                ),
            )
        for target_work_id in target_ids.values():
            _insert_job(conn, "materialize_mirror", new_revision_id, target_work_id, now)
            if any(
                assignment["target_work_key"] in target_ids
                and assignment["target_work_key"] in target_ids
                and assignment["provider"] != "local"
                and target_ids[assignment["target_work_key"]] == target_work_id
                for assignment in preview["provider_assignments"]
            ):
                _insert_job(conn, "scrape_work", new_revision_id, target_work_id, now)
        _insert_job(conn, "refresh_projection", new_revision_id, "", now)
        new_revision_ids.append(new_revision_id)

    # 播放进度以 episode + asset 为键迁移；旧事件保留，补一条带新关系的审计事件。
    migrated_progress = 0
    for item in item_by_asset.values():
        rows = conn.execute(
            "SELECT * FROM playback_progress WHERE episode_id = ? AND asset_id = ?",
            (item["old_episode_id"], item["asset_id"]),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO playback_progress(
                    episode_id, asset_id, work_id, position, duration, completed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id, asset_id) DO UPDATE SET
                    work_id = excluded.work_id,
                    position = excluded.position,
                    duration = excluded.duration,
                    completed = excluded.completed,
                    updated_at = excluded.updated_at
                WHERE excluded.updated_at >= playback_progress.updated_at
                """,
                (
                    item["new_episode_id"], item["asset_id"], item["new_work_id"],
                    row["position"], row["duration"], row["completed"], row["updated_at"],
                ),
            )
            migrated_progress += 1
    for row in conn.execute("SELECT * FROM playback_history WHERE work_id = ?", (old_work_id,)).fetchall():
        item = item_by_asset.get(str(row["asset_id"] or ""))
        if item is None:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO playback_history(
                event_id, work_id, episode_id, asset_id, played_at,
                title_snapshot, season_snapshot, episode_snapshot, source_provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{row['event_id']}:repair:{operation_id}", item["new_work_id"],
                item["new_episode_id"], row["asset_id"], row["played_at"],
                row["title_snapshot"], row["season_snapshot"], row["episode_snapshot"],
                row["source_provider"],
            ),
        )

    result = {
        "preview_id": operation_id,
        "status": "completed",
        "old_work_id": old_work_id,
        "target_work_ids": target_ids,
        "new_revision_ids": new_revision_ids,
        "migrated_asset_count": len(item_by_asset),
        "migrated_progress_count": migrated_progress,
    }
    conn.execute(
        "UPDATE maintenance_operations SET status = 'completed', result_json = ?, updated_at = ? WHERE operation_id = ?",
        (_canonical_json(result), now, operation_id),
    )
    return result


def apply_identity_repair(database: V4Database, *, preview_id: str, digest: str) -> dict:
    """校验预览摘要后，在单事务中应用身份恢复。"""

    with database.connect() as conn:
        status, preview, result_json = _load_operation(conn, preview_id)
        if status == "completed":
            return json.loads(result_json or "{}")
        if status != "preview":
            raise IdentityRepairBlockedError(f"身份恢复当前状态不可应用: {status}")
        if preview.get("digest") != digest:
            raise IdentityRepairBlockedError("身份恢复预览已变化，请重新生成预览")
        if preview.get("blocked"):
            raise IdentityRepairBlockedError("身份恢复预览存在阻塞项，不能应用")
        current = _collect_preview(conn, str(preview["work_id"]))
        if current["digest"] != digest:
            raise IdentityRepairBlockedError("数据库内容已变化，请重新生成身份恢复预览")
        conn.execute("BEGIN IMMEDIATE")
        result = _apply_transaction(conn, preview, preview_id)
        conn.commit()
        return result


def resume_identity_repair(database: V4Database, *, operation_id: str) -> dict:
    """恢复未完成的身份操作；已完成操作直接返回原结果。"""

    with database.connect() as conn:
        status, preview, result_json = _load_operation(conn, operation_id)
        if status == "completed":
            return json.loads(result_json or "{}")
        digest = str(preview.get("digest") or "")
    return apply_identity_repair(database, preview_id=operation_id, digest=digest)
