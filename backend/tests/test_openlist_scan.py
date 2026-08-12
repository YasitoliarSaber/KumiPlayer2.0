# -*- coding: utf-8 -*-
"""OpenList 目录扫描与预设服务聚焦测试。

覆盖：递归遍历、分页、取消、安全上限、空目录、进度回调，
以及 scan_openlist_preset 的成功 / 未变化 / 增量 / 失败无残留语义。
"""

import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import (
    OpenListDirPage,
    OpenListEntry,
    OpenListScanLimitExceeded,
)
from app.integrations.openlist.scan import _ScanCancelled, scan_remote_tree
from app.media_presets.service import scan_openlist_preset
from app.media_presets.store import list_presets

# 测试远端目录树：path -> [(name, is_dir, size, modified)]
TREE = {
    "/夸克网盘/动画": [
        ("冰菓", True, None, None),
        ("摇曳露营", True, None, None),
    ],
    "/夸克网盘/动画/冰菓": [
        ("冰菓 - 01.mkv", False, 100, 1700000000),
        ("冰菓 - 02.mkv", False, 200, 1700000001),
        ("字幕.ass", False, 10, 1700000002),
    ],
    "/夸克网盘/动画/摇曳露营": [
        ("摇曳露营 - 01.mkv", False, 300, 1700000003),
    ],
}

LARGE_DIR = {"/大目录": [(f"文件 {i:03d}.mkv", False, i, 1700000000) for i in range(250)]}


class FakeClient:
    """无网络假客户端：按 tree 提供 list_dir 分页。"""

    def __init__(self, tree=None, *, list_delay=0.0):
        # 深拷贝，避免增量测试修改共享的模块级 TREE 污染其他用例
        self.tree = {path: list(items) for path, items in dict(tree or TREE).items()}
        self.calls: list[tuple[str, int]] = []
        self.list_delay = list_delay

    def list_dir(self, path, page=1, per_page=100):
        self.calls.append((normalize_remote_path(path), page))
        if self.list_delay:
            time.sleep(self.list_delay)
        all_entries = []
        for name, is_dir, size, modified in self.tree.get(normalize_remote_path(path), []):
            all_entries.append(
                OpenListEntry(
                    name=name,
                    is_dir=is_dir,
                    size=size,
                    modified=modified,
                    remote_path=join_remote_path(path, name),
                )
            )
        start = (page - 1) * per_page
        chunk = all_entries[start:start + per_page]
        return OpenListDirPage(entries=chunk, total=len(all_entries))


def _video_names(entries):
    return [e.name for e in entries if not e.is_dir and e.name.endswith(".mkv")]


