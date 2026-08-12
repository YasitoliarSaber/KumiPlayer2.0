from pathlib import Path

from app.db.database import init_db
from app.tracking.models import TrackingBinding
from app.tracking.service import (
    _baseline_snapshot_for_plan,
    _diff_counts,
    _is_safe_seasonal_append,
    _rebase_snapshot_to_tracking_root,
    _tracking_attention_from_scrape_result,
    scan_all_tracking,
    scan_tracking_binding,
)
from app.tracking.store import list_tracking_scan_runs, upsert_tracking_binding


def test_root_import_recognizes_multiple_work_directories(tmp_path, monkeypatch):
    """一个新番媒体库根目录应生成多部作品计划，而不是被当成一部作品。"""
    from app.tracking.service import import_seasonal_root

    root = tmp_path / "新番媒体库"
    work_a = root / "作品A"
    work_b = root / "作品B"
    work_a.mkdir(parents=True)
    work_b.mkdir()
    (work_a / "作品A.S01E01.mkv").write_bytes(b"a")
    (work_b / "作品B.S01E01.mkv").write_bytes(b"b")
    saved_plans = []
    progress_updates = []

    monkeypatch.setattr("app.tracking.service.save_raw_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.tracking.service.save_import_plan", lambda plan, **_kwargs: saved_plans.append(plan))
    monkeypatch.setattr(
        "app.import_pipeline.service.run_auto_import_pipeline",
        lambda source, plan_id, **_kwargs: {"source": source, "plan_id": plan_id, "status": "succeeded"},
    )

    result = import_seasonal_root(
        str(root),
        "local",
        include_scrape=False,
        progress_callback=lambda progress, message, patch: progress_updates.append((progress, message, patch or {})),
    )

    assert result["detected_work_count"] == 2
    assert result["video_count"] == 2
    assert saved_plans[0].import_scope == "seasonal"
    assert any(update[2].get("detected_work_count") == 2 for update in progress_updates)
    assert any(update[2].get("video_count") == 2 for update in progress_updates)
    final_logs = progress_updates[-1][2].get("logs") or []
    assert any(log.get("message") == "扫描新番根目录" for log in final_logs)
    assert any("识别到 2 部新番" in log.get("message", "") for log in final_logs)


def test_first_tracking_scan_builds_playable_library_without_tmdb(tmp_path, monkeypatch):
    from app.core import config as core_config
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking.db")
    init_db()
    mirror = tmp_path / "mirror"
    cfg = core_config.AppConfig(mirror_dir=str(mirror))
    monkeypatch.setattr("app.core.config.load_config", lambda force_reload=False: cfg)
    refreshed_work_ids = []
    monkeypatch.setattr(
        "app.tracking.service.refresh_tracking_library_work",
        lambda plan, work_id: refreshed_work_ids.append(work_id),
    )
    latest_plan_updates = []
    monkeypatch.setattr(
        "app.mirror.generator.save_import_plan",
        lambda plan, update_latest=True: latest_plan_updates.append(update_latest),
    )
    monkeypatch.setattr(
        "app.import_plan.service.save_import_plan",
        lambda plan, update_latest=True: latest_plan_updates.append(update_latest),
    )

    root = tmp_path / "作品A"
    root.mkdir()
    (root / "作品A.S01E01.mkv").write_bytes(b"episode-one")
    binding = upsert_tracking_binding(TrackingBinding(
        work_id="series-test-a",
        display_title="作品A",
        logical_source="local",
        root_path=str(root),
        series_group="作品A",
        season_number=1,
        attention_state="waiting_metadata",
    ))

    result = scan_tracking_binding(binding.binding_id, include_scrape=False)

    assert result["status"] == "succeeded"
    assert result["added_count"] == 1
    assert list(mirror.rglob("*.strm"))
    assert refreshed_work_ids == ["series-test-a"]
    assert latest_plan_updates == [False, False]


def test_offline_tracking_source_is_non_destructive(tmp_path, monkeypatch):
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking.db")
    init_db()
    binding = upsert_tracking_binding(TrackingBinding(
        work_id="series-offline",
        display_title="离线作品",
        root_path=str(tmp_path / "missing"),
    ))

    result = scan_tracking_binding(binding.binding_id, include_scrape=False)

    assert result["status"] == "source_unavailable"
    assert result["deleted_count"] == 0
    runs = list_tracking_scan_runs(work_id="series-offline")
    assert len(runs) == 1
    assert runs[0]["status"] == "source_unavailable"


