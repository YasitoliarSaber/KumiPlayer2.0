"""后端数据流 V2（schema v3）核心测试。

覆盖：空库建全部表与外键/唯一约束、backend_data_epoch=2、重复初始化幂等、
高版本拒绝、v1/v2 旧库 reset-required、DDL 中途失败不前进版本、
旧 OpenList 表不再创建、旧通用表保留、连接 PRAGMA。
"""

import sqlite3
from pathlib import Path

import pytest

from app.core.config import load_config, save_config
from app.db.database import (
    CURRENT_SCHEMA_VERSION,
    ResetRequiredError,
    close_connection,
    get_connection,
    init_db,
)

EXPECTED_V3_TABLES = {
    "app_meta",
    "sources",
    "source_roots",
    "import_batches",
    "import_batch_roots",
    "scan_runs",
    "source_directories",
    "source_nodes",
    "source_stage_entries",
    "media_units",
    "import_revisions",
    "import_revision_items",
    "media_libraries",
    "jobs",
    "job_attempts",
    "scrape_bindings",
    "scrape_reviews",
    "scrape_failures",
    "artifact_records",
}

#: 旧 OpenList 专用表：不得进入新运行路径
DEPRECATED_OPENLIST_TABLES = {
    "openlist_scan_roots",
    "openlist_scan_units",
    "openlist_scan_directories",
    "openlist_index_entries",
    "openlist_scan_stage_entries",
}

#: 旧通用表：任务 2-5 迁移完成前继续供旧功能使用
LEGACY_TABLES = {
    "tasks",
    "playback_history",
    "scrape_candidate_cache",
    "scrape_review_queue",
    "failed_cases",
    "tracking_bindings",
    "tracking_scan_runs",
    "work_overrides",
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield db_path
    close_connection()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


class TestV2SchemaCreation:
    def test_empty_db_creates_all_tables_and_epoch(self, db):
        conn = get_connection()
        tables = _tables(conn)
        assert EXPECTED_V3_TABLES <= tables
        assert LEGACY_TABLES <= tables
        assert not (DEPRECATED_OPENLIST_TABLES & tables)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute(
            "SELECT value FROM app_meta WHERE key = 'backend_data_epoch'"
        ).fetchone()[0] == "2"

    def test_repeated_init_is_idempotent(self, db):
        init_db()
        init_db()
        conn = get_connection()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION

    def test_foreign_keys_enforced(self, db):
        conn = get_connection()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_roots (root_id, source_id, normalized_locator) VALUES ('r1', 'missing-source', '/动画')"
            )
            conn.commit()

    def test_unique_constraint_enforced(self, db):
        conn = get_connection()
        ts = '2026-08-11T00:00:00+08:00'
        conn.execute(
            "INSERT INTO sources (source_id, source_type, created_at, updated_at) VALUES ('s1', 'openlist', ?, ?)",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO source_roots (root_id, source_id, normalized_locator, created_at, updated_at) VALUES ('r1', 's1', '/动画', ?, ?)",
            (ts, ts),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_roots (root_id, source_id, normalized_locator, created_at, updated_at) VALUES ('r2', 's1', '/动画', ?, ?)",
                (ts, ts),
            )
            conn.commit()

    def test_connection_pragmas_applied(self, db):
        conn = get_connection()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


class TestVersionGates:
    def test_newer_database_rejected(self, tmp_path, monkeypatch):
        path = tmp_path / "future.db"
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        connection.close()
        monkeypatch.setattr("app.db.database._db_path", path)
        with pytest.raises(RuntimeError, match="高于当前程序支持"):
            init_db()

    @pytest.mark.parametrize("version", [1, 2])
    def test_legacy_database_requires_reset(self, tmp_path, monkeypatch, version):
        path = tmp_path / f"v{version}.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
        connection.close()
        monkeypatch.setattr("app.db.database._db_path", path)
        with pytest.raises(ResetRequiredError, match="一次性重置"):
            init_db()

    def test_unversioned_legacy_database_requires_reset(self, tmp_path, monkeypatch):
        """早期无版本库（有表但 user_version=0）同样要求重置。"""
        path = tmp_path / "legacy-no-version.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        monkeypatch.setattr("app.db.database._db_path", path)
        with pytest.raises(ResetRequiredError):
            init_db()


class TestResetBackendState:
    def test_preview_does_not_delete_anything(self, tmp_path, monkeypatch):
        """重置服务默认只预览，不删除任何数据。"""
        data_dir = tmp_path / "data"
        (data_dir / "import_plans").mkdir(parents=True)
        (data_dir / "import_plans" / "p.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))

        from app.maintenance.reset_backend_state import preview_reset

        preview = preview_reset()
        assert preview["targets"]
        assert (data_dir / "import_plans" / "p.json").exists()

    def test_apply_never_targets_source_roots_or_disk_root(self, tmp_path, monkeypatch):
        """apply 时任何目标是来源根/磁盘根/主目录 → 立即中止。"""
        data_dir = tmp_path / "data"
        (data_dir / "import_plans").mkdir(parents=True)
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))

        from app.maintenance.reset_backend_state import (
            ResetProtectionError,
            apply_reset,
        )

        # 数据根被配置为来源根时拒绝
        config = load_config(force_reload=True)
        config.pan115_root = str(data_dir)
        save_config(config)
        with pytest.raises(ResetProtectionError):
            apply_reset()
        assert (data_dir / "import_plans").exists()

        # 磁盘根永远拒绝
        monkeypatch.setattr(
            "app.maintenance.reset_backend_state.get_data_dir",
            lambda: Path(tmp_path.anchor),
        )
        with pytest.raises(ResetProtectionError):
            apply_reset()


class TestAtomicSchemaCreation:
    def test_ddl_failure_does_not_advance_version(self, tmp_path, monkeypatch):
        """DDL 中途失败：user_version 不前进、不残留半成品版本状态。"""
        path = tmp_path / "atomic.db"
        monkeypatch.setattr("app.db.database._db_path", path)
        import app.db.database as db_mod
        if hasattr(db_mod._local, "connection"):
            db_mod._local.connection = None

        from app.db import schema_v3

        original = schema_v3.create_schema_v3

        def exploding_create(conn):
            original(conn)
            raise RuntimeError("模拟 DDL 中途失败")

        monkeypatch.setattr(schema_v3, "create_schema_v3", exploding_create)
        with pytest.raises(RuntimeError, match="模拟 DDL 中途失败"):
            init_db()
        db_mod.close_connection()

        # 版本未前进；表未完整残留（事务回滚）
        raw = sqlite3.connect(path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        finally:
            raw.close()

        # 恢复后重新初始化成功（不残留半成品）
        monkeypatch.setattr(schema_v3, "create_schema_v3", original)
        init_db()
        conn = get_connection()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert _tables(conn) >= EXPECTED_V3_TABLES
        db_mod.close_connection()