class TestScanRemoteTree:
    def test_recursive_traversal(self):
        client = FakeClient()
        entries = scan_remote_tree(client, "/夸克网盘/动画")
        videos = _video_names(entries)
        assert sorted(videos) == ["冰菓 - 01.mkv", "冰菓 - 02.mkv", "摇曳露营 - 01.mkv"]
        dirs = [e for e in entries if e.is_dir]
        assert {e.name for e in dirs} == {"冰菓", "摇曳露营"}
        ice = next(e for e in entries if e.name == "冰菓 - 01.mkv")
        assert ice.depth == 2
        assert ice.remote_path == "/夸克网盘/动画/冰菓/冰菓 - 01.mkv"

    def test_empty_directory(self):
        client = FakeClient({"/空目录": []})
        assert scan_remote_tree(client, "/空目录") == []

    def test_cancel_before_first_request(self):
        client = FakeClient()
        with pytest.raises(_ScanCancelled):
            scan_remote_tree(client, "/夸克网盘/动画", should_cancel=lambda: True)
        assert client.calls == []

    def test_cancel_midway(self):
        client = FakeClient()
        state = {"count": 0}

        def should_cancel() -> bool:
            state["count"] += 1
            return state["count"] > 3

        with pytest.raises(_ScanCancelled):
            scan_remote_tree(client, "/夸克网盘/动画", should_cancel=should_cancel)

    def test_entry_limit_exceeded(self):
        client = FakeClient(LARGE_DIR)
        with pytest.raises(OpenListScanLimitExceeded):
            scan_remote_tree(client, "/大目录", max_entries=10)

    def test_depth_limit_exceeded(self):
        deep_tree = {
            "/a": [("b", True, None, None)],
            "/a/b": [("c", True, None, None)],
            "/a/b/c": [("文件.mkv", False, 1, 1)],
        }
        client = FakeClient(deep_tree)
        with pytest.raises(OpenListScanLimitExceeded):
            scan_remote_tree(client, "/a", max_depth=2)

    def test_paginates_large_directory(self):
        client = FakeClient(LARGE_DIR)
        entries = scan_remote_tree(client, "/大目录")
        assert len(entries) == 250
        assert len([call for call in client.calls if call[0] == "/大目录"]) == 3

    def test_progress_callback_reports_counts(self):
        client = FakeClient()
        reports = []
        scan_remote_tree(
            client,
            "/夸克网盘/动画",
            progress_callback=lambda progress, message, patch: reports.append((message, patch)),
        )
        assert reports
        last_message, last_patch = reports[-1]
        assert "已发现" in last_message
        assert last_patch["phase"] == "remote_scan"
        assert last_patch["overall_total_known"] is False
        assert last_patch["found_directory_count"] == 2
        assert last_patch["found_file_count"] == 4
        assert last_patch["found_entry_count"] == 6
        assert last_patch["found_video_candidate_count"] == 3
        # 全部目录读取完成后，已扫描目录数 = 根 + 两个子目录
        assert last_patch["scanned_directory_count"] == 3
        assert last_patch["queued_directory_count"] == 0
        # 深度优先栈 LIFO：根 → 摇曳露营 → 冰菓，最后处理的是冰菓
        assert last_patch["current_path"] == "/夸克网盘/动画/冰菓"
        # 服务端提供了 total：当前目录总数与已读取数可用（冰菓 3 项）
        assert last_patch["current_directory_total"] == 3
        assert last_patch["current_directory_collected"] == 3

    def test_progress_callback_page_reads_current_directory(self):
        """分页目录的中间进度：已读取数小于总数，待扫队列长度正确。"""
        client = FakeClient(LARGE_DIR)
        reports = []
        scan_remote_tree(
            client,
            "/大目录",
            progress_callback=lambda progress, message, patch: reports.append(patch),
        )
        assert reports
        # LARGE_DIR 有 250 个文件、每页 100：第一次翻页回调发生在第 2 页
        page_two = next(item for item in reports if item.get("current_page") == 2)
        assert page_two["current_directory_total"] == 250
        assert page_two["current_directory_collected"] == 100
        assert page_two["scanned_directory_count"] == 0
        assert page_two["queued_directory_count"] == 0

    def test_progress_callback_without_remote_total(self):
        """服务端未提供 total 时，当前目录总数必须为 None，不伪造总量。"""
        class NoTotalClient(FakeClient):
            def list_dir(self, path, page=1, per_page=100):
                result = super().list_dir(path, page=page, per_page=per_page)
                return OpenListDirPage(entries=result.entries, total=None)

        reports = []
        scan_remote_tree(
            NoTotalClient({"/目录": [("a.mkv", False, 1, 1), ("b.mkv", False, 1, 1)]}),
            "/目录",
            progress_callback=lambda progress, message, patch: reports.append(patch),
        )
        assert reports
        assert all(item["overall_total_known"] is False for item in reports)
        assert reports[-1]["current_directory_total"] is None
        assert reports[-1]["current_directory_collected"] == 2
        assert reports[-1]["found_video_candidate_count"] == 2


# ============================================================
# scan_openlist_preset 服务
# ============================================================

