"""V4 数据库结构合同。

这些测试先定义新数据流的最低持久化边界：空库一次建立 V4，旧媒体库不迁移，
schema 版本与物理结构必须一一对应。
"""

from __future__ import annotations

import sqlite3

import pytest


def test_empty_database_creates_one_strict_v4_schema(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "kumiplayer.db")
    database.initialize()

    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
        "revision_bindings",
        "jobs",
        "artifacts",
    } <= tables


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

    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 5")

    with pytest.raises(RuntimeError, match="高于当前程序支持"):
        V4Database(path).initialize()
