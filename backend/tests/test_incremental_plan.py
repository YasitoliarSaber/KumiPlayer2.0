# -*- coding: utf-8 -*-
"""增量计划必须支持追更链路已经审核过的纯新增。"""

from app.import_plan.diff import compute_diff
from app.import_plan.incremental import build_incremental_plan
from app.raw.models import RawFile, RawSnapshot


def _snapshot(paths: list[str]) -> RawSnapshot:
    files = [
        RawFile(
            id=f"episode-{index}", source="baidu", source_root="H:/新番/作品A",
            relative_path=path, real_path=f"H:/新番/作品A/{path}",
            name=path.split("/")[-1], ext=".mkv", resource_hint="video", size=100,
        )
        for index, path in enumerate(paths, start=1)
    ]
    return RawSnapshot(source="baidu", source_root="H:/新番/作品A", files=files, file_count=len(files), video_count=len(files))


def test_reviewed_pure_append_can_build_incremental_plan_despite_count_threshold():
    old = _snapshot(["作品A/Season 1/作品A.S01E01.mkv"])
    new = _snapshot([
        "作品A/Season 1/作品A.S01E01.mkv",
        "作品A/Season 1/作品A.S01E02.mkv",
    ])
    diff = compute_diff(old, new)

    assert diff.safety.blocked is True
    plan = build_incremental_plan(
        diff, "baidu", new.source_root, new_snapshot=new, allow_blocked=True,
    )

    assert plan is not None
    assert [item.episode_number for item in plan.items if item.resource_type == "video"] == [2]
