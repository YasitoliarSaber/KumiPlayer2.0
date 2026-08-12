from app.import_plan.diff import DiffItem, DiffResult
from app.import_plan.incremental import merge_incremental_plan
from app.import_plan.models import ImportPlan, ImportPlanItem


def _item(item_id: str, path: str, episode: int) -> ImportPlanItem:
    return ImportPlanItem(
        id=item_id,
        plan_id="base",
        raw_file_id=f"raw-{item_id}",
        source="local",
        relative_path=path,
        real_path=f"H:/新番/{path}",
        resource_type="video",
        action="generate_strm",
        work_id="specific-work",
        work_title="作品A",
        media_type="tv",
        show_type="anime_series",
        series_group="作品A",
        card_type="main_series",
        group_type="season",
        season_number=1,
        episode_number=episode,
        availability="available",
    )


def test_merge_keeps_old_items_and_marks_missing_without_deleting():
    base = ImportPlan(
        plan_id="base",
        source="local",
        source_snapshot_id="old",
        status="executed",
        items=[_item("e1", "作品A.S01E01.mkv", 1), _item("e2", "作品A.S01E02.mkv", 2)],
    )
    delta = ImportPlan(
        plan_id="delta",
        source="local",
        source_snapshot_id="new",
        status="draft",
        items=[_item("e4-new", "作品A.S01E04.mkv", 4)],
    )
    diff = DiffResult(
        source="local",
        old_snapshot_id="old",
        new_snapshot_id="new",
        items=[
            DiffItem(change_type="unchanged", old_relative_path="作品A.S01E01.mkv", new_relative_path="作品A.S01E01.mkv"),
            DiffItem(change_type="missing", old_relative_path="作品A.S01E02.mkv"),
            DiffItem(change_type="added", new_relative_path="作品A.S01E04.mkv"),
        ],
    )

    merged = merge_incremental_plan(base, delta, diff)

    assert merged.plan_id == "delta"
    assert merged.source_snapshot_id == "new"
    assert merged.status == "confirmed"
    by_episode = {item.episode_number: item for item in merged.items}
    assert set(by_episode) == {1, 2, 4}
    assert by_episode[2].availability == "missing"
    assert by_episode[4].series_group == "作品A"


def test_replaced_file_preserves_episode_identity():
    old = _item("stable-e1", "作品A.S01E01.mkv", 1)
    replacement = _item("new-e1", "作品A.S01E01.mkv", 1)
    replacement.real_path = "H:/新番/作品A.S01E01.mkv"
    base = ImportPlan(plan_id="base", source="local", status="executed", items=[old])
    delta = ImportPlan(plan_id="delta", source="local", status="draft", items=[replacement])
    diff = DiffResult(
        source="local",
        new_snapshot_id="new",
        items=[DiffItem(
            change_type="replaced",
            old_relative_path=old.relative_path,
            new_relative_path=replacement.relative_path,
        )],
    )

    merged = merge_incremental_plan(base, delta, diff)

    assert len(merged.items) == 1
    assert merged.items[0].id == "stable-e1"
    assert merged.items[0].availability == "available"


def test_reappeared_episode_restores_missing_identity_instead_of_duplicating():
    missing = _item("stable-e2", "作品A.S01E02.mkv", 2)
    missing.availability = "missing"
    restored = _item("new-e2", "作品A.S01E02.mkv", 2)
    base = ImportPlan(plan_id="base", source="local", status="executed", items=[missing])
    delta = ImportPlan(plan_id="delta", source="local", status="draft", items=[restored])
    diff = DiffResult(
        source="local",
        old_snapshot_id="missing-snapshot",
        new_snapshot_id="restored-snapshot",
        items=[DiffItem(
            change_type="added",
            raw_file_id="raw-new-e2",
            new_relative_path=restored.relative_path,
            resource_type="video",
        )],
    )

    merged = merge_incremental_plan(base, delta, diff)

    episode_twos = [item for item in merged.items if item.episode_number == 2]
    assert len(episode_twos) == 1
    assert episode_twos[0].id == "stable-e2"
    assert episode_twos[0].availability == "available"
