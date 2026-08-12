# -*- coding: utf-8 -*-
"""增量 diff API 测试"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from app.main import app

_DATA_DIR = Path(__file__).parent.parent.parent / "data"


def _cleanup():
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR)


def _make_plan(plan_id, status, paths):
    return _make_plan_for_source(plan_id, status, "pan115", paths)


def _make_plan_for_source(plan_id, status, source, paths):
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.raw.models import RawFile, RawSnapshot
    from app.raw.store import save_raw_snapshot

    items = [
        ImportPlanItem(
            id=f"{plan_id}_item_{idx}",
            plan_id=plan_id,
            raw_file_id=f"{plan_id}_raw_{idx}",
            source=source,
            relative_path=path,
            real_path=f"H:\\115open\\{path}",
            resource_type="video",
            action="generate_strm",
        )
        for idx, path in enumerate(paths, start=1)
    ]
    plan = ImportPlan(
        plan_id=plan_id,
        source=source,
        source_snapshot_id=plan_id,
        created_at="2026-01-01T00:00:00+08:00",
        status=status,
        items=items,
    )
    save_import_plan(plan)
    save_raw_snapshot(RawSnapshot(
        snapshot_id=plan_id,
        source=source,
        source_root=r"H:\115open",
        created_at="2026-01-01T00:00:00+08:00",
        file_count=len(paths),
        video_count=len(paths),
        files=[RawFile(
            id=f"{plan_id}_raw_{idx}", source=source, source_root=r"H:\115open",
            relative_path=path, real_path=f"H:\\115open\\{path}",
            name=path.split("/")[-1], ext=".mkv", resource_hint="video", size=1000,
        ) for idx, path in enumerate(paths, start=1)],
    ))
    return plan


def test_create_and_get_diff_api():
    """POST diff 后可 GET diff"""
    _cleanup()
    try:
        old_paths = [f"动画/AIR.2005/AIR.S01E{i:02d}.mkv" for i in range(1, 11)]
        new_paths = old_paths + ["动画/AIR.2005/AIR.S01E11.mkv"]
        _make_plan("old_plan", "confirmed", old_paths)
        _make_plan("new_plan", "draft", new_paths)
        client = TestClient(app)

        resp = client.post("/api/imports/pan115/diff", json={"old_snapshot_id": "old_plan", "new_snapshot_id": "new_plan"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["added_count"] == 1
        assert data["safety"]["blocked"] is False

        get_resp = client.get(f"/api/imports/pan115/diff/{data['diff_id']}")
        assert get_resp.status_code == 200
        assert get_resp.json()["diff_id"] == data["diff_id"]
    finally:
        _cleanup()


def test_incremental_preview_api_added_only():
    """incremental preview 只基于新增视频生成预览"""
    _cleanup()
    try:
        old_paths = [f"动画/AIR.2005/AIR.S01E{i:02d}.mkv" for i in range(1, 11)]
        new_paths = old_paths + ["动画/AIR.2005/AIR.S01E11.mkv"]
        _make_plan("old_plan", "confirmed", old_paths)
        _make_plan("new_plan", "draft", new_paths)
        # save_import_plan 会把 latest 指到 new_plan；重新保存 old_plan 作为 latest baseline 后，preview 用 new_snapshot_id。
        _make_plan("old_plan", "confirmed", old_paths)
        client = TestClient(app)

        resp = client.post("/api/imports/pan115/incremental/preview", json={"new_snapshot_id": "new_plan"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["total_items"] == 11
        assert any(item["relative_path"].endswith("AIR.S01E11.mkv") for item in data["items"])
    finally:
        _cleanup()


def test_incremental_preview_blocked_409():
    """blocked diff 不能生成 incremental preview"""
    _cleanup()
    try:
        old_paths = [f"动画/test/S01E{i:02d}.mkv" for i in range(10)]
        new_paths = [f"动画/test/S01E{i:02d}.mkv" for i in range(3)]
        _make_plan("old_plan", "confirmed", old_paths)
        _make_plan("new_plan", "draft", new_paths)
        _make_plan("old_plan", "confirmed", old_paths)
        client = TestClient(app)

        resp = client.post("/api/imports/pan115/incremental/preview", json={"new_snapshot_id": "new_plan"})
        assert resp.status_code == 409
    finally:
        _cleanup()


def test_diff_source_mismatch_400():
    """URL source 与 plan.source 不匹配时拒绝"""
    _cleanup()
    try:
        _make_plan_for_source("old_plan", "confirmed", "pan115", ["动画/AIR.2005/AIR.S01E01.mkv"])
        _make_plan_for_source("new_plan", "draft", "baidu", ["动画/AIR.2005/AIR.S01E01.mkv"])
        client = TestClient(app)

        resp = client.post("/api/imports/pan115/diff", json={"old_snapshot_id": "old_plan", "new_snapshot_id": "new_plan"})
        assert resp.status_code == 400
    finally:
        _cleanup()


if __name__ == "__main__":
    tests = [
        test_create_and_get_diff_api,
        test_incremental_preview_api_added_only,
        test_incremental_preview_blocked_409,
        test_diff_source_mismatch_400,
    ]
    passed = failed = 0
    for t in tests:
        try:
            _cleanup()
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        finally:
            _cleanup()
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
