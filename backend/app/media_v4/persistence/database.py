"""V4 SQLite 连接和严格初始化入口。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.media_v4.persistence.schema_v4 import (
    V4_SCHEMA_VERSION,
    create_schema_v4,
    create_v6_structures,
    create_v8_structures,
    create_v9_structures,
    migrate_schema_v4_to_v5,
    migrate_schema_v5_to_v6,
    migrate_schema_v6_to_v7,
    migrate_schema_v7_to_v8,
    migrate_schema_v8_to_v9,
)


class V4ResetRequiredError(RuntimeError):
    """检测到旧媒体库，V4 只允许一次性重置。"""


class V4Database:
    """轻量 V4 数据库句柄；每次 connect 返回独立连接。"""

    CURRENT_SCHEMA_VERSION = V4_SCHEMA_VERSION
    REQUIRED_TABLES = frozenset(
        {
            "v4_meta",
            "source_roots",
            "source_scans",
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
            "work_relations",
            "revision_work_candidates",
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
        }
    )

    def __init__(self, path: str | Path):
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

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开一个短生命周期连接，并在离开作用域时真正关闭它。"""

        conn = self.open_connection()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            # 保持 sqlite3.Connection 上下文管理器的直觉语义：调用方只
            # 需要 ``with database.connect()`` 就能提交普通写入；显式事务
            # 仍可在块内自行 BEGIN/COMMIT。
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
        """空库创建 V4；旧库不迁移，高版本拒绝打开。"""

        with self.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > self.CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前程序支持的 {self.CURRENT_SCHEMA_VERSION}，请升级 KumiPlayer"
                )
            if version == 8 and self._has_user_tables(conn):
                # v8 → v9 增量迁移：来源退役字段与维护操作表。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v8_to_v9(conn)
                    conn.execute(f"PRAGMA user_version = {self.CURRENT_SCHEMA_VERSION}")
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但物理结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 7 and self._has_user_tables(conn):
                # v7 → v8/v9 增量迁移：来源根级模式字段并回填，随后退役字段与维护表。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v7_to_v8(conn)
                    migrate_schema_v8_to_v9(conn)
                    conn.execute(f"PRAGMA user_version = {self.CURRENT_SCHEMA_VERSION}")
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但物理结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 6 and self._has_user_tables(conn):
                # v6 → v7 增量迁移：候选表增加 original_title / aliases_json。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v6_to_v7(conn)
                    migrate_schema_v7_to_v8(conn)
                    migrate_schema_v8_to_v9(conn)
                    conn.execute(f"PRAGMA user_version = {self.CURRENT_SCHEMA_VERSION}")
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但物理结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 5 and self._has_user_tables(conn):
                # v5 → v6 增量迁移：新增关系/候选表与作品卡片字段。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v5_to_v6(conn)
                    migrate_schema_v6_to_v7(conn)
                    migrate_schema_v7_to_v8(conn)
                    conn.execute(f"PRAGMA user_version = {self.CURRENT_SCHEMA_VERSION}")
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但物理结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 4 and self._has_user_tables(conn):
                # v4 → v5 增量迁移：只新增 tree_scan_validation，保留已确认媒体数据。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v4_to_v5(conn)
                    migrate_schema_v5_to_v6(conn)
                    migrate_schema_v6_to_v7(conn)
                    migrate_schema_v7_to_v8(conn)
                    migrate_schema_v8_to_v9(conn)
                    conn.execute(f"PRAGMA user_version = {self.CURRENT_SCHEMA_VERSION}")
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但物理结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version < self.CURRENT_SCHEMA_VERSION and self._has_user_tables(conn):
                raise V4ResetRequiredError(
                    f"数据库版本 {version} 属于旧后端数据架构，需要一次性重置后才能继续；"
                    "V4 不执行旧媒体数据迁移"
                )
            if version == self.CURRENT_SCHEMA_VERSION and self._has_user_tables(conn):
                self._validate_physical_schema(conn)
                return

            conn.execute("BEGIN IMMEDIATE")
            try:
                create_schema_v4(conn)
                conn.execute(f"PRAGMA user_version = {self.CURRENT_SCHEMA_VERSION}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _validate_physical_schema(self, conn: sqlite3.Connection) -> None:
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
            create_schema_v4(expected)
            create_v6_structures(expected)
            create_v8_structures(expected)
            create_v9_structures(expected)
            expected_signature = self._schema_signature(expected)
        finally:
            expected.close()
        actual_signature = self._schema_signature(conn)
        if actual_signature != expected_signature:
            raise V4ResetRequiredError(
                "数据库声明为 V4，但物理结构与唯一 V4 schema 不一致，需要一次性重置"
            )

    @staticmethod
    def _schema_signature(conn: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
        rows = conn.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger')
              AND name NOT LIKE 'sqlite_%'
              AND sql IS NOT NULL
            ORDER BY type, name
            """
        ).fetchall()
        return tuple((str(row[0]), str(row[1]), " ".join(str(row[2]).split())) for row in rows)
