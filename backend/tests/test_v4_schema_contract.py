"""V4 数据库结构合同。

这些测试先定义新数据流的最低持久化边界：空库一次建立 V4，旧媒体库不迁移，
schema 版本与物理结构必须一一对应。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.media_v4.persistence.schema_v4 import V4_SCHEMA_VERSION


def test_empty_database_creates_one_strict_v4_schema(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "kumiplayer.db")
    database.initialize()

    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V4_SCHEMA_VERSION
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    assert {
        "v4_meta",
        "source_roots",
        "source_scans",
        "source_evidence",
        "parsed_facts",
        "works",
        "work_aliases",
        "provider_bindings",
        "seasons",
        "season_provider_mappings",
        "episodes",
        "episode_provider_mappings",
        "editions",
        "assets",
        "episode_assets",
        "import_revisions",
        "revision_evidence",
        "revision_bindings",
        "jobs",
        "artifacts",
        "source_health",
        "openlist_telemetry",
        "tree_scan_validation",
        "bangumi_matches",
        "bangumi_episode_sync",
    } <= tables

    with database.connect() as conn:
        job_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert {"cancel_requested", "heartbeat_at", "started_at", "finished_at"} <= job_columns


def test_existing_old_database_requires_reset_instead_of_implicit_migration(tmp_path):
    from app.media_v4.persistence.database import V4Database, V4ResetRequiredError

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE legacy_media(id TEXT PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 3")

    with pytest.raises(V4ResetRequiredError, match="一次性重置"):
        V4Database(path).initialize()


def test_future_database_version_is_rejected(tmp_path):
    from app.media_v4.persistence.database import V4Database

    # PRAGMA user_version 值位置不接受绑定参数，这里用字面量写「未来版本」；
    # schema 升级时同步更新断言与字面量，防止测试悄悄失去未来语义。
    assert V4_SCHEMA_VERSION == 21
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 22")

    with pytest.raises(RuntimeError, match="高于当前程序支持"):
        V4Database(path).initialize()


def test_v15_database_is_upgraded_without_resetting_media_data(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.schema_v4 import create_schema_v4

    path = tmp_path / "v15-outbox.db"
    with sqlite3.connect(path) as conn:
        create_schema_v4(conn)
        conn.execute("PRAGMA user_version = 15")
        conn.execute(
            "INSERT INTO v4_meta(key, value) VALUES ('preserved', 'yes')"
        )

    database = V4Database(path)
    database.initialize()

    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V4_SCHEMA_VERSION
        assert conn.execute("SELECT value FROM v4_meta WHERE key = 'preserved'").fetchone()[0] == "yes"
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)")}
    assert {"cancel_requested", "heartbeat_at", "started_at", "finished_at"} <= columns


def test_claimed_v4_database_with_incomplete_physical_schema_requires_reset(tmp_path):
    from app.media_v4.persistence.database import V4Database, V4ResetRequiredError

    path = tmp_path / "partial-v4.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE dummy(id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 4")

    with pytest.raises(V4ResetRequiredError, match="物理结构"):
        V4Database(path).initialize()


def test_claimed_v4_database_with_wrong_columns_requires_reset(tmp_path):
    from app.media_v4.persistence.database import V4Database, V4ResetRequiredError
    from app.media_v4.persistence.schema_v4 import create_schema_v4

    path = tmp_path / "wrong-column-v4.db"
    with sqlite3.connect(path) as conn:
        create_schema_v4(conn)
        conn.execute("ALTER TABLE works RENAME TO works_valid")
        conn.execute("CREATE TABLE works(work_id TEXT PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 4")

    with pytest.raises(V4ResetRequiredError, match="物理结构"):
        V4Database(path).initialize()


def test_database_enforces_one_active_confirmed_revision_per_root(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "active-revision.db")
    database.initialize()
    with database.connect() as conn:
        conn.executescript(
            """
            INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at)
            VALUES ('root', 'local', 'local_scan', 'now', 'now');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan-1', 'root', 1, 'completed');
            INSERT INTO source_scans(scan_id, root_id, generation, status)
            VALUES ('scan-2', 'root', 2, 'completed');
            INSERT INTO import_revisions(
                revision_id, root_id, scan_id, resolver_version, status, created_at
            ) VALUES ('rev-1', 'root', 'scan-1', 'fixture', 'confirmed', 'now');
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO import_revisions(
                    revision_id, root_id, scan_id, resolver_version, status, created_at
                ) VALUES ('rev-2', 'root', 'scan-2', 'fixture', 'confirmed', 'now')
                """
            )
