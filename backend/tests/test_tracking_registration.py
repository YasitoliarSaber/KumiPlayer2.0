# -*- coding: utf-8 -*-

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.raw.models import RawSnapshot


def test_register_seasonal_plan_binds_only_independent_work_directories(tmp_path, monkeypatch):
    from app.tracking import registration

    seasonal_root = tmp_path / "新番"
    work_root = seasonal_root / "目录作品"
    season_root = work_root / "Season 1"
    season_root.mkdir(parents=True)
    loose_file = seasonal_root / "散装作品.S01E01.mkv"
    loose_file.write_text("", encoding="utf-8")
    episode = season_root / "目录作品.S01E01.mkv"
    episode.write_text("", encoding="utf-8")

    snapshot = RawSnapshot(snapshot_id="snap", source="baidu", source_root=str(seasonal_root), import_scope="seasonal")
    plan = ImportPlan(
        plan_id="plan",
        source="baidu",
        source_snapshot_id="snap",
        import_family="anime",
        import_scope="seasonal",
        items=[
            ImportPlanItem(work_id="work-folder", work_title="目录作品", series_group="目录作品", resource_type="video", real_path=str(episode), season_number=1),
            ImportPlanItem(work_id="work-loose", work_title="散装作品", series_group="散装作品", resource_type="video", real_path=str(loose_file), season_number=1),
        ],
    )
    saved = []
    monkeypatch.setattr(registration, "load_raw_snapshot", lambda _snapshot_id: snapshot)
    monkeypatch.setattr(registration, "upsert_tracking_binding", lambda binding: saved.append(binding) or binding)
    monkeypatch.setattr(registration, "get_tracking_binding_by_identity", lambda *_args: None)
    monkeypatch.setattr(registration, "get_tracking_binding_by_root", lambda *_args: None)
    monkeypatch.setattr(registration, "save_raw_snapshot", lambda _snapshot, **_kwargs: None)
    monkeypatch.setattr(registration, "save_import_plan", lambda _plan, update_latest=False: None)

    result = registration.register_seasonal_plan(plan)

    assert result["registered"] == 1
    assert result["skipped_loose"] == 1
    from app.library.index import _library_work_id

    assert saved[0].work_id == _library_work_id(plan.items[0])
    assert saved[0].root_path == str(work_root)


def test_register_seasonal_plan_uses_library_card_identity_and_baseline(tmp_path, monkeypatch):
    """新番追更绑定必须对齐前端卡片 ID，并以当前导入计划作为增量基准。"""
    from app.library.index import _library_work_id
    from app.tracking import registration

    seasonal_root = tmp_path / "新番"
    work_root = seasonal_root / "目录作品"
    season_root = work_root / "Season 1"
    season_root.mkdir(parents=True)
    episode = season_root / "目录作品.S01E01.mkv"
    episode.write_text("", encoding="utf-8")
    snapshot = RawSnapshot(
        snapshot_id="snap-card-id", source="baidu", source_root=str(seasonal_root),
        import_scope="seasonal",
    )
    item = ImportPlanItem(
        work_id="raw-work-id", work_title="目录作品", series_group="目录作品",
        card_type="main_series", resource_type="video", real_path=str(episode),
        season_number=1,
    )
    plan = ImportPlan(
        plan_id="plan-card-id", source="baidu", source_snapshot_id=snapshot.snapshot_id,
        import_family="anime", import_scope="seasonal", items=[item],
    )
    saved = []
    monkeypatch.setattr(registration, "load_raw_snapshot", lambda _snapshot_id: snapshot)
    monkeypatch.setattr(registration, "upsert_tracking_binding", lambda binding: saved.append(binding) or binding)
    monkeypatch.setattr(registration, "get_tracking_binding_by_identity", lambda *_args: None)
    monkeypatch.setattr(registration, "get_tracking_binding_by_root", lambda *_args: None)
    monkeypatch.setattr(registration, "save_raw_snapshot", lambda _snapshot, **_kwargs: None)
    monkeypatch.setattr(registration, "save_import_plan", lambda _plan, update_latest=False: None)

    registration.register_seasonal_plan(plan)

    assert len(saved) == 1
    assert saved[0].work_id == _library_work_id(item)
    assert saved[0].last_snapshot_id != snapshot.snapshot_id
    assert saved[0].baseline_plan_id != plan.plan_id


