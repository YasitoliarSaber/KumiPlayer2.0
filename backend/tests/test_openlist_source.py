# -*- coding: utf-8 -*-
"""OpenList 来源适配器聚焦测试。

覆盖：清单 → RawSnapshot 转换、相对路径与本地真实路径合同、
目录过滤、越界条目跳过、缺失清单报错、快照 ID 稳定性。
"""

from pathlib import Path

import pytest

from app.integrations.openlist.manifest import write_manifest
from app.integrations.openlist.models import OpenListEntry
from app.sources.openlist import OpenListAdapter, make_snapshot_id
from app.sources.registry import get_source_adapter


def _entries():
    return [
        OpenListEntry(name="冰菓", is_dir=True, remote_path="/夸克网盘/动画/冰菓", depth=1),
        OpenListEntry(
            name="冰菓 - 01.mkv", is_dir=False, size=100, modified=1700000000,
            remote_path="/夸克网盘/动画/冰菓/冰菓 - 01.mkv", depth=2,
        ),
        OpenListEntry(
            name="字幕.ass", is_dir=False, size=10, modified=1700000001,
            remote_path="/夸克网盘/动画/冰菓/字幕.ass", depth=2,
        ),
        OpenListEntry(
            name="其它.mkv", is_dir=False, size=200,
            remote_path="/夸克网盘/动画/其它.mkv", depth=1,
        ),
    ]


def _write(tmp_path: Path) -> Path:
    manifest_id = make_snapshot_id("/夸克网盘/动画", "deadbeef")
    path, _ = write_manifest(
        manifest_id,
        _entries(),
        remote_locator="/夸克网盘/动画",
        source_root=str(tmp_path / "本地动画"),
    )
    return path


class TestOpenListAdapter:
    def test_parse_builds_raw_snapshot(self, tmp_path):
        local_root = tmp_path / "本地动画"
        snapshot = OpenListAdapter().parse(str(_write(tmp_path)), str(local_root))

        assert snapshot.source == "openlist"
        assert snapshot.source_root == str(local_root)
        assert snapshot.file_count == 3
        assert snapshot.video_count == 2
        assert snapshot.snapshot_id

        files = {f.name: f for f in snapshot.files}
        assert set(files) == {"冰菓 - 01.mkv", "字幕.ass", "其它.mkv"}

        ice = files["冰菓 - 01.mkv"]
        assert ice.relative_path == "冰菓/冰菓 - 01.mkv"  # 相对选中目录
        assert ice.source_path_parts == ["冰菓", "冰菓 - 01.mkv"]
        assert ice.virtual_root == "冰菓"
        assert ice.resource_hint == "video"
        assert ice.size == 100
        assert ice.mtime == 1700000000.0
        assert ice.parent_path == "冰菓"
        assert ice.depth == 2

    def test_real_path_always_local_mount(self, tmp_path):
        local_root = tmp_path / "本地动画"
        snapshot = OpenListAdapter().parse(str(_write(tmp_path)), str(local_root))
        for item in snapshot.files:
            assert item.real_path.startswith(str(local_root))
            assert "http" not in item.real_path.lower()
            assert "/夸克网盘" not in item.real_path

        top = next(f for f in snapshot.files if f.name == "其它.mkv")
        assert top.real_path == str(local_root / "其它.mkv")

    def test_directories_not_in_files(self, tmp_path):
        snapshot = OpenListAdapter().parse(str(_write(tmp_path)), str(tmp_path))
        assert all(f.is_file for f in snapshot.files)
        assert all(f.name != "冰菓" for f in snapshot.files)

    def test_entries_outside_locator_skipped(self, tmp_path):
        """远端路径不在选中目录之下 → 跳过，不进入快照。"""
        manifest_id = make_snapshot_id("/夸克网盘/动画", "beef")
        path, _ = write_manifest(
            manifest_id,
            [OpenListEntry(name="越界.mkv", is_dir=False, remote_path="/其它网盘/越界.mkv", depth=1)],
            remote_locator="/夸克网盘/动画",
            source_root=str(tmp_path),
        )
        snapshot = OpenListAdapter().parse(str(path), str(tmp_path))
        assert snapshot.file_count == 0

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(ValueError):
            OpenListAdapter().parse(str(tmp_path / "不存在.json"), str(tmp_path))

    def test_snapshot_id_stable(self):
        assert make_snapshot_id("/夸克网盘/动画", "abc123") == make_snapshot_id("/夸克网盘/动画", "abc123")
        assert make_snapshot_id("/夸克网盘/动画", "abc123") != make_snapshot_id("/夸克网盘/动画", "abc124")

    def test_registry_registers_openlist(self):
        adapter = get_source_adapter("openlist")
        assert adapter.source_id == "openlist"
        assert adapter.mirror_namespace == "openlist"