def test_missing_only_scan_waits_for_review_without_advancing_baseline(tmp_path, monkeypatch):
    from app.core import config as core_config
    from app.db import database
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.raw.store import save_raw_snapshot
    from app.sources.local import LocalScanner
    from app.tracking.store import get_tracking_binding

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking.db")
    init_db()
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda force_reload=False: core_config.AppConfig(
            mirror_dir=str(tmp_path / "mirror"), pan115_root=str(tmp_path / "115挂载"),
        ),
    )
    root = tmp_path / "作品A"
    root.mkdir()
    episode = root / "作品A.S01E01.mkv"
    episode.write_bytes(b"episode-one")
    old_snapshot = LocalScanner().scan(str(root), source_root=str(root), include_root=True)
    save_raw_snapshot(old_snapshot)
    base = ImportPlan(
        plan_id="base-missing",
        source="local",
        source_snapshot_id=old_snapshot.snapshot_id,
        status="executed",
        items=[ImportPlanItem(
            id="stable-e1", plan_id="base-missing", raw_file_id=old_snapshot.files[0].id,
            source="local", relative_path=old_snapshot.files[0].relative_path,
            real_path=old_snapshot.files[0].real_path, resource_type="video",
            action="generate_strm", work_id="series-missing", canonical_work_id="series-missing",
            work_title="作品A", series_group="作品A", card_type="main_series",
            media_type="tv", show_type="anime_series", group_type="season",
            season_number=1, episode_number=1,
        )],
    )
    save_import_plan(base)
    binding = upsert_tracking_binding(TrackingBinding(
        work_id="series-missing", display_title="作品A", root_path=str(root),
        series_group="作品A", season_number=1,
        last_snapshot_id=old_snapshot.snapshot_id, baseline_plan_id=base.plan_id,
    ))
    episode.unlink()

    result = scan_tracking_binding(binding.binding_id, include_scrape=False)

    assert result["status"] == "waiting_review"
    assert result["missing_count"] == 1
    saved = get_tracking_binding("series-missing")
    assert saved is not None
    assert saved.attention_state == "waiting_review"
    assert saved.last_snapshot_id == old_snapshot.snapshot_id


def test_mounted_cloud_tracking_scan_disables_content_fingerprint(tmp_path, monkeypatch):
    from app.core import config as core_config
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking.db")
    init_db()
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda force_reload=False: core_config.AppConfig(
            mirror_dir=str(tmp_path / "mirror"), pan115_root=str(tmp_path / "115挂载"),
        ),
    )
    monkeypatch.setattr(
        "app.sources.local._content_fingerprint",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("挂载盘不应读取视频内容")),
    )
    root = tmp_path / "115挂载" / "作品A"
    root.mkdir(parents=True)
    (root / "作品A.S01E01.mkv").write_bytes(b"remote-placeholder")
    binding = upsert_tracking_binding(TrackingBinding(
        work_id="series-mounted", display_title="作品A", logical_source="local",
        root_path=str(root), series_group="作品A", season_number=1,
    ))

    result = scan_tracking_binding(binding.binding_id, include_scrape=False)

    assert result["status"] == "succeeded"


def test_tracking_scan_keeps_mounted_files_when_resolve_is_unavailable(tmp_path, monkeypatch):
    """挂载网盘可能无法 resolve；扫描仍必须保留该作品目录中的剧集。"""
    from app.core import config as core_config
    from app.db import database

    database.close_connection()
    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking.db")
    init_db()
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda force_reload=False: core_config.AppConfig(mirror_dir=str(tmp_path / "mirror")),
    )
    root = tmp_path / "挂载作品"
    root.mkdir()
    (root / "挂载作品.S01E01.mkv").write_bytes(b"episode-one")
    binding = upsert_tracking_binding(TrackingBinding(
        work_id="series-no-resolve", display_title="挂载作品", logical_source="baidu",
        root_path=str(root), series_group="挂载作品", season_number=1,
    ))
    original_resolve = Path.resolve

    def unavailable_resolve(path, *args, **kwargs):
        if str(path).startswith(str(root)):
            raise OSError("挂载盘不支持 resolve")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", unavailable_resolve)

    result = scan_tracking_binding(binding.binding_id, include_scrape=False)

    assert result["status"] == "succeeded"
    assert result["added_count"] == 1