def test_register_seasonal_plan_migrates_matching_legacy_raw_binding(tmp_path, monkeypatch):
    """新卡片 ID 写入成功后，旧原始 ID 绑定应迁移并清理，避免重复扫描。"""
    from app.tracking import registration
    from app.tracking.models import TrackingBinding

    seasonal_root = tmp_path / "新番"
    work_root = seasonal_root / "目录作品"
    season_root = work_root / "Season 1"
    season_root.mkdir(parents=True)
    episode = season_root / "目录作品.S01E01.mkv"
    episode.write_text("", encoding="utf-8")
    snapshot = RawSnapshot(
        snapshot_id="snap-migrate", source="baidu", source_root=str(seasonal_root),
        import_scope="seasonal",
    )
    item = ImportPlanItem(
        work_id="legacy-raw-work", work_title="目录作品", series_group="目录作品",
        card_type="main_series", resource_type="video", real_path=str(episode),
        season_number=1,
    )
    plan = ImportPlan(
        plan_id="plan-migrate", source="baidu", source_snapshot_id=snapshot.snapshot_id,
        import_family="anime", import_scope="seasonal", items=[item],
    )
    legacy = TrackingBinding(
        binding_id="legacy-binding", work_id=item.work_id, display_title="旧标题",
        logical_source="baidu", root_path=str(work_root), season_number=1,
        tracking_state="paused", attention_state="waiting_metadata",
        last_snapshot_id="old-snapshot", baseline_plan_id="old-plan",
    )
    saved = []
    deleted = []
    monkeypatch.setattr(registration, "load_raw_snapshot", lambda _snapshot_id: snapshot)
    monkeypatch.setattr(registration, "get_tracking_binding_by_root", lambda *_args: None)
    monkeypatch.setattr(
        registration,
        "get_tracking_binding_by_identity",
        lambda work_id, _root_path, _season: legacy if work_id == item.work_id else None,
    )
    monkeypatch.setattr(registration, "upsert_tracking_binding", lambda binding: saved.append(binding) or binding)
    monkeypatch.setattr(registration, "delete_tracking_binding", lambda binding_id: deleted.append(binding_id) or True)
    monkeypatch.setattr(registration, "save_raw_snapshot", lambda _snapshot, **_kwargs: None)
    monkeypatch.setattr(registration, "save_import_plan", lambda _plan, update_latest=False: None)

    result = registration.register_seasonal_plan(plan)

    assert result["migrated"] == 1
    assert deleted == [legacy.binding_id]
    assert saved[0].work_id != item.work_id
    assert saved[0].tracking_state == "paused"
    assert saved[0].last_snapshot_id != "old-snapshot"
    assert saved[0].baseline_plan_id != "old-plan"


def test_tracking_binding_registration_reuses_matching_identity(tmp_path):
    """重复镜像不会为同一作品目录和季度创建第二个追更绑定。"""
    from app.db.database import init_db
    from app.tracking.models import TrackingBinding
    from app.tracking.store import upsert_tracking_binding

    init_db()
    root = tmp_path / "新番作品"
    root.mkdir()
    first = upsert_tracking_binding(TrackingBinding(
        work_id="work-repeat",
        display_title="新番作品",
        logical_source="baidu",
        root_path=str(root),
        season_number=1,
    ))
    second = upsert_tracking_binding(TrackingBinding(
        work_id="work-repeat",
        display_title="新番作品更新名称",
        logical_source="baidu",
        root_path=str(root),
        season_number=1,
    ))

    assert second.binding_id == first.binding_id


