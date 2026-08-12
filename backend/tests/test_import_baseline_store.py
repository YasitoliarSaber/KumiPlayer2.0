# -*- coding: utf-8 -*-
"""全量导入基线不能被追更/局部快照覆盖。"""

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.raw.models import RawSnapshot


def test_latest_confirmed_plan_skips_tracking_slice(tmp_path, monkeypatch):
    from app.import_plan import store

    monkeypatch.setattr(store, "_get_data_dir", lambda: tmp_path)
    full = ImportPlan(
        plan_id="full-baidu",
        source="baidu",
        status="confirmed",
        created_at="2026-07-18T11:00:00+08:00",
        items=[ImportPlanItem(id=f"full-{index}") for index in range(20)],
    )
    tracking = ImportPlan(
        plan_id="full-baidu_tracking_work-a",
        source="baidu",
        status="confirmed",
        created_at="2026-07-18T12:00:00+08:00",
        import_scope="seasonal",
        summary={"tracking_binding_id": "work-a"},
        items=[ImportPlanItem(id="episode-1")],
    )
    store.save_import_plan(full)
    store.save_import_plan(tracking)

    loaded = store.load_latest_confirmed_import_plan("baidu")

    assert loaded is not None
    assert loaded.plan_id == "full-baidu"


def test_latest_confirmed_plan_prefers_full_over_newer_seasonal_slice(tmp_path, monkeypatch):
    from app.import_plan import store

    monkeypatch.setattr(store, "_get_data_dir", lambda: tmp_path)
    full = ImportPlan(
        plan_id="full-baidu",
        source="baidu",
        status="confirmed",
        created_at="2026-07-18T11:00:00+08:00",
        items=[ImportPlanItem(id=f"full-{index}") for index in range(20)],
    )
    seasonal = ImportPlan(
        plan_id="seasonal-baidu",
        source="baidu",
        status="confirmed",
        created_at="2026-07-18T12:00:00+08:00",
        import_scope="seasonal",
        items=[ImportPlanItem(id="new-episode")],
    )
    store.save_import_plan(full)
    store.save_import_plan(seasonal)

    loaded = store.load_latest_confirmed_import_plan("baidu")

    assert loaded is not None
    assert loaded.plan_id == "full-baidu"


def test_scoped_raw_snapshot_does_not_replace_full_latest(tmp_path, monkeypatch):
    from app.raw import store

    monkeypatch.setattr(store, "_get_snapshots_dir", lambda: tmp_path)
    full = RawSnapshot(snapshot_id="full", source="baidu", file_count=100)
    scoped = RawSnapshot(
        snapshot_id="full_tracking_work-a",
        source="baidu",
        import_scope="seasonal",
        file_count=1,
    )
    store.save_raw_snapshot(full)
    store.save_raw_snapshot(scoped, update_latest=False)

    loaded = store.load_latest_raw_snapshot("baidu")

    assert loaded is not None
    assert loaded.snapshot_id == "full"


def test_latest_raw_snapshot_recovers_from_legacy_tracking_pointer(tmp_path, monkeypatch):
    from app.raw import store

    monkeypatch.setattr(store, "_get_snapshots_dir", lambda: tmp_path)
    full = RawSnapshot(snapshot_id="full", source="baidu", file_count=100)
    scoped = RawSnapshot(
        snapshot_id="full_tracking_work-a",
        source="baidu",
        import_scope="seasonal",
        file_count=1,
    )
    store.save_raw_snapshot(full)
    # 模拟旧版本已经把追更切片写进 latest 指针。
    store.save_raw_snapshot(scoped)

    loaded = store.load_latest_raw_snapshot("baidu")

    assert loaded is not None
    assert loaded.snapshot_id == "full"
