"""P-009：V4 schema 兼容校验合同。

真实数据库经连续 ALTER TABLE 升级后，列物理顺序与空库初始 CREATE 不同；
SQLite 语义按列名读写，因此顺序差异必须视为兼容。校验应使用逻辑合同
（列名/类型/约束/索引/触发器），不得用 raw DDL 文本逐字比较。真正缺表、
缺列、约束不符或缺触发器仍必须 fail-closed。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "compat.db")
    database.initialize()
    return database


def _create_v5_layout(path: Path, *, rows: list[tuple]) -> None:
    """构造真实 v5 布局：source_roots 采用旧列顺序（无 v6/v8/v9 追加列），
    其余表使用当前 schema；随后通过真实 ALTER TABLE 连续追加各版本字段，
    模拟真实升级库的列物理顺序。"""

    from app.media_v4.persistence.schema_v4 import create_schema_v4

    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        create_schema_v4(conn)
        # 用 v5 物理布局重建 source_roots（root_container 等在旧列之后/不存在）
        # 夹具只模拟 source_roots 的历史列顺序；禁止 rename 同步改写子表 FK，
        # 否则会人为制造真实迁移中不存在的外键损坏。
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("ALTER TABLE source_roots RENAME TO source_roots_legacy")
        conn.execute(
            """
            CREATE TABLE source_roots (
                root_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                ingest_method TEXT NOT NULL,
                source_locator TEXT NOT NULL DEFAULT '',
                playback_locator TEXT NOT NULL DEFAULT '',
                route_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, route_id, display_name, enabled, created_at, updated_at) "
            "SELECT root_id, provider, ingest_method, source_locator, playback_locator, route_id, display_name, enabled, created_at, updated_at FROM source_roots_legacy"
        )
        conn.execute("DROP TABLE source_roots_legacy")
        conn.execute("PRAGMA legacy_alter_table = OFF")
        # 连续 ALTER：v6 root_container、v8 source_mode/last_scan_mode、v9 retired 字段
        conn.execute("ALTER TABLE source_roots ADD COLUMN root_container TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE source_roots ADD COLUMN source_mode TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE source_roots ADD COLUMN last_scan_mode TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE source_roots ADD COLUMN retired_at TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE source_roots ADD COLUMN retired_reason TEXT NOT NULL DEFAULT ''")
        conn.execute("PRAGMA user_version = 5")
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'now', 'now')",
                row,
            )
        conn.commit()
    finally:
        conn.close()


def test_v5_layout_with_altered_columns_initializes_and_keeps_data(tmp_path):
    """16.5-1：真实 v5 布局 + ALTER 连续追加 → 初始化成功且已有数据仍在。"""

    from app.media_v4.persistence.database import V4Database

    db_path = tmp_path / "legacy-v5.db"
    _create_v5_layout(db_path, rows=[("root-keep", "pan115", "openlist_scan", "/Anime", "K:\\Anime")])

    database = V4Database(db_path)
    database.initialize()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        root = conn.execute("SELECT * FROM source_roots WHERE root_id = 'root-keep'").fetchone()
        assert root is not None
        assert root["provider"] == "pan115"
        assert root["source_locator"] == "/Anime"
        # v8 迁移按既有 ingest_method 回填来源根模式（openlist_scan → openlist_full）
        assert root["source_mode"] == "openlist_full"
        columns = [str(row["name"]) for row in conn.execute("PRAGMA table_info(source_roots)").fetchall()]
        assert "retired_at" in columns
        assert "last_scan_mode" in columns


def test_column_order_only_difference_is_accepted(tmp_path):
    """16.5-2a：同一组列仅物理顺序不同（列追加到末尾）→ 初始化成功。"""

    from app.media_v4.persistence.database import V4Database

    db_path = tmp_path / "reordered.db"
    _create_v5_layout(db_path, rows=[("root-a", "local", "local_scan", "D:\\Media", "D:\\Media")])
    # 手动标为 v10（模拟已迁移到 v10 的真实库，列顺序为追加式）
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 10")
        conn.commit()

    database = V4Database(db_path)
    database.initialize()

    with sqlite3.connect(db_path) as conn:
        root = conn.execute("SELECT * FROM source_roots WHERE root_id = 'root-a'").fetchone()
        assert root is not None


