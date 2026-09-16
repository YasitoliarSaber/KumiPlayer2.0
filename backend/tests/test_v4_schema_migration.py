"""v4 → v5 增量迁移：只新增 tree_scan_validation 表，保留已确认媒体数据。"""

from __future__ import annotations

import sqlite3

import pytest
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.schema_v4 import (
    V4_SCHEMA_VERSION,
    _add_column_if_missing,
    create_schema_v4,
    create_v6_structures,
)


def _build_v7_database(path) -> None:
    """构造一个物理 v7 数据库（v8 结构去掉 source_mode 列 + user_version=7）。"""

    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        create_schema_v4(conn)
        create_v6_structures(conn)
        _add_column_if_missing(conn, "revision_work_candidates", "original_title")
        _add_column_if_missing(conn, "revision_work_candidates", "aliases_json")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()


def _insert_v7_root(conn: sqlite3.Connection, *, root_id: str, provider: str, ingest_method: str, route_id: str = "") -> None:
    conn.execute(
        "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, route_id, display_name, root_container, created_at, updated_at) "
        "VALUES (?, ?, ?, '', '', ?, '', '', 'now', 'now')",
        (root_id, provider, ingest_method, route_id),
    )


def test_v7_database_is_migrated_to_current_schema_with_source_mode_backfill(tmp_path):
    db_path = tmp_path / "legacy-v7.db"
    _build_v7_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _insert_v7_root(conn, root_id="root-local", provider="local", ingest_method="local_scan")
        _insert_v7_root(
            conn,
            root_id="root-hybrid",
            provider="pan115",
            ingest_method="directory_tree",
            route_id="route-anime",
        )
        _insert_v7_root(conn, root_id="root-tree", provider="baidu", ingest_method="directory_tree")
        _insert_v7_root(
            conn,
            root_id="root-open",
            provider="pan115",
            ingest_method="openlist_scan",
            route_id="route-anime",
        )
        conn.commit()

    database = V4Database(db_path)
    database.initialize()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V4_SCHEMA_VERSION
        rows = {
            str(row["root_id"]): str(row["source_mode"])
            for row in conn.execute("SELECT root_id, source_mode FROM source_roots").fetchall()
        }
    assert rows == {
        "root-local": "local",
        "root-hybrid": "tree_openlist",
        "root-tree": "tree_snapshot",
        "root-open": "openlist_full",
    }


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
        assert version == V4_SCHEMA_VERSION
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
        assert version == V4_SCHEMA_VERSION
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tree_scan_validation'"
        ).fetchone()
        assert table is not None


def test_v14_scan_rows_gain_progress_columns_and_backfill(tmp_path):
    """真实 v14 扫描行升级后可读出阶段、计数和心跳，不丢失已发现事实。"""

    db_path = tmp_path / "legacy-v14-scan.db"
    database = V4Database(db_path)
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-v14-scan', 'local', 'local_scan', '2026-08-30T00:00:00+00:00', '2026-08-30T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-v14', 'root-v14-scan', 1, 'running', '2026-08-30T00:00:01+00:00')"
        )
        conn.execute(
            "INSERT INTO source_evidence(evidence_id, scan_id, root_id, source_key, relative_path, entry_kind) "
            "VALUES ('evidence-v14', 'scan-v14', 'root-v14-scan', 'Show/E01.mkv', 'Show/E01.mkv', 'video')"
        )
        conn.execute(
            "INSERT INTO parsed_facts(parsed_fact_id, evidence_id, parser_version) "
            "VALUES ('facts-v14', 'evidence-v14', 'v14')"
        )
        conn.execute("ALTER TABLE source_scans DROP COLUMN cancel_requested")
        conn.execute("ALTER TABLE source_scans DROP COLUMN heartbeat_at")
        conn.execute("ALTER TABLE source_scans DROP COLUMN total_count")
        conn.execute("ALTER TABLE source_scans DROP COLUMN processed_count")
        conn.execute("ALTER TABLE source_scans DROP COLUMN stage")
        conn.execute("PRAGMA user_version = 14")

    database.initialize()

    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V4_SCHEMA_VERSION
        row = conn.execute(
            "SELECT stage, processed_count, total_count, heartbeat_at, cancel_requested "
            "FROM source_scans WHERE scan_id = 'scan-v14'"
        ).fetchone()
    assert dict(row) == {
        "stage": "recognizing",
        "processed_count": 1,
        "total_count": 1,
        "heartbeat_at": "2026-08-30T00:00:01+00:00",
        "cancel_requested": 0,
    }


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


def _build_v18_database(path) -> None:
    """构造一个物理 v18 数据库：当前完整结构减去 v19 的扫描目录 frontier。"""

    from app.media_v4.persistence.schema_v4 import (
        create_v8_structures,
        create_v9_structures,
        create_v10_structures,
        create_v13_structures,
        create_v15_structures,
        create_v16_structures,
        create_v17_structures,
        create_v18_structures,
    )

    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        create_schema_v4(conn)
        create_v6_structures(conn)
        create_v8_structures(conn)
        create_v9_structures(conn)
        create_v10_structures(conn)
        create_v13_structures(conn)
        create_v15_structures(conn)
        create_v16_structures(conn)
        create_v17_structures(conn)
        create_v18_structures(conn)
        conn.execute("PRAGMA user_version = 18")
        conn.commit()
    finally:
        conn.close()