def test_seasonal_append_bypasses_only_total_count_safety_block():
    """早期新番一次补入多集可以直接更新，其他风险信号仍不放行。"""
    from app.import_plan.diff import compute_diff
    from app.raw.models import RawFile, RawSnapshot

    def snapshot(count: int, *, changed: bool = False) -> RawSnapshot:
        files = [
            RawFile(
                id=f"episode-{index}", relative_path=f"Season 1/S01E{index:02d}.mkv",
                name=f"S01E{index:02d}.mkv", ext=".mkv", resource_hint="video",
                size=200 if changed and index == 1 else 100,
            )
            for index in range(1, count + 1)
        ]
        return RawSnapshot(source="baidu", video_count=len(files), files=files)

    pure_append = compute_diff(snapshot(1), snapshot(4))
    assert pure_append.safety.blocked is True
    assert _is_safe_seasonal_append(pure_append) is True

    changed_existing = compute_diff(snapshot(1), snapshot(4, changed=True))
    assert changed_existing.safety.blocked is True
    assert _is_safe_seasonal_append(changed_existing) is False


def test_seasonal_append_ignores_non_video_attachment_replacements():
    """图片等附件变化不能把正常的新番新增剧集送入人工确认。"""
    from app.import_plan.diff import compute_diff
    from app.raw.models import RawFile, RawSnapshot

    old = RawSnapshot(files=[
        RawFile(relative_path="作品A/Season 1/作品A.S01E01.mkv", name="作品A.S01E01.mkv", ext=".mkv", resource_hint="video", size=100),
        RawFile(relative_path="作品A/poster.jpg", name="poster.jpg", ext=".jpg", resource_hint="image", size=100),
    ], video_count=1)
    new = RawSnapshot(files=[
        RawFile(relative_path="作品A/Season 1/作品A.S01E01.mkv", name="作品A.S01E01.mkv", ext=".mkv", resource_hint="video", size=100),
        RawFile(relative_path="作品A/Season 1/作品A.S01E02.mkv", name="作品A.S01E02.mkv", ext=".mkv", resource_hint="video", size=100),
        RawFile(relative_path="作品A/Season 1/作品A.S01E03.mkv", name="作品A.S01E03.mkv", ext=".mkv", resource_hint="video", size=100),
        RawFile(relative_path="作品A/poster.jpg", name="poster.jpg", ext=".jpg", resource_hint="image", size=200),
    ], video_count=3)

    diff = compute_diff(old, new)

    assert diff.safety.blocked is True
    assert diff.added_count == 2
    assert diff.replaced_count == 1
    assert _is_safe_seasonal_append(diff) is True


def test_tracking_diff_counts_only_added_videos_as_episodes():
    from app.import_plan.diff import compute_diff
    from app.raw.models import RawFile, RawSnapshot

    old = RawSnapshot(files=[], video_count=0)
    new = RawSnapshot(files=[
        RawFile(relative_path="作品A.S01E01.mkv", name="作品A.S01E01.mkv", ext=".mkv", resource_hint="video"),
        RawFile(relative_path="poster.jpg", name="poster.jpg", ext=".jpg", resource_hint="image"),
    ], video_count=1)

    counts = _diff_counts(compute_diff(old, new))

    assert counts["added_count"] == 2
    assert counts["added_episode_count"] == 1


def test_existing_complete_metadata_is_not_marked_waiting():
    """already_scraped 表示元数据完整，不应因 auto_scraped 为零产生误报。"""
    assert _tracking_attention_from_scrape_result({
        "total_targets": 1,
        "auto_scraped": 0,
        "skipped_existing": 1,
        "review_queued": 0,
        "failed": 0,
    }) == "ready"
    assert _tracking_attention_from_scrape_result({
        "total_targets": 1,
        "auto_scraped": 0,
        "skipped_existing": 0,
        "review_queued": 0,
        "failed": 1,
    }) == "waiting_metadata"
    assert _tracking_attention_from_scrape_result({
        "total_targets": 1,
        "auto_scraped": 0,
        "skipped_existing": 0,
        "review_queued": 1,
        "failed": 0,
    }) == "waiting_review"


