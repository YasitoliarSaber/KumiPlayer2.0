"""v4 → v5 增量迁移：只新增 tree_scan_validation 表，保留已确认媒体数据。"""

from __future__ import annotations

import sqlite3

import pytest

from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.schema_v4 import (
    V4_SCHEMA_VERSION,
    create_schema_v4,
)


def _build_v4_database(path) -> None:
    """构造一个物理 v4 数据库（v5 结构去掉新表 + user_version=4）。"""

    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        create_schema_v4(conn)
        # 去掉 v5 新增表，得到严格 v4 物理结构。
        conn.execute("DROP TABLE tree_scan_validation")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    finally:
        conn.close()


def test_v4_database_is_migrated_to_v5_without_data_loss(tmp_path):
    db_path = tmp_path / "legacy-v4.db"
    _build_v4_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-keep', 'local', 'local_scan', '2026-08-23T00:00:00+00:00', '2026-08-23T00:00:00+00:00')"
        )
        conn.commit()

    database = V4Database(db_path)
    database.initialize()

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 5
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tree_scan_validation'"
        ).fetchone()
        assert table is not None
        root = conn.execute(
            "SELECT provider FROM source_roots WHERE root_id = 'root-keep'"
        ).fetchone()
        assert root is not None
        assert root[0] == "local"


def test_fresh_database_is_v5_and_has_validation_table(tmp_path):
    database = V4Database(tmp_path / "fresh.db")
    database.initialize()

    with sqlite3.connect(tmp_path / "fresh.db") as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == V4_SCHEMA_VERSION == 5
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tree_scan_validation'"
        ).fetchone()
        assert table is not None


def test_legacy_v3_database_still_requires_reset(tmp_path):
    db_path = tmp_path / "legacy-v3.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE legacy_marker (id INTEGER)")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    from app.media_v4.persistence.database import V4ResetRequiredError

    with pytest.raises(V4ResetRequiredError):
        V4Database(db_path).initialize()


def test_tree_scan_validation_round_trip_and_cleanup(tmp_path):
    database = V4Database(tmp_path / "validation.db")
    database.initialize()
    from app.media_v4.sources.scan_validation import (
        delete_tree_scan_validation,
        load_tree_scan_validation,
        upsert_tree_scan_validation,
    )

    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-v', 'baidu', 'directory_tree', '2026-08-24T00:00:00+00:00', '2026-08-24T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) "
            "VALUES ('scan-v', 'root-v', 1, 'validated')"
        )

    assert load_tree_scan_validation(database, "scan-v") is None
    upsert_tree_scan_validation(
        database,
        scan_id="scan-v",
        root_id="root-v",
        effective_root=str(tmp_path),
        ok=True,
        hits=1,
        total=1,
        reason="ok",
        samples=["Show/Show.S01E01.mkv"],
        candidates=[str(tmp_path)],
    )
    validation = load_tree_scan_validation(database, "scan-v")
    assert validation is not None
    assert validation["ok"] is True
    assert validation["samples"] == ["Show/Show.S01E01.mkv"]
    delete_tree_scan_validation(database, "scan-v")
    assert load_tree_scan_validation(database, "scan-v") is None


def test_validation_helpers_detect_expiry_and_unreachable_samples(tmp_path):
    database = V4Database(tmp_path / "helpers.db")
    database.initialize()
    from app.media_v4.sources.scan_validation import (
        is_tree_validation_expired,
        load_tree_scan_validation,
        samples_currently_reachable,
        upsert_tree_scan_validation,
    )

    media = tmp_path / "Show" / "Show.S01E01.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"video")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-helper', 'baidu', 'directory_tree', '2026-08-24T00:00:00+00:00', '2026-08-24T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) "
            "VALUES ('scan-helper', 'root-helper', 1, 'validated')"
        )
    upsert_tree_scan_validation(
        database,
        scan_id="scan-helper",
        root_id="root-helper",
        effective_root=str(tmp_path),
        ok=True,
        hits=1,
        total=1,
        reason="ok",
        samples=["Show/Show.S01E01.mkv"],
        candidates=[],
    )
    validation = load_tree_scan_validation(database, "scan-helper")
    assert is_tree_validation_expired(validation) is False
    assert samples_currently_reachable(validation) is True

    media.unlink()
    assert samples_currently_reachable(validation) is False
    assert is_tree_validation_expired({"validated_at": "2020-01-01T00:00:00+00:00"}) is True
