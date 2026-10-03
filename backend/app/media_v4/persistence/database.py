"""V4 SQLite 连接和严格初始化入口。"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.media_v4.persistence.schema_v4 import (
    V4_SCHEMA_VERSION,
    create_schema_v4,
    migrate_schema_v4_to_v5,
    migrate_schema_v5_to_v6,
    migrate_schema_v6_to_v7,
    migrate_schema_v7_to_v8,
    migrate_schema_v8_to_v9,
    migrate_schema_v9_to_v10,
    migrate_schema_v10_to_v11,
    migrate_schema_v11_to_v12,
    migrate_schema_v12_to_v13,
    migrate_schema_v13_to_v14,
    migrate_schema_v14_to_v15,
    migrate_schema_v15_to_v16,
    migrate_schema_v16_to_v17,
    migrate_schema_v17_to_v18,
    migrate_schema_v18_to_v19,
    migrate_schema_v19_to_v20,
    migrate_schema_v20_to_v21,
    migrate_schema_v21_to_v22,
    migrate_schema_v22_to_v23,
    migrate_schema_v23_to_v24,
    migrate_schema_v24_to_v25,
)


def _normalize_default(value) -> str:
    """规范化默认值字面量：去空白、统一引号，避免引号写法差异误判。"""

    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("\\'", "'").replace('"', "'")


class V4ResetRequiredError(RuntimeError):
    """检测到旧媒体库，V4 只允许一次性重置。"""


class V4Database:
    """轻量 V4 数据库句柄；每次 connect 返回独立连接。"""

    CURRENT_SCHEMA_VERSION = V4_SCHEMA_VERSION
    # PRAGMA user_version 的值位置不接受绑定参数（SQLite 语法限制），只能在
    # _set_user_version 内写字面量；文件末尾的模块级校验保证它与
    # CURRENT_SCHEMA_VERSION 不漂移。
    REQUIRED_TABLES = frozenset(
        {
            "v4_meta",
            "source_roots",
            "source_scans",
            "source_scan_directories",
            "source_evidence",
            "parsed_facts",
            "works",
            "work_aliases",
            "work_source_bindings",
            "provider_bindings",
            "seasons",
            "season_provider_mappings",
            "episodes",
            "episode_provider_mappings",
            "editions",
            "assets",
            "episode_assets",
            "work_assets",
            "import_revisions",
            "revision_evidence",
            "revision_bindings",
            "revision_issues",
            "revision_overrides",
            "scrape_bindings",
            "jobs",
            "artifacts",
            "library_generations",
            "library_cards",
            "playback_progress",
            "tracking_states",
            "source_health",
            "openlist_telemetry",
            "tree_scan_validation",
            "source_scan_requests",
            "work_relations",
            "revision_work_candidates",
            "maintenance_operations",
            "maintenance_operation_items",
            "work_overrides",
            "playback_history",
            "bangumi_matches",
            "bangumi_episode_sync",
            "source_files",
            "source_file_observations",
            "metadata_snapshots",
            "revision_metadata_refs",
            "artifact_references",
        }
    )
    REQUIRED_TRIGGERS = frozenset(
        {
            "v4_source_evidence_immutable_update",
            "v4_source_evidence_immutable_delete",
            "v4_parsed_facts_immutable_update",
            "v4_parsed_facts_immutable_delete",
            "v4_confirmed_binding_update_guard",
            "v4_confirmed_binding_delete_guard",
            "v4_confirmed_evidence_update_guard",
            "v4_confirmed_evidence_delete_guard",
            "v4_confirmed_override_write_guard",
            "v4_confirmed_override_update_guard",
            "v4_confirmed_override_delete_guard",
            "v4_confirmed_revision_snapshot_guard",
            "v4_confirmed_revision_delete_guard",
            "v4_source_file_observations_update_guard",
            "v4_metadata_snapshot_update_guard",
            "v4_season_identity_kind_guard_insert",
            "v4_season_identity_kind_guard_update",
        }
    )

    def __init__(self, path: str | Path):
        # 最后一道保险：测试进程绝不允许操作真实数据目录（2026-09-17 误覆盖事故）。
        from app.core.paths import assert_test_process_does_not_touch_real_data

        assert_test_process_does_not_touch_real_data(path)
        self.path = Path(path)

    def open_connection(self) -> sqlite3.Connection:
        """打开一个原始连接；需要长期复用的基础设施必须自行关闭。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @staticmethod
    def _set_user_version(conn: sqlite3.Connection) -> None:
        """写入当前 schema 版本号。

        PRAGMA user_version 的值位置不接受绑定参数（SQLite 语法限制），只能
        使用字面量；写回后立即读回校验，版本升级时若字面量未同步会立即失败。
        """

        conn.execute("PRAGMA user_version = 25")
        written = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if written != V4_SCHEMA_VERSION:
            raise RuntimeError(
                f"PRAGMA user_version 写入值 {written} 与 CURRENT_SCHEMA_VERSION 不一致，请同步更新字面量"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开一个短生命周期连接，并在离开作用域时真正关闭它。"""

        conn = self.open_connection()
        try:
            yield conn
            # 保持 sqlite3.Connection 上下文管理器的直觉语义：调用方只需要
            # ``with database.connect()`` 就能提交普通写入；显式事务仍可在
            # 块内自行 BEGIN/COMMIT。
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _has_user_tables(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        return row is not None

    def initialize(self) -> None:
        """逐版迁移和最终校验在同一事务中；失败不提升版本或改写旧事实。"""
        migrations = {
            4: migrate_schema_v4_to_v5,
            5: migrate_schema_v5_to_v6,
            6: migrate_schema_v6_to_v7,
            7: migrate_schema_v7_to_v8,
            8: migrate_schema_v8_to_v9,
            9: migrate_schema_v9_to_v10,
            10: migrate_schema_v10_to_v11,
            11: migrate_schema_v11_to_v12,
            12: migrate_schema_v12_to_v13,
            13: migrate_schema_v13_to_v14,
            14: migrate_schema_v14_to_v15,
            15: migrate_schema_v15_to_v16,
            16: migrate_schema_v16_to_v17,
            17: migrate_schema_v17_to_v18,
            18: migrate_schema_v18_to_v19,
            19: migrate_schema_v19_to_v20,
            20: migrate_schema_v20_to_v21,
            21: migrate_schema_v21_to_v22,
            22: migrate_schema_v22_to_v23,
            23: migrate_schema_v23_to_v24,
            24: migrate_schema_v24_to_v25,
        }
        with self.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > self.CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前程序支持的 {self.CURRENT_SCHEMA_VERSION}，请升级 KumiPlayer"
                )
            populated = self._has_user_tables(conn)
            if populated and version == self.CURRENT_SCHEMA_VERSION:
                self._validate_physical_schema(conn)
                self._reconcile_source_root_activity(conn)
                return
            if populated and version not in migrations:
                raise V4ResetRequiredError(
                    f"数据库版本 {version} 属于旧后端数据架构，V4 不执行旧媒体数据迁移，需要一次性重置"
                )
            # seasons 等值重建需要临时关闭 FK；只在独占初始化连接、事务外设置。
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.execute("BEGIN IMMEDIATE")
                if populated:
                    for source_version in range(version, self.CURRENT_SCHEMA_VERSION):
                        migrations[source_version](conn)
                else:
                    create_schema_v4(conn)
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise V4ResetRequiredError("结构迁移存在外键违约，已回滚；原数据保留")
                self._validate_physical_schema(conn)
                self._set_user_version(conn)
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                raise V4ResetRequiredError("数据库物理结构不完整，迁移已回滚；" + str(exc)) from exc
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _reconcile_source_root_activity(conn: sqlite3.Connection) -> None:
        """修复“先退役、后重新确认”却仍被当成退役来源的旧状态。

        维护清理发生在最后一次确认之后时仍保持退役；只有较新的 confirmed
        revision 能证明用户确实重新建立了该来源。这个不变量也让升级前已经
        完成、但来源卡消失的导入在下次启动时自动恢复。
        """

        cursor = conn.execute(
            """
            UPDATE source_roots AS sr
            SET retired_at = '', retired_reason = ''
            WHERE sr.retired_at != ''
              AND EXISTS (
                  SELECT 1
                  FROM import_revisions ir
                  WHERE ir.root_id = sr.root_id
                    AND ir.status = 'confirmed'
                    AND ir.confirmed_at != ''
                    AND julianday(ir.confirmed_at) > julianday(sr.retired_at)
              )
            """
        )
        if cursor.rowcount > 0:
            conn.execute(
                """
                INSERT INTO v4_meta(key, value)
                VALUES ('library_projection_dirty', datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )

    def _validate_physical_schema(self, conn: sqlite3.Connection) -> None:
        """P-009：用逻辑 schema 合同校验，不逐字比较 DDL 文本。

        显式迁移允许新列按历史顺序追加；空库初始 CREATE 可把同字段置于
        任意定义位置。两者只要列名/类型/默认值/约束、索引、外键与触发器
        合同一致，就视为兼容。真正缺表、缺列、约束不符或缺触发器仍 fail-closed。
        """

        objects: dict[str, set[str]] = {
            row["type"]: set()
            for row in conn.execute(
                "SELECT DISTINCT type FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        }
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        ):
            objects.setdefault(row["type"], set()).add(row["name"])
        missing_tables = self.REQUIRED_TABLES - objects.get("table", set())
        missing_triggers = self.REQUIRED_TRIGGERS - objects.get("trigger", set())
        if missing_tables or missing_triggers:
            details = []
            if missing_tables:
                details.append("缺少表: " + ", ".join(sorted(missing_tables)))
            if missing_triggers:
                details.append("缺少触发器: " + ", ".join(sorted(missing_triggers)))
            raise V4ResetRequiredError(
                "数据库声明为 V4，但物理结构不完整，需要一次性重置；" + "；".join(details)
            )
        expected = sqlite3.connect(":memory:")
        expected.row_factory = sqlite3.Row
        try:
            # 唯一完整建库入口：空库、迁移结果与 expected 三者使用同一函数，
            # 不在 expected 路径多调 helper 掩盖空库遗漏。
            create_schema_v4(expected)
            for table in sorted(self.REQUIRED_TABLES):
                actual_cols = self._table_contract(conn, table)
                expected_cols = self._table_contract(expected, table)
                if actual_cols != expected_cols:
                    missing = expected_cols - actual_cols
                    detail = f"（表 {table}"
                    if missing:
                        detail += " 缺失列/约束: " + ", ".join(sorted(map(str, missing)))[:200]
                    detail += "）"
                    raise V4ResetRequiredError(
                        "数据库声明为 V4，但物理结构与唯一 V4 schema 不一致，需要一次性重置" + detail
                    )
                if self._foreign_key_contract(conn, table) != self._foreign_key_contract(expected, table):
                    raise V4ResetRequiredError(
                        "数据库声明为 V4，但物理结构（外键）与唯一 V4 schema 不一致，需要一次性重置"
                    )
            if self._index_contract(conn, self.REQUIRED_TABLES) != self._index_contract(expected, self.REQUIRED_TABLES):
                raise V4ResetRequiredError(
                    "数据库声明为 V4，但物理结构（索引）与唯一 V4 schema 不一致，需要一次性重置"
                )
            if self._trigger_contract(conn, self.REQUIRED_TRIGGERS) != self._trigger_contract(expected, self.REQUIRED_TRIGGERS):
                raise V4ResetRequiredError(
                    "数据库声明为 V4，但物理结构（触发器）与唯一 V4 schema 不一致，需要一次性重置"
                )
        finally:
            expected.close()

    @staticmethod
    def _table_contract(database: sqlite3.Connection, table: str) -> frozenset[tuple]:
        """表逻辑合同：列名/声明类型/NOT NULL/默认值/主键位序，忽略物理列顺序（cid）。"""

        rows = database.execute("SELECT * FROM pragma_table_info(?)", (table,)).fetchall()
        return frozenset(
            (
                str(row["name"]),
                str(row["type"] or "").upper(),
                int(row["notnull"] or 0),
                _normalize_default(row["dflt_value"]),
                int(row["pk"] or 0),
            )
            for row in rows
        )

    @staticmethod
    def _foreign_key_contract(database: sqlite3.Connection, table: str) -> frozenset[tuple[str, str, str, str, str]]:
        """外键逻辑合同：关联表、列与删除/更新动作，忽略内部编号顺序。"""

        rows = database.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,)).fetchall()
        return frozenset(
            (
                str(row["table"]),
                str(row["from"]),
                str(row["to"]),
                str(row["on_update"]),
                str(row["on_delete"]),
            )
            for row in rows
        )

    @staticmethod
    def _index_contract(
        database: sqlite3.Connection, tables: frozenset[str]
    ) -> tuple[tuple[str, int, tuple[str, ...], str], ...]:
        """受管索引合同：非自动索引的名称、唯一性与列序。"""

        contracts: list[tuple[str, int, tuple[str, ...], str]] = []
        for table in sorted(tables):
            for index in database.execute("SELECT * FROM pragma_index_list(?)", (table,)).fetchall():
                name = str(index["name"])
                if name.startswith("sqlite_autoindex_"):
                    continue
                columns = tuple(
                    str(row["name"])
                    for row in database.execute("SELECT * FROM pragma_index_info(?)", (name,)).fetchall()
                )
                sql = str(database.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0] or '')
                predicate = re.split(r'\bWHERE\b', sql, maxsplit=1, flags=re.IGNORECASE)
                where = ''.join(predicate[1].split()).rstrip(';') if len(predicate) == 2 else ''
                contracts.append((name, int(index["unique"] or 0), columns, where))
        return tuple(sorted(contracts))

    @staticmethod
    def _trigger_contract(
        database: sqlite3.Connection, trigger_names: frozenset[str]
    ) -> frozenset[tuple[str, str]]:
        """触发器合同：名称 + 规范化 DDL。"""

        if not trigger_names:
            return frozenset()
        rows = database.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE type = 'trigger' AND sql IS NOT NULL
              AND name IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(sorted(trigger_names)),),
        ).fetchall()
        return frozenset(
            (str(row["name"]), " ".join(str(row["sql"]).split()))
            for row in rows
        )
