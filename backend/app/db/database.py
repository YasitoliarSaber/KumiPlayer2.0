"""SQLite 数据库初始化和连接管理（后端数据流 V2）。

存储路径：data/kumiplayer.db

新架构（V2，schema v3）：
- ``app_meta.backend_data_epoch = 2`` 标识后端数据架构代次；
- Source Catalog / Import Revision / jobs / scrape 状态全部落在 SQLite；
- 连接统一启用 ``foreign_keys=ON``、WAL、``synchronous=NORMAL``、
  ``busy_timeout=5000``；
- schema 创建在单事务内完成，失败后 ``user_version`` 不得前进；
- v1/v2 旧库（含旧 OpenList schema）拒绝打开，要求一次性重置
  （``reset_backend_v2``），不编写旧业务数据迁移。
"""

import sqlite3
import threading
from pathlib import Path

# 数据库路径
_db_path: Path | None = None
_local = threading.local()
CURRENT_SCHEMA_VERSION = 3
BACKEND_DATA_EPOCH = "2"


class ResetRequiredError(RuntimeError):
    """旧版本数据库需要一次性重置后才能继续（不执行半迁移）。"""


def get_db_path() -> Path:
    """获取数据库路径"""
    global _db_path
    if _db_path is None:
        from app.core.paths import get_data_dir
        _db_path = get_data_dir() / "kumiplayer.db"
    return _db_path


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")


def get_connection() -> sqlite3.Connection:
    """获取线程本地数据库连接（统一 PRAGMA）。"""
    if not hasattr(_local, "connection") or _local.connection is None:
        db_path = get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _local.connection = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
        )
        _local.connection.row_factory = sqlite3.Row
        _apply_connection_pragmas(_local.connection)
    return _local.connection


def _has_any_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return bool(row and row[0] > 0)


def init_db() -> None:
    """初始化 V2 架构数据库。

    - schema_version > 3：拒绝（高版本程序）；
    - schema_version < 3 且存在旧表：抛 ResetRequiredError（要求一次性重置）；
    - 空库：单事务内创建全部表/索引并写入 backend_data_epoch=2，
      DDL 中途失败则回滚，user_version 不前进。
    """
    conn = get_connection()
    cursor = conn.cursor()
    schema_version = int(cursor.execute("PRAGMA user_version").fetchone()[0])
    if schema_version > CURRENT_SCHEMA_VERSION:
        close_connection()
        raise RuntimeError(
            f"数据库版本 {schema_version} 高于当前程序支持的 {CURRENT_SCHEMA_VERSION}，请升级 KumiPlayer"
        )

    if schema_version < CURRENT_SCHEMA_VERSION:
        if _has_any_tables(conn):
            close_connection()
            raise ResetRequiredError(
                f"数据库版本 {schema_version} 属于旧后端数据架构，需要一次性重置后才能继续；"
                "请先备份并确认，再运行重置脚本（默认预览）"
            )
        # 空库：单事务建表，失败不前进版本
        from app.db.schema_v3 import create_schema_v3

        conn.execute("BEGIN IMMEDIATE")
        try:
            create_schema_v3(conn)
            cursor.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # 轻量列迁移（不 bump user_version）：import_revision_items 语义载荷。
    # revision 需保留 card_type（镜像目录命名按 series_group 聚合，OpenList
    # 系列多季合并一张卡）以及人工修正语义字段（original_title/year/…/user_override_id）；
    # 旧 v3 库不重新执行整份 create_schema_v3()，因此逐个 ALTER 幂等补齐，
    # 不要求用户重置数据库。
    _REVISION_ITEM_EXTRA_COLUMNS = (
        ("card_type", "TEXT NOT NULL DEFAULT ''"),
        ("original_title", "TEXT NOT NULL DEFAULT ''"),
        ("year", "INTEGER"),
        ("media_type", "TEXT NOT NULL DEFAULT ''"),
        ("show_type", "TEXT NOT NULL DEFAULT ''"),
        ("belongs_to_series", "TEXT NOT NULL DEFAULT ''"),
        ("relation_type", "TEXT NOT NULL DEFAULT ''"),
        ("special_number", "INTEGER"),
        ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("reasons_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("user_override_id", "TEXT NOT NULL DEFAULT ''"),
    )
    cols = [row[1] for row in conn.execute("PRAGMA table_info(import_revision_items)").fetchall()]
    if cols:
        for col_name, col_ddl in _REVISION_ITEM_EXTRA_COLUMNS:
            if col_name not in cols:
                conn.execute(
                    f"ALTER TABLE import_revision_items ADD COLUMN {col_name} {col_ddl}"
                )
        conn.commit()

    # 幂等轻量扩展（不 bump user_version）：来源级风控健康表。
    # 已有 v3 数据库不会重新执行整份 create_schema_v3()，因此这里单独
    # 用 CREATE TABLE IF NOT EXISTS 补齐，不强制用户重置数据库。
    from app.db.schema_v3 import ensure_source_health_table

    ensure_source_health_table(conn)

    # 旧 v3 库升级：source_stage_runs（run→root 归属）补建后，历史遗留的
    # source_stage_entries 没有归属，无法按来源根清理。分页暂存属于可重建数据，
    # 因此升级时安全清除这类无归属的暂存条目；同时清理 root 已不存在的孤儿映射。
    # 旧库可能缺少这些表（尚未建对应 schema），因此逐条容错。
    try:
        conn.execute(
            "DELETE FROM source_stage_entries WHERE run_id NOT IN (SELECT run_id FROM source_stage_runs)"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "DELETE FROM source_stage_runs WHERE root_id NOT IN (SELECT root_id FROM source_roots)"
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()

    # 幂等轻量扩展（不 bump user_version）：scrape_bindings 的完整语义载荷。
    # ScrapeMapItem 全量字段存入 metadata_json，供 LibraryIndex 投影还原；
    # 已有 v3 库逐个 ALTER 补齐，不要求用户重置数据库。
    binding_cols = [row[1] for row in conn.execute("PRAGMA table_info(scrape_bindings)").fetchall()]
    if binding_cols and "metadata_json" not in binding_cols:
        conn.execute(
            "ALTER TABLE scrape_bindings ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
        conn.commit()

    conn.commit()
    close_connection()


def close_connection() -> None:
    """关闭线程本地连接"""
    if hasattr(_local, "connection") and _local.connection is not None:
        _local.connection.close()
        _local.connection = None
