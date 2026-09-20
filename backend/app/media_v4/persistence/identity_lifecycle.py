"""历史证据保留与活动作品身份分离；不迁移、重识别或删除真实媒体。"""

from __future__ import annotations

import sqlite3


def retired_only_work_ids(conn: sqlite3.Connection) -> set[str]:
    """兼容旧清理残留：仅有退役来源，或已由单作品删除明确标记 hidden。"""

    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT w.work_id FROM works w
            JOIN revision_bindings rb ON rb.work_id = w.work_id
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE w.status = 'active' AND sr.retired_at != ''
              AND NOT EXISTS (
                SELECT 1 FROM revision_bindings live
                JOIN import_revisions rev ON rev.revision_id = live.revision_id
                JOIN source_roots root ON root.root_id = rev.root_id
                WHERE live.work_id = w.work_id AND rev.status = 'confirmed'
                  AND root.retired_at = ''
              )
            UNION
            SELECT w.work_id FROM works w
            JOIN work_overrides o ON o.work_id = w.work_id
            WHERE w.status = 'active' AND
              CASE WHEN json_valid(o.override_json)
                THEN json_extract(o.override_json, '$.hidden') = 1 ELSE 0 END
            UNION
            SELECT work_id FROM works WHERE status != 'active'
            """
        ).fetchall()
    }


def retire_work_identity(conn: sqlite3.Connection, work_id: str, now: str) -> None:
    """仅在获准清理/重导入的事务内释放身份；不可变历史关联原样保留。"""

    conn.execute(
        "UPDATE works SET identity_key = ?, status = 'superseded', updated_at = ? WHERE work_id = ?",
        (f"retired:{work_id}", now, work_id),
    )
    for table in ("provider_bindings", "work_aliases", "work_source_bindings"):
        conn.execute(f"DELETE FROM {table} WHERE work_id = ?", (work_id,))


def work_holds_live_slot(conn: sqlite3.Connection, work_id: str) -> bool:
    """该作品是否仍占着媒体库位置（= 仍由**活动来源的已确认导入**提供）。

    这是判断"在线身份是否名花有主"的唯一依据：只有还留在媒体库里的作品才算
    占用者。三个容易漏掉的反例都被覆盖——
    - 来源已退役（``sr.retired_at != ''``）；
    - 作品的导入已被取代（``ir.status = 'superseded'``，例如按来源删除后重导）；
    - 作品本身已退出（``w.status != 'active'``）。
    历史事实（evidence / parsed_facts / bindings）一律保留，这里只回答"还算不算数"。
    """

    row = conn.execute(
        """
        SELECT 1 FROM works w
        WHERE w.work_id = ? AND w.status = 'active'
          AND EXISTS (
            SELECT 1 FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id = w.work_id AND ir.status = 'confirmed' AND sr.retired_at = ''
          )
        LIMIT 1
        """,
        (work_id,),
    ).fetchone()
    return row is not None


def release_inactive_provider_identity(
    conn: sqlite3.Connection,
    provider: str,
    media_type: str,
    provider_id: str,
) -> None:
    """写入新权威绑定前，清除已退出媒体库的旧 owner 槽位。

    阶段 3：解除在线 ID 独占后，同一条在线映射可能同时被多个本地作品引用。
    这里只在**没有任何活动作品共享该映射**时才释放失效槽位，否则会删掉其他作品
    仍然在用的资料引用（修复一部作品不得影响另一部）。
    """

    conn.execute(
        "DELETE FROM provider_bindings "
        "WHERE provider = ? AND media_type = ? AND provider_id = ? "
        "AND EXISTS ("
        "SELECT 1 FROM works w "
        "WHERE w.work_id = provider_bindings.work_id AND w.status != 'active'"
        ") "
        "AND NOT EXISTS ("
        "SELECT 1 FROM provider_bindings live "
        "JOIN works lw ON lw.work_id = live.work_id "
        "WHERE live.provider = provider_bindings.provider "
        "AND live.media_type = provider_bindings.media_type "
        "AND live.provider_id = provider_bindings.provider_id "
        "AND lw.status = 'active'"
        ")",
        (provider, media_type, provider_id),
    )


def release_retired_source_identities(
    conn: sqlite3.Connection,
    root_id: str,
    now: str,
    *,
    work_keys: set[str],
    titles: set[str],
    provider_identities: set[tuple[str, str, str]],
) -> None:
    """确认时仅释放本来源或本次作品命中的已清理/已删除身份，不做全库迁移。"""

    source_ids = {
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT rb.work_id FROM revision_bindings rb "
            "JOIN import_revisions ir ON ir.revision_id = rb.revision_id WHERE ir.root_id = ?",
            (root_id,),
        ).fetchall()
    }
    for work_id in retired_only_work_ids(conn):
        row = conn.execute("SELECT identity_key, preferred_title FROM works WHERE work_id = ?", (work_id,)).fetchone()
        aliases = {str(item[0]).casefold() for item in conn.execute("SELECT normalized_title FROM work_aliases WHERE work_id = ?", (work_id,)).fetchall()}
        aliases.add(str(row[1]).strip().casefold())
        providers = {tuple(str(value) for value in item) for item in conn.execute("SELECT provider,media_type,provider_id FROM provider_bindings WHERE work_id = ?", (work_id,)).fetchall()}
        if work_id in source_ids or str(row[0]) in work_keys or aliases & titles or providers & provider_identities:
            retire_work_identity(conn, work_id, now)
