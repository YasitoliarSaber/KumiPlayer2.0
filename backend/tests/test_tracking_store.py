from app.db.database import init_db
from app.tracking.models import TrackingBinding
from app.tracking.store import (
    count_tracking_state_for_clear,
    delete_tracking_state_for_clear,
    get_tracking_binding,
    list_tracking_bindings,
    list_tracking_scan_runs,
    record_tracking_scan_run,
    save_tracking_scan_result,
    upsert_tracking_binding,
)


def test_tracking_clear_count_initializes_missing_database_schema(tmp_path, monkeypatch):
    """清理预览在全新数据库上应返回 0，而不是因为追更表尚未创建而报错。"""
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "new-library.db")

    assert count_tracking_state_for_clear("all") == {
        "binding_count": 0,
        "scan_run_count": 0,
    }


def test_tracking_binding_round_trip(tmp_path, monkeypatch):
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking.db")
    init_db()

    saved = upsert_tracking_binding(TrackingBinding(
        binding_id="binding-1",
        work_id="series-existing",
        logical_source="local",
        root_path=r"H:\新番\作品A",
        season_number=1,
        tracking_state="tracking",
        attention_state="waiting_metadata",
    ))

    loaded = get_tracking_binding("series-existing")
    assert loaded is not None
    assert loaded.binding_id == saved.binding_id
    assert loaded.root_path == r"H:\新番\作品A"
    assert loaded.attention_state == "waiting_metadata"
    assert [item.work_id for item in list_tracking_bindings()] == ["series-existing"]


def test_stale_waiting_metadata_state_is_healed_when_metadata_already_exists(tmp_path, monkeypatch):
    """升级前误写的 waiting_metadata 不应继续污染新番页。"""
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking-heal.db")
    init_db()
    upsert_tracking_binding(TrackingBinding(
        binding_id="binding-complete",
        work_id="series-complete",
        logical_source="baidu",
        root_path=r"H:\新番\作品A",
        attention_state="waiting_metadata",
        last_result={
            "status": "succeeded",
            "scrape": {
                "total_targets": 1,
                "auto_scraped": 0,
                "skipped_existing": 1,
                "review_queued": 0,
                "failed": 0,
            },
        },
    ))

    loaded = get_tracking_binding("series-complete")

    assert loaded is not None
    assert loaded.attention_state == "ready"


def test_clear_tracking_state_is_scoped_by_source_and_removes_scan_history(tmp_path, monkeypatch):
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking-clear.db")
    init_db()

    pan = upsert_tracking_binding(TrackingBinding(
        binding_id="binding-pan",
        work_id="series-pan",
        logical_source="pan115",
        root_path=r"H:\新番\作品A",
    ))
    local = upsert_tracking_binding(TrackingBinding(
        binding_id="binding-local",
        work_id="series-local",
        logical_source="local",
        root_path=r"D:\新番\作品B",
    ))
    record_tracking_scan_run(pan, {"status": "waiting_review"})
    record_tracking_scan_run(local, {"status": "succeeded"})

    deleted = delete_tracking_state_for_clear("pan115")

    assert deleted == {"binding_count": 1, "scan_run_count": 1}
    assert [item.work_id for item in list_tracking_bindings()] == ["series-local"]
    assert [item["work_id"] for item in list_tracking_scan_runs()] == ["series-local"]


def test_deleted_tracking_binding_cannot_be_recreated_by_late_scan_result(tmp_path, monkeypatch):
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking-late-result.db")
    init_db()

    binding = upsert_tracking_binding(TrackingBinding(
        binding_id="binding-late",
        work_id="series-late",
        logical_source="baidu",
        root_path=r"B:\新番\作品C",
    ))
    delete_tracking_state_for_clear("baidu")

    saved = save_tracking_scan_result(binding, {"status": "succeeded"})

    assert saved is None
    assert list_tracking_bindings() == []
    assert list_tracking_scan_runs() == []
