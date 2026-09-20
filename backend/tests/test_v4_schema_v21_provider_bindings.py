"""v20 → v21：在线资料可被多部本地作品引用，旧绑定必须无损升级。"""

from __future__ import annotations

import sqlite3

import pytest

from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.schema_v4 import V4_SCHEMA_VERSION

_V20_PROVIDER_BINDINGS = """
CREATE TABLE provider_bindings (
    work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    media_type TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    PRIMARY KEY(provider, media_type, provider_id),
    UNIQUE(work_id, provider, media_type)
)
"""

_V20_INDICES = (
    "idx_v4_bindings_work",
    "idx_v4_artifacts_work",
    "idx_v4_jobs_revision",
)


def _primary_key_columns(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute("PRAGMA table_info(provider_bindings)").fetchall()
    return tuple(str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5]))


def _index_unique(conn: sqlite3.Connection, name: str) -> int | None:
    for row in conn.execute("PRAGMA index_list(provider_bindings)").fetchall():
        if str(row[1]) == name:
            return int(row[2])
    return None


def _binding_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        tuple(str(value) for value in row)
        for row in conn.execute(
            "SELECT work_id, provider, media_type, provider_id FROM provider_bindings "
            "ORDER BY work_id, provider, media_type, provider_id"
        ).fetchall()
    ]


def _seed_work(conn: sqlite3.Connection, work_id: str) -> None:
    conn.execute(
        "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
        "VALUES (?, ?, 'series', ?, 'now', 'now')",
        (work_id, f"local:{work_id}", work_id),
    )


def _make_old_fixture(tmp_path, *, version: int) -> V4Database:
    database = V4Database(tmp_path / f"legacy-v{version}.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute("DROP TABLE provider_bindings")
        conn.execute(_V20_PROVIDER_BINDINGS)
        for work_id in ("work-a", "work-b", "work-c"):
            _seed_work(conn, work_id)
        conn.executemany(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) VALUES (?, 'tmdb', 'tv', ?)",
            (("work-a", "111"), ("work-b", "222")),
        )
        if version == 20:
            for name in _V20_INDICES:
                assert conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
                ).fetchone() is not None
        elif version == 19:
            for name in _V20_INDICES:
                conn.execute(f"DROP INDEX {name}")
        else:
            raise ValueError(version)
        conn.execute(f"PRAGMA user_version = {version}")
    return database


def test_v20_provider_binding_rows_survive_migration_and_can_be_shared(tmp_path):
    database = _make_old_fixture(tmp_path, version=20)
    with database.connect() as conn:
        assert _primary_key_columns(conn) == ("provider", "media_type", "provider_id")
        before = _binding_rows(conn)

    database.initialize()

    with database.connect() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == V4_SCHEMA_VERSION == 21
        assert _primary_key_columns(conn) == ("work_id", "provider", "media_type")
        assert _binding_rows(conn) == before
        assert _index_unique(conn, "idx_v4_provider_bindings_identity") == 0
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
                "VALUES ('work-c', 'tmdb', 'tv', '111')"
        )
        assert _binding_rows(conn) == [
            ("work-a", "tmdb", "tv", "111"),
            ("work-b", "tmdb", "tv", "222"),
            ("work-c", "tmdb", "tv", "111"),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
                "VALUES ('work-a', 'tmdb', 'tv', '333')"
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_real_v19_structure_reaches_v21_after_legacy_chain(tmp_path):
    database = _make_old_fixture(tmp_path, version=19)
    with database.connect() as conn:
        assert _primary_key_columns(conn) == ("provider", "media_type", "provider_id")
        for name in _V20_INDICES:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
            ).fetchone() is None
        before = _binding_rows(conn)

    database.initialize()

    with database.connect() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == V4_SCHEMA_VERSION == 21
        assert _primary_key_columns(conn) == ("work_id", "provider", "media_type")
        assert _binding_rows(conn) == before
        assert _index_unique(conn, "idx_v4_provider_bindings_identity") == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fresh_database_uses_v21_provider_binding_contract(tmp_path):
    database = V4Database(tmp_path / "fresh.db")
    database.initialize()

    with database.connect() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == V4_SCHEMA_VERSION == 21
        assert _primary_key_columns(conn) == ("work_id", "provider", "media_type")
        assert _index_unique(conn, "idx_v4_provider_bindings_identity") == 0