def test_v18_database_is_migrated_to_v19_without_touching_media_facts(tmp_path):
    """v18 真实库必须能升到 v19（新增扫描目录 frontier），不能被判成需要重置。"""

    db_path = tmp_path / "legacy-v18.db"
    _build_v18_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, "
            "playback_locator, route_id, display_name, created_at, updated_at) "
            "VALUES ('root-keep', 'baidu', 'directory_tree', 'K:/百度网盘/01动画', '', '', "
            "'01动画', '2026-09-15T00:00:00+00:00', '2026-09-15T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) "
            "VALUES ('scan-keep', 'root-keep', 1, 'completed')"
        )

    V4Database(db_path).initialize()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        root = conn.execute(
            "SELECT display_name FROM source_roots WHERE root_id = 'root-keep'"
        ).fetchone()
        scan = conn.execute(
            "SELECT status FROM source_scans WHERE scan_id = 'scan-keep'"
        ).fetchone()

    assert version == V4_SCHEMA_VERSION == 20
    assert "source_scan_directories" in tables
    assert V4Database.REQUIRED_TABLES >= {"source_scan_directories"}
    # 既有媒体事实与来源记录不能被迁移改写。
    assert str(root["display_name"]) == "01动画"
    assert str(scan["status"]) == "completed"


def test_startup_backfill_only_touches_rows_that_need_it(tmp_path):
    """幂等迁移在每次启动都会跑：UPDATE 必须只命中真正需要补齐的行。

    没有 WHERE 时，每次启动都会对整张 jobs 表产生一次覆盖写事务；jobs 只增不减
    （每作品每 revision 至少两条），WAL 放量随导入次数线性增长。
    """

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.schema_v4 import migrate_schema_v15_to_v16

    database = V4Database(tmp_path / "backfill.db")
    database.initialize()
    with database.connect() as conn:
        # jobs.revision_id / work_id 有外键，先建最小来源与 revision。
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-backfill', 'baidu', 'directory_tree', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-backfill', 'root-backfill', 1, 'completed', 'ready', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at) "
            "VALUES ('rev-1', 'root-backfill', 'scan-backfill', 'fixture', 'confirmed', 'now')"
        )
        for index in range(5):
            needs_backfill = index == 0
            conn.execute(
                "INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, status, "
                "heartbeat_at, started_at, finished_at, created_at, updated_at) "
                "VALUES (?, 'scrape_work', 'rev-1', ?, ?, 'succeeded', ?, 'started', ?, 'now', 'now')",
                (
                    f"job-{index}",
                    f"w-{index}",
                    f"key-{index}",
                    "" if needs_backfill else "beat",
                    "" if needs_backfill else "done",
                ),
            )
        before = conn.total_changes
        migrate_schema_v15_to_v16(conn)
        changed = conn.total_changes - before
        row = conn.execute(
            "SELECT heartbeat_at, finished_at FROM jobs WHERE job_id = 'job-0'"
        ).fetchone()

    assert changed == 1, f"只应补齐 1 行，实际改动 {changed} 行"
    assert row["heartbeat_at"] and row["finished_at"], "需要补齐的行仍然要被补上"


def test_v19_database_gains_query_indices_without_touching_media_facts(tmp_path):
    """v19 真实库必须能升到 v20（新增三个查询索引），且媒体事实不变。"""

    import sqlite3 as _sqlite3

    from app.media_v4.persistence.schema_v4 import (
        create_v8_structures,
        create_v9_structures,
        create_v10_structures,
        create_v13_structures,
        create_v15_structures,
        create_v16_structures,
        create_v17_structures,
        create_v18_structures,
        create_v19_structures,
    )

    db_path = tmp_path / "legacy-v19.db"
    conn = _sqlite3.connect(db_path)
    try:
        conn.row_factory = _sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        create_schema_v4(conn)
        create_v6_structures(conn)
        create_v8_structures(conn)
        create_v9_structures(conn)
        create_v10_structures(conn)
        create_v13_structures(conn)
        create_v15_structures(conn)
        create_v16_structures(conn)
        create_v17_structures(conn)
        create_v18_structures(conn)
        create_v19_structures(conn)
        conn.execute("PRAGMA user_version = 19")
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, "
            "playback_locator, route_id, display_name, created_at, updated_at) "
            "VALUES ('root-keep', 'baidu', 'directory_tree', 'K:/百度网盘/01动画', '', '', "
            "'01动画', '2026-09-15T00:00:00+00:00', '2026-09-15T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    # v19 上还没有这三个索引（否则本用例就无法证明迁移新增了它们）。
    with _sqlite3.connect(db_path) as probe:
        before = {
            str(row[0])
            for row in probe.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert not {"idx_v4_bindings_work", "idx_v4_artifacts_work", "idx_v4_jobs_revision"} & before

    V4Database(db_path).initialize()

    with _sqlite3.connect(db_path) as conn:
        conn.row_factory = _sqlite3.Row
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        indices = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        root = conn.execute(
            "SELECT display_name FROM source_roots WHERE root_id = 'root-keep'"
        ).fetchone()

    assert version == V4_SCHEMA_VERSION == 20
    assert {"idx_v4_bindings_work", "idx_v4_artifacts_work", "idx_v4_jobs_revision"} <= indices
    assert str(root["display_name"]) == "01动画"