def test_missing_column_still_requires_reset(tmp_path):
    """16.5-2b：删除必需列 → 仍必须 fail-closed。"""

    from app.media_v4.persistence.database import V4Database, V4ResetRequiredError

    db_path = tmp_path / "missing-col.db"
    _create_v5_layout(db_path, rows=[("root-a", "local", "local_scan", "D:\\Media", "D:\\Media")])
    with sqlite3.connect(db_path) as conn:
        # 构造 v10 但删除一个必需列（SQLite 无法 DROP COLUMN，用重建模拟）
        conn.execute("PRAGMA user_version = 10")
        conn.execute("ALTER TABLE source_roots RENAME TO source_roots_full")
        conn.execute(
            """
            CREATE TABLE source_roots (
                root_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                ingest_method TEXT NOT NULL,
                source_locator TEXT NOT NULL DEFAULT '',
                playback_locator TEXT NOT NULL DEFAULT '',
                route_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                root_container TEXT NOT NULL DEFAULT '',
                last_scan_mode TEXT NOT NULL DEFAULT '',
                retired_at TEXT NOT NULL DEFAULT '',
                retired_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, route_id, display_name, enabled, root_container, last_scan_mode, retired_at, retired_reason, created_at, updated_at) "
            "SELECT root_id, provider, ingest_method, source_locator, playback_locator, route_id, display_name, enabled, root_container, last_scan_mode, retired_at, retired_reason, created_at, updated_at FROM source_roots_full"
        )
        conn.execute("DROP TABLE source_roots_full")
        conn.commit()

    with pytest.raises(V4ResetRequiredError, match="物理结构"):
        V4Database(db_path).initialize()


def test_missing_v10_table_still_requires_reset(tmp_path):
    """16.5-2c：缺少 v10 必需表（如 playback_history）→ fail-closed。"""

    from app.media_v4.persistence.database import V4Database, V4ResetRequiredError

    db_path = tmp_path / "missing-table.db"
    _create_v5_layout(db_path, rows=[("root-a", "local", "local_scan", "D:\\Media", "D:\\Media")])
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 10")
        conn.execute("DROP TABLE playback_history")
        conn.commit()

    with pytest.raises(V4ResetRequiredError, match="缺少表"):
        V4Database(db_path).initialize()


def test_broken_default_still_requires_reset(tmp_path):
    """16.5-2d：默认值/约束改坏 → fail-closed。"""

    from app.media_v4.persistence.database import V4Database, V4ResetRequiredError

    db_path = tmp_path / "bad-default.db"
    _create_v5_layout(db_path, rows=[("root-a", "local", "local_scan", "D:\\Media", "D:\\Media")])
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 10")
        conn.execute("ALTER TABLE source_roots RENAME TO source_roots_full")
        conn.execute(
            """
            CREATE TABLE source_roots (
                root_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                ingest_method TEXT NOT NULL,
                source_locator TEXT NOT NULL DEFAULT '',
                playback_locator TEXT NOT NULL DEFAULT '',
                route_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                root_container TEXT NOT NULL DEFAULT 'BROKEN',
                source_mode TEXT NOT NULL DEFAULT '',
                last_scan_mode TEXT NOT NULL DEFAULT '',
                retired_at TEXT NOT NULL DEFAULT '',
                retired_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, route_id, display_name, enabled, root_container, source_mode, last_scan_mode, retired_at, retired_reason, created_at, updated_at) "
            "SELECT root_id, provider, ingest_method, source_locator, playback_locator, route_id, display_name, enabled, root_container, source_mode, last_scan_mode, retired_at, retired_reason, created_at, updated_at FROM source_roots_full"
        )
        conn.execute("DROP TABLE source_roots_full")
        conn.commit()

    with pytest.raises(V4ResetRequiredError, match="物理结构"):
        V4Database(db_path).initialize()


def test_missing_foreign_key_still_requires_reset(tmp_path):
    """逻辑列合同相同但关联约束丢失时，不能把损坏库当作健康库。"""

    from app.media_v4.persistence.database import V4ResetRequiredError

    database = _fresh_database(tmp_path)
    with database.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE source_scans RENAME TO source_scans_full")
        conn.execute(
            """
            CREATE TABLE source_scans (
                scan_id TEXT PRIMARY KEY,
                root_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                UNIQUE(root_id, generation)
            )
            """
        )
        conn.execute("DROP TABLE source_scans_full")
        conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(V4ResetRequiredError, match="外键"):
        database.initialize()


def test_unmanaged_local_objects_do_not_invalidate_v4_contract(tmp_path):
    """用户或诊断工具的额外对象不属于 V4 schema 合同，不能阻断启动。"""

    database = _fresh_database(tmp_path)
    with database.connect() as conn:
        conn.execute("CREATE TABLE local_diagnostic_note (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
        conn.execute("CREATE INDEX idx_local_diagnostic_note ON local_diagnostic_note(note)")
        conn.execute(
            "CREATE TRIGGER local_diagnostic_note_audit "
            "AFTER INSERT ON local_diagnostic_note BEGIN SELECT 1; END"
        )

    database.initialize()


def test_v5_upgrade_creates_v8_v9_v10_objects(tmp_path):
    """16.5-3：v5 连续升级后 v8/v9/v10 的表与列实际存在，不得只断言版本号。"""

    from app.media_v4.persistence.database import V4Database

    db_path = tmp_path / "v5-upgrade.db"
    _create_v5_layout(db_path, rows=[("root-a", "local", "local_scan", "D:\\Media", "D:\\Media")])
    V4Database(db_path).initialize()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table in ("maintenance_operations", "work_overrides", "playback_history", "revision_work_candidates", "tree_scan_validation"):
            assert table in tables, f"缺少 {table}"
        cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(source_roots)").fetchall()}
        for col in ("source_mode", "last_scan_mode", "retired_at", "retired_reason"):
            assert col in cols, f"缺少列 {col}"


def test_lifespan_and_health_with_temporary_data_dir(tmp_path):
    """16.5-4：临时 KUMIPLAYER_DATA_DIR 下 app.main lifespan + /api/health 成功。"""

    from fastapi.testclient import TestClient

    old = os.environ.get("KUMIPLAYER_DATA_DIR")
    os.environ["KUMIPLAYER_DATA_DIR"] = str(tmp_path / "data")
    try:
        from app.main import app

        with TestClient(app) as client:
            response = client.get("/api/health")
            assert response.status_code == 200, response.text
    finally:
        if old is None:
            os.environ.pop("KUMIPLAYER_DATA_DIR", None)
        else:
            os.environ["KUMIPLAYER_DATA_DIR"] = old


def test_lifespan_with_migrated_layout_database(tmp_path, monkeypatch):
    """16.5-4b：迁移布局数据库（v5→v10 追加式列）同样可完成 lifespan。"""

    from fastapi.testclient import TestClient

    data_dir = tmp_path / "migrated-data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "kumiplayer.db"
    _create_v5_layout(db_path, rows=[("root-a", "local", "local_scan", "D:\\Media", "D:\\Media")])
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    # 重新初始化以执行迁移
    from app.media_v4.persistence.database import V4Database

    V4Database(db_path).initialize()

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200, response.text