def test_tracking_recovers_video_present_in_snapshot_but_missing_from_plan():
    """失败扫描曾推进快照时，累计计划中缺失的视频必须重新作为新增处理。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.raw.models import RawFile, RawSnapshot

    episode_one = RawFile(relative_path="作品A/Season 1/作品A.S01E01.mkv", resource_hint="video")
    episode_two = RawFile(relative_path="作品A/Season 1/作品A.S01E02.mkv", resource_hint="video")
    poisoned_snapshot = RawSnapshot(files=[episode_one, episode_two], file_count=2, video_count=2)
    base_plan = ImportPlan(items=[ImportPlanItem(
        relative_path=episode_one.relative_path, resource_type="video", episode_number=1,
    )])

    recovered = _baseline_snapshot_for_plan(poisoned_snapshot, base_plan)

    assert recovered.video_count == 1
    assert [item.relative_path for item in recovered.files] == [episode_one.relative_path]


def test_tracking_rebases_tree_snapshot_before_comparing_real_folder(tmp_path):
    """目录树基线多出的“新番”前缀不能把原有剧集误报为移动。"""
    from app.import_plan.diff import compute_diff
    from app.raw.models import RawFile, RawSnapshot

    root = tmp_path / "新番" / "作品A"
    episode = root / "Season 1" / "作品A.S01E01.mkv"
    old = RawSnapshot(source="baidu", files=[RawFile(
        id="old", relative_path="新番/作品A/Season 1/作品A.S01E01.mkv",
        real_path=str(episode), name=episode.name, ext=".mkv",
        size=100, resource_hint="video",
    )], video_count=1)
    current = RawSnapshot(source="baidu", files=[RawFile(
        id="new", relative_path="作品A/Season 1/作品A.S01E01.mkv",
        real_path=str(episode), name=episode.name, ext=".mkv",
        size=100, resource_hint="video",
    )], video_count=1)

    rebased = _rebase_snapshot_to_tracking_root(old, root)
    diff = compute_diff(rebased, current)

    assert [item.relative_path for item in rebased.files] == ["作品A/Season 1/作品A.S01E01.mkv"]
    assert diff.unchanged_count == 1
    assert diff.moved_count == 0


def test_scan_all_keeps_library_card_binding_for_duplicate_directory(tmp_path, monkeypatch):
    """历史手动绑定与目录树绑定指向同一目录时，只扫描当前作品卡片对应的一条。"""
    from app.library.models import LibraryIndex, WorkIndex
    from app.tracking.service import _deduplicate_tracking_bindings

    root = tmp_path / "作品A"
    canonical = TrackingBinding(
        binding_id="canonical", work_id="series-card", display_title="作品A",
        logical_source="baidu", root_path=str(root), season_number=1,
    )
    stale = TrackingBinding(
        binding_id="stale", work_id="series-manual", display_title="旧作品A",
        logical_source="baidu", root_path=str(root), season_number=1,
    )
    monkeypatch.setattr(
        "app.library.store.load_library_index",
        lambda: LibraryIndex(works=[WorkIndex(work_id="series-card", title="作品A", import_scope="seasonal")]),
    )

    selected, stale_ids = _deduplicate_tracking_bindings([stale, canonical])

    assert [item.work_id for item in selected] == ["series-card"]
    assert stale_ids == ["stale"]


def test_scan_all_only_processes_visible_source_and_work_ids(monkeypatch):
    """批量扫描必须取来源与前端可见作品 ID 的交集，不能扫描其他来源绑定。"""
    bindings = [
        TrackingBinding(binding_id="baidu-visible", work_id="work-baidu-visible", logical_source="baidu", root_path="B:/visible"),
        TrackingBinding(binding_id="baidu-hidden", work_id="work-baidu-hidden", logical_source="baidu", root_path="B:/hidden"),
        TrackingBinding(binding_id="pan115-wrong", work_id="work-pan115-wrong", logical_source="pan115", root_path="P:/wrong"),
    ]
    scanned: list[str] = []
    monkeypatch.setattr("app.tracking.service.list_tracking_bindings", lambda state=None: bindings)
    monkeypatch.setattr("app.tracking.service._deduplicate_tracking_bindings", lambda items: (items, []))
    monkeypatch.setattr("app.tracking.service.scan_tracking_binding", lambda binding_id, **kwargs: scanned.append(binding_id) or {"status": "succeeded"})
    monkeypatch.setattr("app.tracking.service.rebuild_tracking_library_from_bindings", lambda items: {"count": len(items)})

    result = scan_all_tracking(
        include_scrape=False,
        logical_source="baidu",
        work_ids=["work-baidu-visible"],
    )

    assert scanned == ["baidu-visible"]
    assert result["total"] == 1


def test_scan_all_rebuilds_only_selected_cards(monkeypatch):
    """列表外的历史绑定不能在扫描当前新番后重新生成额外卡片。"""
    bindings = [
        TrackingBinding(binding_id="visible", work_id="work-visible", logical_source="local", root_path="D:/visible"),
        TrackingBinding(binding_id="stale", work_id="work-stale", logical_source="local", root_path="D:/stale"),
    ]
    rebuilt = []
    monkeypatch.setattr("app.tracking.service.list_tracking_bindings", lambda state=None: bindings)
    monkeypatch.setattr("app.tracking.service._deduplicate_tracking_bindings", lambda items: (items, []))
    monkeypatch.setattr("app.tracking.service.scan_tracking_binding", lambda *_args, **_kwargs: {"status": "succeeded"})
    monkeypatch.setattr(
        "app.tracking.service.rebuild_tracking_library_from_bindings",
        lambda items: rebuilt.extend(items) or {"restored": len(items)},
    )

    scan_all_tracking(include_scrape=False, work_ids=["work-visible"])

    assert [binding.work_id for binding in rebuilt] == ["work-visible"]


def test_scan_all_returns_titles_and_episode_counts_for_updated_works(monkeypatch):
    """批量结果必须能直接生成“哪部作品新增几集”的前端摘要。"""
    bindings = [
        TrackingBinding(
            binding_id="updated", work_id="work-updated", display_title="与你相恋到生命尽头",
            logical_source="baidu", root_path="B:/updated",
        ),
        TrackingBinding(
            binding_id="unchanged", work_id="work-unchanged", display_title="没有更新的作品",
            logical_source="baidu", root_path="B:/unchanged",
        ),
    ]
    monkeypatch.setattr("app.tracking.service.list_tracking_bindings", lambda state=None: bindings)
    monkeypatch.setattr("app.tracking.service._deduplicate_tracking_bindings", lambda items: (items, []))
    monkeypatch.setattr(
        "app.tracking.service.scan_tracking_binding",
        lambda binding_id, **_kwargs: {
            "status": "succeeded",
            "added_count": 2 if binding_id == "updated" else 0,
            "added_episode_count": 2 if binding_id == "updated" else 0,
        },
    )
    monkeypatch.setattr("app.tracking.service.rebuild_tracking_library_from_bindings", lambda _items: {})

    result = scan_all_tracking(include_scrape=False)

    assert result["results"][0]["display_title"] == "与你相恋到生命尽头"
    assert result["results"][0]["added_episode_count"] == 2
    assert result["results"][1]["display_title"] == "没有更新的作品"
    assert result["results"][1]["added_episode_count"] == 0


def test_scan_all_cooperatively_stops_before_next_binding(monkeypatch):
    """收到取消请求后，不得继续扫描队列中的下一部作品。"""
    bindings = [
        TrackingBinding(binding_id="first", work_id="work-first", logical_source="baidu", root_path="B:/first"),
        TrackingBinding(binding_id="second", work_id="work-second", logical_source="baidu", root_path="B:/second"),
    ]
    scanned: list[str] = []
    monkeypatch.setattr("app.tracking.service.list_tracking_bindings", lambda state=None: bindings)
    monkeypatch.setattr("app.tracking.service._deduplicate_tracking_bindings", lambda items: (items, []))
    monkeypatch.setattr("app.tracking.service.scan_tracking_binding", lambda binding_id, **kwargs: scanned.append(binding_id) or {"status": "succeeded"})

    def should_cancel():
        return len(scanned) >= 1

    try:
        scan_all_tracking(include_scrape=False, should_cancel=should_cancel)
    except RuntimeError as error:
        assert str(error) == "任务已停止"
    else:
        assert False, "取消后批量扫描应立即结束"

    assert scanned == ["first"]


def test_scan_all_cancelled_after_last_binding_does_not_rebuild_library(monkeypatch):
    """最后一部扫描完成后收到清理取消信号，也不能用旧绑定重建作品索引。"""
    bindings = [
        TrackingBinding(
            binding_id="last",
            work_id="work-last",
            logical_source="baidu",
            root_path="B:/last",
        ),
    ]
    scanned: list[str] = []
    rebuilds: list[list[TrackingBinding]] = []
    monkeypatch.setattr("app.tracking.service.list_tracking_bindings", lambda state=None: bindings)
    monkeypatch.setattr("app.tracking.service._deduplicate_tracking_bindings", lambda items: (items, []))
    monkeypatch.setattr(
        "app.tracking.service.scan_tracking_binding",
        lambda binding_id, **kwargs: scanned.append(binding_id) or {"status": "succeeded"},
    )
    monkeypatch.setattr(
        "app.tracking.service.rebuild_tracking_library_from_bindings",
        lambda items: rebuilds.append(items) or {"count": len(items)},
    )

    try:
        scan_all_tracking(
            include_scrape=False,
            should_cancel=lambda: len(scanned) >= 1,
        )
    except RuntimeError as error:
        assert str(error) == "任务已停止"
    else:
        assert False, "清理取消后不应进入媒体库重建"

    assert scanned == ["last"]
    assert rebuilds == []
