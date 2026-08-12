# -*- coding: utf-8 -*-
"""增量 diff 测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.import_plan.diff import compute_diff
from app.raw.models import RawFile, RawSnapshot


def _make_snapshot(files):
    """构造测试用 RawSnapshot"""
    video_count = sum(1 for f in files if f.resource_hint == "video")
    return RawSnapshot(
        snapshot_id="test_snap",
        source="pan115",
        created_at="2026-01-01T00:00:00",
        file_count=len(files),
        video_count=video_count,
        files=files,
    )


def _make_file(path, resource_hint="video", size=1000, file_id=""):
    """构造测试用 RawFile"""
    name = path.split("/")[-1]
    ext = "." + name.split(".")[-1] if "." in name else ""
    return RawFile(
        id=file_id,
        source="pan115",
        relative_path=path,
        real_path=f"H:\\115open\\{path}",
        name=name,
        ext=ext,
        resource_hint=resource_hint,
        size=size,
    )


def test_added():
    """新增文件识别为 added"""
    old = _make_snapshot([_make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1")])
    new = _make_snapshot([
        _make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1"),
        _make_file("动画/AIR.2005/AIR.S01E02.mkv", file_id="f2"),
    ])
    diff = compute_diff(old, new)
    assert diff.added_count == 1
    added = [i for i in diff.items if i.change_type == "added"]
    assert added[0].new_relative_path == "动画/AIR.2005/AIR.S01E02.mkv"


def test_missing():
    """消失文件识别为 missing"""
    old = _make_snapshot([
        _make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1"),
        _make_file("动画/AIR.2005/AIR.S01E02.mkv", file_id="f2"),
    ])
    new = _make_snapshot([_make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1")])
    diff = compute_diff(old, new)
    assert diff.missing_count == 1
    missing = [i for i in diff.items if i.change_type == "missing"]
    assert missing[0].old_relative_path == "动画/AIR.2005/AIR.S01E02.mkv"


def test_unchanged():
    """相同路径识别为 unchanged"""
    old = _make_snapshot([_make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1")])
    new = _make_snapshot([_make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1")])
    diff = compute_diff(old, new)
    assert diff.unchanged_count == 1


def test_same_path_with_changed_fingerprint_is_replaced():
    """本地同一路径的文件大小或修改时间变化时必须识别为替换版本。"""
    old_file = _make_file("动画/AIR.2005/AIR.S01E01.mkv", size=1000)
    old_file.source = "local"
    old = _make_snapshot([old_file])
    new_file = _make_file("动画/AIR.2005/AIR.S01E01.mkv", size=1200)
    new_file.source = "local"
    new_file.mtime = 200.0
    new = _make_snapshot([new_file])

    diff = compute_diff(old, new)

    replaced = [item for item in diff.items if item.change_type == "replaced"]
    assert len(replaced) == 1
    assert replaced[0].old_relative_path == replaced[0].new_relative_path


def test_cloud_mount_mtime_drift_does_not_block_seasonal_append():
    """目录树基线与网盘挂载时间戳不一致时，原有剧集仍应视为未变化。"""
    old_file = _make_file("新番/作品A/Season 1/作品A.S01E01.mkv", size=1000)
    old_file.mtime = 100.0
    current_file = _make_file("新番/作品A/Season 1/作品A.S01E01.mkv", size=1000)
    current_file.mtime = 200.0
    added_file = _make_file("新番/作品A/Season 1/作品A.S01E02.mkv", size=1200)
    added_file.mtime = 200.0

    diff = compute_diff(_make_snapshot([old_file]), _make_snapshot([current_file, added_file]))

    assert diff.unchanged_count == 1
    assert diff.replaced_count == 0
    assert diff.added_count == 1


def test_directory_tree_metadata_can_be_calibrated_by_mounted_scan():
    """目录树未提供文件元数据时，首次挂载扫描不应把旧集当替换版本。"""
    old_file = _make_file("动画/AIR.2005/AIR.S01E01.mkv", size=None)
    old_file.mtime = None
    new_file = _make_file("动画/AIR.2005/AIR.S01E01.mkv", size=1200)
    new_file.mtime = 200.0

    diff = compute_diff(_make_snapshot([old_file]), _make_snapshot([new_file]))

    assert diff.unchanged_count == 1
    assert diff.replaced_count == 0


def test_same_size_and_mtime_but_changed_content_fingerprint_is_replaced():
    old_file = _make_file("动画/AIR.2005/AIR.S01E01.mkv", size=1000)
    old_file.mtime = 100.0
    old_file.content_fingerprint = "old"
    new_file = _make_file("动画/AIR.2005/AIR.S01E01.mkv", size=1000)
    new_file.mtime = 100.0
    new_file.content_fingerprint = "new"

    diff = compute_diff(_make_snapshot([old_file]), _make_snapshot([new_file]))

    assert diff.replaced_count == 1


def test_moved():
    """同 basename + size 不同路径识别为 moved"""
    old = _make_snapshot([_make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1", size=500)])
    new = _make_snapshot([_make_file("动画/AIR.2005/Season1/AIR.S01E01.mkv", file_id="f1", size=500)])
    diff = compute_diff(old, new)
    assert diff.moved_count == 1
    moved = [i for i in diff.items if i.change_type == "moved"]
    assert moved[0].old_relative_path == "动画/AIR.2005/AIR.S01E01.mkv"
    assert moved[0].new_relative_path == "动画/AIR.2005/Season1/AIR.S01E01.mkv"


def test_safety_blocked_delete_ratio():
    """delete_ratio > 0.30 blocked"""
    old_files = [_make_file(f"动画/test/S01E{i:02d}.mkv", file_id=f"f{i}", size=100) for i in range(10)]
    new_files = [_make_file(f"动画/test/S01E{i:02d}.mkv", file_id=f"f{i}", size=100) for i in range(3)]
    old = _make_snapshot(old_files)
    new = _make_snapshot(new_files)
    diff = compute_diff(old, new)
    assert diff.safety.blocked is True
    assert diff.safety.delete_ratio > 0.30


def test_safety_blocked_path_change_ratio():
    """path_change_ratio > 0.30 blocked"""
    old_files = [_make_file(f"动画/test/S01E{i:02d}.mkv", file_id=f"f{i}", size=100) for i in range(10)]
    new_files = [_make_file(f"动画/test/New/S01E{i:02d}.mkv", file_id=f"f{i}", size=100) for i in range(10)]
    old = _make_snapshot(old_files)
    new = _make_snapshot(new_files)
    diff = compute_diff(old, new)
    assert diff.safety.blocked is True
    assert diff.safety.path_change_ratio > 0.30


def test_safety_blocked_total_change_ratio():
    """total_change_ratio > 0.50 blocked"""
    old_files = [_make_file(f"动画/test/S01E{i:02d}.mkv", file_id=f"f{i}", size=100) for i in range(10)]
    new_files = old_files + [
        _make_file(f"动画/test/S01E{i:02d}.mkv", file_id=f"f{i}", size=100)
        for i in range(10, 17)
    ]
    old = _make_snapshot(old_files)
    new = _make_snapshot(new_files)
    diff = compute_diff(old, new)
    assert diff.safety.blocked is True
    assert diff.safety.total_change_ratio > 0.50


def test_safety_not_blocked_normal():
    """正常变化不 blocked"""
    old_files = [_make_file(f"动画/test/S01E{i:02d}.mkv", file_id=f"f{i}") for i in range(10)]
    new_files = old_files + [_make_file(f"动画/test/S01E11.mkv", file_id="f11")]
    old = _make_snapshot(old_files)
    new = _make_snapshot(new_files)
    diff = compute_diff(old, new)
    assert diff.safety.blocked is False


def test_missing_no_delete_operation():
    """missing 不生成删除操作，只标记 needs_review"""
    old = _make_snapshot([_make_file("动画/AIR.2005/AIR.S01E01.mkv", file_id="f1")])
    new = _make_snapshot([])
    diff = compute_diff(old, new)
    missing = [i for i in diff.items if i.change_type == "missing"]
    assert len(missing) == 1
    assert missing[0].needs_review is True


if __name__ == "__main__":
    tests = [
        test_added,
        test_missing,
        test_unchanged,
        test_moved,
        test_safety_blocked_delete_ratio,
        test_safety_blocked_path_change_ratio,
        test_safety_blocked_total_change_ratio,
        test_safety_not_blocked_normal,
        test_missing_no_delete_operation,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