def _make_local_mount(tmp_path: Path) -> Path:
    """构造本地挂载目录树（与 TREE 对应），返回本地动画根。"""
    root = tmp_path / "quark" / "动画"
    (root / "冰菓").mkdir(parents=True)
    (root / "摇曳露营").mkdir(parents=True)
    (root / "冰菓" / "冰菓 - 01.mkv").write_bytes(b"1")
    (root / "冰菓" / "冰菓 - 02.mkv").write_bytes(b"2")
    (root / "冰菓" / "字幕.ass").write_bytes(b"3")
    (root / "摇曳露营" / "摇曳露营 - 01.mkv").write_bytes(b"4")
    return root


class TestScanOpenlistPreset:
    def test_accessible_webdav_mount_does_not_require_realpath_resolution(self, tmp_path, monkeypatch):
        """WebDAV 挂载可枚举时，WinError 1005 不能被误判为目录不存在。"""
        local_root = _make_local_mount(tmp_path)
        original_resolve = Path.resolve

        def reject_webdav_realpath(path: Path, strict: bool = False):
            if path == local_root:
                raise OSError(1005, "挂载服务不支持 realpath")
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", reject_webdav_realpath)

        preset, *_ = scan_openlist_preset(
            FakeClient(), "/夸克网盘/动画", str(local_root), "anime", ""
        )

        assert preset.source == "openlist"

    def test_success_creates_preset(self, tmp_path):
        local_root = _make_local_mount(tmp_path)
        preset, version, plan, diff, reused, unchanged = scan_openlist_preset(
            FakeClient(), "/夸克网盘/动画", str(local_root), "anime", ""
        )
        assert preset.source == "openlist"
        assert preset.update_mode == "openlist_scan"
        assert preset.remote_locator == "/夸克网盘/动画"
        assert preset.source_root == str(local_root)
        assert reused is False and unchanged is False
        assert plan.plan_id
        assert version.input_type == "openlist"
        assert version.remote_locator == "/夸克网盘/动画"
        assert version.sha256
        # 清单已落盘供审计
        assert (tmp_path / "data" / "openlist_manifests").exists()

    def test_manifest_sha256_dedup_unchanged(self, tmp_path):
        local_root = _make_local_mount(tmp_path)
        client = FakeClient()
        first = scan_openlist_preset(client, "/夸克网盘/动画", str(local_root), "anime", "")
        second = scan_openlist_preset(client, "/夸克网盘/动画", str(local_root), "anime", "")
        assert second[4] is True and second[5] is True  # reused + unchanged
        assert second[0].preset_id == first[0].preset_id
        assert second[0].version_count == 1  # 未新增版本
        assert len(list_presets()) == 1

    def test_incremental_update_adds_version(self, tmp_path):
        local_root = _make_local_mount(tmp_path)
        client = FakeClient()
        scan_openlist_preset(client, "/夸克网盘/动画", str(local_root), "anime", "")
        # 远端与本地同时新增一集
        client.tree["/夸克网盘/动画/冰菓"].append(("冰菓 - 03.mkv", False, 300, 1700000004))
        (local_root / "冰菓" / "冰菓 - 03.mkv").write_bytes(b"5")
        preset, version, plan, diff, reused, unchanged = scan_openlist_preset(
            client, "/夸克网盘/动画", str(local_root), "anime", ""
        )
        assert reused is True and unchanged is False
        assert preset.version_count == 2
        assert diff is not None
        assert plan.plan_id

    def test_local_mount_missing_fails_without_residue(self, tmp_path):
        missing = tmp_path / "不存在" / "动画"
        with pytest.raises(HTTPException) as exc:
            scan_openlist_preset(FakeClient(), "/夸克网盘/动画", str(missing), "anime", "")
        assert "本地挂载目录不存在" in str(exc.value.detail)
        assert not (tmp_path / "data" / "openlist_manifests").exists()
        assert list_presets() == []

    def test_cancel_fails_without_residue(self, tmp_path):
        local_root = _make_local_mount(tmp_path)
        with pytest.raises(_ScanCancelled):
            scan_openlist_preset(
                FakeClient(),
                "/夸克网盘/动画",
                str(local_root),
                "anime",
                "",
                should_cancel=lambda: True,
            )
        assert list_presets() == []
        manifest_dir = tmp_path / "data" / "openlist_manifests"
        assert not manifest_dir.exists() or not any(manifest_dir.iterdir())

    def test_video_missing_fails_without_residue(self, tmp_path):
        """远端只有文本文件时拒绝，且不产生任何持久化。"""
        local_root = _make_local_mount(tmp_path)
        client = FakeClient({
            "/夸克网盘/文档": [("说明.txt", False, 1, 1)],
        })
        with pytest.raises(HTTPException) as exc:
            scan_openlist_preset(client, "/夸克网盘/文档", str(local_root), "anime", "")
        assert "没有识别到视频" in str(exc.value.detail)
        assert list_presets() == []

    def test_path_validation_failure_blocks_without_residue(self, tmp_path):
        """本地挂载存在但抽样视频缺失 → 阻止并清理候选产物。"""
        local_root = tmp_path / "quark" / "动画"
        (local_root / "冰菓").mkdir(parents=True)  # 目录在，但视频文件缺失
        (local_root / "摇曳露营").mkdir(parents=True)
        with pytest.raises(HTTPException):
            scan_openlist_preset(FakeClient(), "/夸克网盘/动画", str(local_root), "anime", "")
        assert list_presets() == []
        manifest_dir = tmp_path / "data" / "openlist_manifests"
        assert not manifest_dir.exists() or not any(manifest_dir.iterdir())

    def test_manifest_content_is_whitelisted_only(self, tmp_path):
        local_root = _make_local_mount(tmp_path)
        scan_openlist_preset(FakeClient(), "/夸克网盘/动画", str(local_root), "anime", "")
        manifest_dir = tmp_path / "data" / "openlist_manifests"
        manifest = json.loads(next(manifest_dir.glob("*.json")).read_text(encoding="utf-8"))
        assert manifest["format"] == "kumiplayer-openlist-manifest"
        assert manifest["remote_locator"] == "/夸克网盘/动画"
        first_entry = manifest["entries"][0]
        assert set(first_entry.keys()) == {"name", "is_dir", "size", "modified", "remote_path", "depth"}

    def test_phase_callbacks_cover_all_stages(self, tmp_path):
        """扫描后的阶段回调依次覆盖写清单、验证挂载、构建计划。"""
        local_root = _make_local_mount(tmp_path)
        reports = []
        scan_openlist_preset(
            FakeClient(),
            "/夸克网盘/动画",
            str(local_root),
            "anime",
            "",
            progress_callback=lambda progress, message, patch: reports.append((progress, message, patch)),
        )
        phases = [patch["phase"] for _, _, patch in reports if patch]
        assert "remote_scan" in phases  # 由 scan_remote_tree 汇报
        # 远端扫描结束 → 写清单 → 验证挂载 → 构建计划（顺序固定）
        ordered = [p for p in phases if p != "remote_scan"]
        assert ordered == ["manifest_write", "local_validate", "plan_build", "complete"]
        manifest_idx = phases.index("manifest_write")
        assert "保存扫描清单" in reports[manifest_idx][1]
        validate_idx = phases.index("local_validate")
        assert "验证本地挂载文件" in reports[validate_idx][1]

    def test_seasonal_scope_rejected_for_openlist(self, tmp_path):
        """OpenList 首发不支持自动追更：新番范围必须明确拒绝。"""
        local_root = _make_local_mount(tmp_path)
        with pytest.raises(HTTPException) as exc:
            scan_openlist_preset(FakeClient(), "/夸克网盘/动画", str(local_root), "anime", "seasonal")
        assert "暂不支持自动追更" in str(exc.value.detail)
        assert list_presets() == []
