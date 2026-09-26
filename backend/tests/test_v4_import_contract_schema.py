"""CHECK-001: real old DDL, atomic migration and fresh import contracts."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.schema_v4 import V4_SCHEMA_VERSION, create_schema_v4

FIXTURE = Path(__file__).parent / 'fixtures/v4_schema_v21_import_contract.sql'
NEW_TABLES = {'source_files', 'source_file_observations', 'metadata_snapshots',
              'revision_metadata_refs', 'artifact_references'}


def old_database(tmp_path):
    database = V4Database(tmp_path / 'v21.db')
    with database.connect() as conn:
        # iterdump emits data before guards, exactly as the frozen old database.
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.executescript(FIXTURE.read_text(encoding='utf-8'))
        conn.execute('PRAGMA foreign_keys=ON')
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 21
        assert conn.execute('PRAGMA table_info(seasons)').fetchall()[2]['notnull'] == 1
    return database


def snapshot(conn, columns=None):
    if columns is None:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        columns = {t: [r['name'] for r in conn.execute(f'PRAGMA table_info({t})')] for t in tables}
    rows = {t: sorted([tuple(r) for r in conn.execute(f'SELECT {",".join(cs)} FROM {t}')], key=repr)
            for t, cs in columns.items()}
    return columns, rows


def test_fresh_creation_is_complete_without_external_helpers(tmp_path):
    """CHECK-001A: sole creation entry, null scopes, guards, FK, idempotency."""
    database = V4Database(tmp_path / 'fresh.db')
    with database.connect() as conn:
        create_schema_v4(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert NEW_TABLES <= tables
        database._validate_physical_schema(conn)
        conn.execute("INSERT INTO works(work_id,identity_key,work_type,created_at,updated_at) VALUES ('w','local:w','unknown','now','now')")
        for scope in ('a', 'b'):
            conn.execute("INSERT INTO seasons(season_id,work_id,local_season_number,season_kind,identity_key) VALUES (?,'w',NULL,'unassigned',?)", (scope, scope))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO seasons(season_id,work_id,local_season_number,season_kind,identity_key) VALUES ('zero','w',0,'regular','zero')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE seasons SET local_season_number=1 WHERE season_id='a'")
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        database._set_user_version(conn)
        before = snapshot(conn)
    database.initialize()
    with database.connect() as conn:
        assert snapshot(conn) == before


def test_real_v21_rows_and_identifiers_survive_upgrade(tmp_path):
    """CHECK-001B: no semantic backfill or loss, including legacy regular/0."""
    database = old_database(tmp_path)
    with database.connect() as conn:
        columns, before = snapshot(conn)
    database.initialize()
    with database.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == V4_SCHEMA_VERSION
        assert V4_SCHEMA_VERSION > 21
        assert snapshot(conn, columns)[1] == before
        assert conn.execute('SELECT identity_key FROM episodes').fetchall()[0][0] == ''
        assert conn.execute('SELECT numbering_json FROM parsed_facts').fetchall()[0][0] == '{}'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        database._validate_physical_schema(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE revision_bindings SET resolved_json='{}' WHERE binding_id='b1'")


@pytest.mark.parametrize('starting_version', [19, 21, 22])
def test_upgrade_validation_failure_rolls_back_ddl_version_and_data(tmp_path, monkeypatch, starting_version):
    """CHECK-001B: fail after all DDL, before commit; retry old structure."""
    database = old_database(tmp_path)
    with database.connect() as conn:
        if starting_version == 22:
            from app.media_v4.persistence.schema_v4 import create_v22_structures
            conn.execute('PRAGMA foreign_keys=OFF')
            create_v22_structures(conn)
        elif starting_version == 19:
            conn.execute('DROP TABLE provider_bindings')
            conn.execute('CREATE TABLE provider_bindings (work_id TEXT NOT NULL REFERENCES works(work_id) ON DELETE CASCADE, provider TEXT NOT NULL, media_type TEXT NOT NULL, provider_id TEXT NOT NULL, PRIMARY KEY(provider,media_type,provider_id), UNIQUE(work_id,provider,media_type))')
            conn.execute("INSERT INTO provider_bindings VALUES ('w1','tmdb','tv','123')")
            for name in ('idx_v4_bindings_work', 'idx_v4_artifacts_work', 'idx_v4_jobs_revision'):
                conn.execute(f'DROP INDEX {name}')
        conn.execute(f'PRAGMA user_version={starting_version}')
        before = snapshot(conn)
        schema_before = list(conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
    original = V4Database._validate_physical_schema

    def fail_validation(self, conn):
        assert 'source_files' in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        raise RuntimeError('injected validation failure')

    monkeypatch.setattr(V4Database, '_validate_physical_schema', fail_validation)
    with pytest.raises(RuntimeError, match='injected validation failure'):
        database.initialize()
    with database.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == starting_version
        assert snapshot(conn) == before
        assert list(conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name')) == schema_before
    monkeypatch.setattr(V4Database, '_validate_physical_schema', original)
    database.initialize()


def test_high_version_is_rejected_without_mutation(tmp_path):
    database = old_database(tmp_path)
    with database.connect() as conn:
        conn.execute('PRAGMA user_version=999')
        before = snapshot(conn)
    with pytest.raises(RuntimeError, match='999'):
        database.initialize()
    with database.connect() as conn:
        assert snapshot(conn) == before


def test_schema_validation_detects_changed_partial_identity_index(tmp_path):
    from app.media_v4.persistence.database import V4ResetRequiredError
    database = V4Database(tmp_path / 'index.db')
    database.initialize()
    with database.connect() as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='uq_v4_episode_local_identity'").fetchone()[0]
        conn.execute('DROP INDEX uq_v4_episode_local_identity')
        conn.execute(sql.replace("identity_key = ''", "identity_key != ''"))
    with pytest.raises(V4ResetRequiredError, match='索引'):
        database.initialize()
