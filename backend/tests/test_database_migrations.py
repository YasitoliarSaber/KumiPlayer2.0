"""SQLite schema 版本与旧数据库处理（后端数据流 V2）。

v1/v2 旧库不再原地迁移：统一要求一次性重置（reset_backend_v2），
避免旧 OpenList 表与新架构双轨并存。
"""

import sqlite3

import pytest

from app.db.database import CURRENT_SCHEMA_VERSION, ResetRequiredError


def _reset_database_module(database, path, monkeypatch):
    database.close_connection()
    monkeypatch.setattr(database, "_db_path", path)


def test_legacy_unversioned_database_requires_reset(tmp_path, monkeypatch):
    """早期无版本库（有表但 user_version=0）要求一次性重置，不做原地迁移。"""
    from app.db import database

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
    legacy.execute("INSERT INTO tasks(task_id) VALUES ('legacy-task')")
    legacy.commit()
    legacy.close()
    _reset_database_module(database, path, monkeypatch)

    with pytest.raises(ResetRequiredError, match="一次性重置"):
        database.init_db()


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_versioned_database_requires_reset(tmp_path, monkeypatch, version):
    """v1/v2 旧库要求一次性重置，不执行半迁移、不创建旧 OpenList 表。"""
    from app.db import database

    path = tmp_path / f"v{version}.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
    legacy.execute(f"PRAGMA user_version = {version}")
    legacy.commit()
    legacy.close()
    _reset_database_module(database, path, monkeypatch)

    with pytest.raises(ResetRequiredError, match="一次性重置"):
        database.init_db()

    # 旧库未被修改
    untouched = sqlite3.connect(path)
    try:
        assert untouched.execute("PRAGMA user_version").fetchone()[0] == version
        assert {row[0] for row in untouched.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()} == {"tasks"}
    finally:
        untouched.close()


def test_v3_init_is_idempotent(tmp_path, monkeypatch):
    from app.db import database

    path = tmp_path / "v3.db"
    _reset_database_module(database, path, monkeypatch)
    database.init_db()
    database.init_db()  # 幂等
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute(
            "SELECT value FROM app_meta WHERE key = 'backend_data_epoch'"
        ).fetchone()[0] == "2"
    finally:
        connection.close()


def test_database_newer_than_application_is_rejected(tmp_path, monkeypatch):
    from app.db import database

    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {database.CURRENT_SCHEMA_VERSION + 1}")
    connection.close()
    _reset_database_module(database, path, monkeypatch)

    with pytest.raises(RuntimeError, match="数据库版本"):
        database.init_db()


def test_v3_init_creates_source_health_table(tmp_path, monkeypatch):
    """v3 初始化应创建 source_health 表（含 state 索引）。"""
    from app.db import database

    path = tmp_path / "v3_source_health.db"
    _reset_database_module(database, path, monkeypatch)
    database.init_db()

    connection = sqlite3.connect(path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "source_health" in tables
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert "idx_source_health_state" in indexes
    finally:
        connection.close()


def test_source_health_survives_repeated_init(tmp_path, monkeypatch):
    """source_health 幂等：重复 init_db 不抛异常、不重置已写入的数据。"""
    from app.db import database

    path = tmp_path / "v3_source_health_idem.db"
    _reset_database_module(database, path, monkeypatch)
    database.init_db()
    database.init_db()  # 第二次：不抛异常（幂等）

    # 向 source_health 写入一行，再 init_db：数据必须仍在
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO source_health (source_id, state, reason_kind, consecutive_failures, updated_at) "
            "VALUES ('src-idem', 'cooling_down', 'risk_control', 1, 100.0)"
        )
        connection.commit()
    finally:
        connection.close()

    database.init_db()  # 第三次：幂等补齐不应重置数据

    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT source_id, state, reason_kind, consecutive_failures FROM source_health WHERE source_id = 'src-idem'"
        ).fetchone()
        assert row == ("src-idem", "cooling_down", "risk_control", 1)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    finally:
        connection.close()