def test_nonseasonal_plan_removes_stale_tracking_bindings(monkeypatch):
    """明确按已完结导入的计划必须清理同批作品遗留的追更绑定。"""
    from app.tracking import registration
    from app.tracking.models import TrackingBinding

    plan = ImportPlan(
        plan_id="completed-plan", source="pan115", import_scope="",
        items=[ImportPlanItem(
            work_id="raw-air", work_title="AIR", series_group="AIR",
            card_type="main_series", resource_type="video",
        )],
    )
    canonical_id = registration._library_work_id(plan.items[0])
    bindings = [
        TrackingBinding(binding_id="raw", work_id="raw-air", logical_source="pan115"),
        TrackingBinding(binding_id="canonical", work_id=canonical_id, logical_source="pan115"),
        TrackingBinding(binding_id="other", work_id="other-work", logical_source="pan115"),
        TrackingBinding(binding_id="baidu", work_id=canonical_id, logical_source="baidu"),
    ]
    deleted = []
    monkeypatch.setattr(registration, "list_tracking_bindings", lambda _state=None: bindings)
    monkeypatch.setattr(
        registration,
        "delete_tracking_binding",
        lambda binding_id: deleted.append(binding_id) or True,
    )

    result = registration.reconcile_tracking_bindings_for_plan(plan)

    assert result == {"removed": 2}
    assert deleted == ["raw", "canonical"]


def test_register_seasonal_plan_creates_one_work_scoped_baseline(tmp_path, monkeypatch):
    """目录树新番登记后，增量基线只能包含当前作品目录。"""
    from app.tracking import registration

    seasonal_root = tmp_path / "新番"
    work_root = seasonal_root / "作品A"
    other_root = seasonal_root / "作品B"
    work_root.mkdir(parents=True)
    other_root.mkdir()
    episode_a = work_root / "作品A.S01E01.mkv"
    episode_b = other_root / "作品B.S01E01.mkv"
    episode_a.write_text("", encoding="utf-8")
    episode_b.write_text("", encoding="utf-8")
    snapshot = RawSnapshot(
        snapshot_id="snap-all", source="baidu", source_root=str(seasonal_root), import_scope="seasonal",
        files=[],
    )
    plan = ImportPlan(
        plan_id="plan-all", source="baidu", source_snapshot_id=snapshot.snapshot_id,
        import_family="anime", import_scope="seasonal",
        items=[
            ImportPlanItem(work_id="work-a", work_title="作品A", series_group="作品A", resource_type="video", real_path=str(episode_a), season_number=1),
            ImportPlanItem(work_id="work-b", work_title="作品B", series_group="作品B", resource_type="video", real_path=str(episode_b), season_number=1),
        ],
    )
    saved_bindings = []
    saved_snapshots = []
    saved_plans = []
    monkeypatch.setattr(registration, "load_raw_snapshot", lambda _snapshot_id: snapshot)
    monkeypatch.setattr(registration, "get_tracking_binding_by_identity", lambda *_args: None)
    monkeypatch.setattr(registration, "get_tracking_binding_by_root", lambda *_args: None)
    monkeypatch.setattr(registration, "upsert_tracking_binding", lambda binding: saved_bindings.append(binding) or binding)
    monkeypatch.setattr(registration, "save_raw_snapshot", lambda value, **_kwargs: saved_snapshots.append(value), raising=False)
    monkeypatch.setattr(registration, "save_import_plan", lambda value, update_latest=False: saved_plans.append((value, update_latest)), raising=False)

    registration.register_seasonal_plan(plan)

    binding_a = next(binding for binding in saved_bindings if binding.display_title == "作品A")
    baseline_plan_a = next(value for value, _ in saved_plans if value.plan_id == binding_a.baseline_plan_id)
    baseline_snapshot_a = next(value for value in saved_snapshots if value.snapshot_id == binding_a.last_snapshot_id)
    assert binding_a.baseline_plan_id != plan.plan_id
    assert binding_a.last_snapshot_id != snapshot.snapshot_id
    assert [item.real_path for item in baseline_plan_a.items] == [str(episode_a)]
    assert baseline_snapshot_a.source_root == str(work_root)
