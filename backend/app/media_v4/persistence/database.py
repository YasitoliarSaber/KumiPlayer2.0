"""V4 SQLite 连接和严格初始化入口。"""

from __future__ import annotations

import json
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
    create_v10_structures,
    create_v13_structures,
    create_v15_structures,
    create_v16_structures,
    create_v17_structures,
    create_v18_structures,
    create_v19_structures,
    create_v20_structures,
    create_v21_structures,
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

        conn.execute("PRAGMA user_version = 21")
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
        """空库创建 V4；旧库不迁移，高版本拒绝打开。"""

        with self.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > self.CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前程序支持的 {self.CURRENT_SCHEMA_VERSION}，请升级 KumiPlayer"
                )
            if version == 13 and self._has_user_tables(conn):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
            if version == 14 and self._has_user_tables(conn):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
            if version == 15 and self._has_user_tables(conn):
                # v15 → v16：仅为既有 jobs outbox 补齐可终止与心跳字段。
                # 这不是旧媒体库迁移，不能因为版本号落后一位而误触发重置。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v15_to_v16(conn)
                    self._set_user_version(conn)
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但任务结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 19 and self._has_user_tables(conn):
                # v19 → v20：补齐查询索引；加法迁移，不改写任何媒体事实。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v19_to_v20(conn)
                    self._set_user_version(conn)
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但索引结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 20 and self._has_user_tables(conn):
                # v20 → v21：在线资料可被多个本地作品共享；每个作品自身
                # 的 provider/type 槽位仍保持唯一。迁移重建的只是绑定表，
                # 不改写媒体事实或已确认 revision。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v20_to_v21(conn)
                    self._set_user_version(conn)
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但在线身份绑定结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 18 and self._has_user_tables(conn):
                # v18 → v19：新增扫描目录 frontier（断点续扫状态）；加法迁移，
                # 不改写任何媒体事实。缺这条分支会让既有 v18 库被判成旧架构重置。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v18_to_v19(conn)
                    self._set_user_version(conn)
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但扫描目录结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 17 and self._has_user_tables(conn):
                # v17 → v18：候选数值证据列；加法迁移，不改写既有候选。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v17_to_v18(conn)
                    self._set_user_version(conn)
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但候选证据结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 16 and self._has_user_tables(conn):
                # v16 → v17：新增 SourceScan 可序列化请求表；加法迁移，
                # 不改写既有媒体、扫描或证据事实。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v16_to_v17(conn)
                    self._set_user_version(conn)
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但扫描请求结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                version = self.CURRENT_SCHEMA_VERSION
            if version == 12 and self._has_user_tables(conn):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
            if version == 11 and self._has_user_tables(conn):
                # v11 → v12 增量迁移：操作明细唯一 Artifact 索引。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
            if version == 10 and self._has_user_tables(conn):
                # v10 → v11 增量迁移：维护操作受管列与逐项明细。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v10_to_v11(conn)
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
            if version == 9 and self._has_user_tables(conn):
                # v9 → v10/v11 增量迁移：播放历史/覆盖表，随后维护操作受管列与明细。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v9_to_v10(conn)
                    migrate_schema_v10_to_v11(conn)
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
            if version == 8 and self._has_user_tables(conn):
                # v8 → v9 增量迁移：来源退役字段与维护操作表。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v8_to_v9(conn)
                    migrate_schema_v9_to_v10(conn)
                    migrate_schema_v10_to_v11(conn)
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
                    migrate_schema_v9_to_v10(conn)
                    migrate_schema_v10_to_v11(conn)
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
                    migrate_schema_v9_to_v10(conn)
                    migrate_schema_v10_to_v11(conn)
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
                # v5 → v10 连续迁移：关系/候选表、来源根级模式、退役字段与 v10 表。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v5_to_v6(conn)
                    migrate_schema_v6_to_v7(conn)
                    migrate_schema_v7_to_v8(conn)
                    migrate_schema_v8_to_v9(conn)
                    migrate_schema_v9_to_v10(conn)
                    migrate_schema_v10_to_v11(conn)
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
                    migrate_schema_v9_to_v10(conn)
                    migrate_schema_v10_to_v11(conn)
                    migrate_schema_v11_to_v12(conn)
                    migrate_schema_v12_to_v13(conn)
                    migrate_schema_v13_to_v14(conn)
                    migrate_schema_v14_to_v15(conn)
                    self._set_user_version(conn)
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
                # v15 各条历史迁移链会先收口到当时的完整结构；v16 仅为
                # outbox 追加可终止/心跳列，迁移幂等且不触碰媒体事实。
                conn.execute("BEGIN IMMEDIATE")
                try:
                    migrate_schema_v15_to_v16(conn)
                    migrate_schema_v16_to_v17(conn)
                    migrate_schema_v17_to_v18(conn)
                    migrate_schema_v18_to_v19(conn)
                    migrate_schema_v19_to_v20(conn)
                    migrate_schema_v20_to_v21(conn)
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    raise V4ResetRequiredError(
                        "数据库声明为 V4 但任务结构不完整，需要一次性重置；" + str(exc)
                    ) from exc
                except Exception:
                    conn.rollback()
                    raise
                self._validate_physical_schema(conn)
                self._reconcile_source_root_activity(conn)
                return

            conn.execute("BEGIN IMMEDIATE")
            try:
                create_schema_v4(conn)
                # 空库创建路径必须补齐"未被 create_schema_v4 收编"的新结构，
                # 否则新装的库会缺表/缺索引，而版本号已经是当前值（B5-2a 与 v20
                # 的索引都踩过这个坑）。
                create_v19_structures(conn)
                create_v20_structures(conn)
                create_v21_structures(conn)
                self._set_user_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

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
            create_schema_v4(expected)
            create_v6_structures(expected)
            create_v8_structures(expected)
            create_v9_structures(expected)
            create_v10_structures(expected)
            create_v13_structures(expected)
            create_v15_structures(expected)
            create_v16_structures(expected)
            create_v17_structures(expected)
            create_v18_structures(expected)
            create_v19_structures(expected)
            create_v20_structures(expected)
            create_v21_structures(expected)
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
    ) -> tuple[tuple[str, int, tuple[str, ...]], ...]:
        """受管索引合同：非自动索引的名称、唯一性与列序。"""

        contracts: list[tuple[str, int, tuple[str, ...]]] = []
        for table in sorted(tables):
            for index in database.execute("SELECT * FROM pragma_index_list(?)", (table,)).fetchall():
                name = str(index["name"])
                if name.startswith("sqlite_autoindex_"):
                    continue
                columns = tuple(
                    str(row["name"])
                    for row in database.execute("SELECT * FROM pragma_index_info(?)", (name,)).fetchall()
                )
                contracts.append((name, int(index["unique"] or 0), columns))
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
