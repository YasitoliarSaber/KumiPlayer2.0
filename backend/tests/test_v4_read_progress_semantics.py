"""B5-1：读取阶段的进度语义。

全量/增量读取阶段无法预知总量（预先递归统计会让请求量翻倍），界面必须显示
“已发现 N 个媒体文件”，而不是一个不动的百分比；只有预先知道条目数的任务
（例如 TXT）才给百分比。
"""

from __future__ import annotations

from datetime import UTC, datetime

_ROOT_COLUMNS = (
    "root_id, provider, ingest_method, source_locator, playback_locator, route_id, "
    "display_name, enabled, created_at, updated_at, root_container, source_mode, "
    "last_scan_mode, retired_at, retired_reason"
)
_SCAN_COLUMNS = (
    "scan_id, root_id, generation, status, started_at, finished_at, error, stage, "
    "processed_count, total_count, heartbeat_at, cancel_requested"
)


def _seed_scan(database, *, stage: str, processed: int, total: int, status: str = "running") -> None:
    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        conn.execute(
            f"INSERT INTO source_roots ({_ROOT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "root-progress", "quark", "openlist_scan", "/夸克网盘/动画", "", "",
                "动画", 1, now, now, "", "openlist_full", "full", "", "",
            ),
        )
        conn.execute(
            f"INSERT INTO source_scans ({_SCAN_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("scan-progress", "root-progress", 1, status, now, "", "", stage, processed, total, now, 0),
        )


def test_reading_stage_reports_discovered_files_instead_of_fake_percent(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "progress.db")
    database.initialize()
    _seed_scan(database, stage="reading_source", processed=143, total=0)

    payload = get_durable_scan(database, "scan-progress", include_entries=False)

    assert payload["progress"] is None
    assert payload["discovered_count"] == 143
    assert "143" in payload["stage_label"]
    assert "读取媒体来源" in payload["stage_label"]


def test_known_total_keeps_static_label_and_percent(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "progress-known.db")
    database.initialize()
    _seed_scan(database, stage="parsing", processed=5, total=10)

    payload = get_durable_scan(database, "scan-progress", include_entries=False)

    assert payload["progress"] == 0.5
    assert payload["stage_label"] == "解析媒体条目"


def test_reading_stage_without_evidence_yet_keeps_plain_label(tmp_path):
    """刚进入读取阶段、还没有任何证据时不要显示 “已发现 0 个”。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "progress-zero.db")
    database.initialize()
    _seed_scan(database, stage="reading_source", processed=0, total=0)

    payload = get_durable_scan(database, "scan-progress", include_entries=False)

    assert payload["stage_label"] == "读取媒体来源"
    assert payload["discovered_count"] == 0


def test_paused_scan_is_resumable_and_never_looks_completed(tmp_path):
    """预算耗尽的扫描必须是 paused + resumable，不能表现为完整成功。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "progress-paused.db")
    database.initialize()
    _seed_scan(database, stage="paused", processed=400, total=400, status="paused")

    payload = get_durable_scan(database, "scan-progress", include_entries=False)

    assert payload["status"] == "paused"
    assert payload["resumable"] is True
    assert "继续" in payload["stage_label"]
    assert payload["interrupted"] is False


def test_completed_scan_is_not_resumable(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "progress-done.db")
    database.initialize()
    _seed_scan(database, stage="ready", processed=12, total=12, status="completed")

    payload = get_durable_scan(database, "scan-progress", include_entries=False)

    assert payload["status"] == "completed"
    assert payload["resumable"] is False
    assert payload["stage_label"] == "识别结果已就绪"
