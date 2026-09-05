"""P-008 目录树播放根词法映射（11.19.2 离线合同）。

播放根由配置根、TXT 位置与导出文件名范围纯词法推导，不 stat/open/枚举
源盘；源盘离线与在线产生完全相同的结果。只有真实的多根歧义才失败。
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from app.media_v4.sources.scanner import read_directory_tree_text
from app.media_v4.sources.tree_root import (
    TreePlaybackRootResolver,
    tree_scope_name,
)
from tests.source_disk_guard import guard_source_disk_io as _guard_source_io


def _resolve(tree: Path, configured_roots: list, extra_candidates: list | None = None):
    resolver = TreePlaybackRootResolver(
        tree,
        configured_roots=configured_roots,
        extra_candidates=extra_candidates or [],
    )
    return resolver.resolve(read_directory_tree_text(tree))


def _write_tree(root: Path, name: str, lines: list[str]) -> Path:
    tree = root / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    tree.write_text("\n".join(lines), encoding="utf-8")
    return tree


def test_tree_scope_name_extracts_the_media_sub_library():
    assert tree_scope_name("01动画_文件目录_20260810194440.txt") == "01动画"
    assert tree_scope_name("电影_文件目录.txt") == "电影"
    assert tree_scope_name("根目录_文件目录_20260810194440.txt") == ""
    assert tree_scope_name("plain-name.txt") == "plain-name"


def test_txt_parent_is_used_when_it_is_a_sub_library_of_the_configured_root(tmp_path):
    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    tree = _write_tree(library, "01动画_文件目录.txt", ["Show/Show.S01E01.mkv"])

    resolution = _resolve(tree, [mount])

    assert resolution.ok is True
    assert Path(resolution.root) == library
    assert resolution.hits == 0 and resolution.total == 0


def test_total_root_alone_works_for_a_full_mount_export(tmp_path):
    mount = tmp_path / "115网盘"
    tree = _write_tree(mount, "根目录_文件目录.txt", ["动画/Show/Show.S01E01.mkv"])

    resolution = _resolve(tree, [mount])

    assert resolution.ok is True
    assert Path(resolution.root) == mount


def test_txt_copied_outside_mount_uses_scope_candidate(tmp_path):
    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    # TXT 被复制到本地清单目录，不在挂载盘内；不创建源视频
    tree = _write_tree(tmp_path / "清单", "01动画_文件目录.txt", ["Show/Show.S01E01.mkv"])

    resolution = _resolve(tree, [mount])

    assert resolution.ok is True
    assert Path(resolution.root) == library


def test_offline_source_disk_maps_without_existence_check(tmp_path, monkeypatch):
    """源盘不存在/离线：词法映射仍成立，不再做样本可达性验证。"""

    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    tree = _write_tree(tmp_path / "清单", "01动画_文件目录.txt", ["Show/Show.S01E01.mkv"])
    _guard_source_io(monkeypatch, [mount])

    resolution = _resolve(tree, [mount])

    assert resolution.ok is True
    assert Path(resolution.root) == library
    assert resolution.hits == 0 and resolution.total == 0
    assert "生成" in resolution.reason


def test_ambiguous_multiple_roots_are_rejected(tmp_path):
    mount = tmp_path / "百度网盘"
    sub_a = mount / "01动画"
    sub_b = mount / "02动画"
    # 两个互不相关的配置根，清单无区分证据 → 歧义失败
    tree = _write_tree(tmp_path / "清单", "动画_文件目录.txt", ["Show/Show.S01E01.mkv"])

    resolution = _resolve(tree, [sub_a, sub_b])

    assert resolution.ok is False
    assert "多个" in resolution.reason


def test_large_tree_maps_lexically_without_sampling_io(tmp_path, monkeypatch):
    """大清单不做头/中/尾抽样探测，纯词法映射即可确定根。"""

    mount = tmp_path / "百度网盘"
    library = mount / "动画"
    lines = [f"Show/Show.S01E{index + 1:02d}.mkv" for index in range(60)]
    tree = _write_tree(tmp_path / "清单", "动画_文件目录.txt", lines)
    _guard_source_io(monkeypatch, [mount])

    resolution = _resolve(tree, [mount])

    assert resolution.ok is True
    assert Path(resolution.root) == library
    assert resolution.hits == 0 and resolution.total == 0


def test_hybrid_route_local_root_is_accepted_without_duplicate_layer(tmp_path):
    mount = tmp_path / "OpenList"
    local_root = mount / "Anime" / "TV"
    tree = _write_tree(tmp_path / "清单", "anime-tree.txt", ["Show/Show.S01E01.mkv"])

    resolution = _resolve(tree, [local_root])

    assert resolution.ok is True
    assert Path(resolution.root) == local_root


# ── 11.19.2 参数化：配置根/清单名/条目 → 播放根 ─────────────────────────────


@pytest.mark.parametrize(
    "tree_parent, tree_name, configured_roots, entry_line, expected_root",
    [
        # 根 Q:\百度网盘，清单名 01动画_文件目录.txt，条目 Show/Season 1/a.mkv
        (r"D:\清单", "01动画_文件目录.txt", [r"Q:\百度网盘"],
         "Show/Season 1/a.mkv", r"Q:\百度网盘\01动画"),
        # 根已是 Q:\百度网盘\01动画，同上条目 → 不重复追加 01动画
        (r"D:\清单", "01动画_文件目录.txt", [r"Q:\百度网盘\01动画"],
         "Show/Season 1/a.mkv", r"Q:\百度网盘\01动画"),
        # 根 Q:\百度网盘，条目已经是 01动画/Show/Season 1/a.mkv → 01动画 只出现一次
        (r"D:\清单", "01动画_文件目录.txt", [r"Q:\百度网盘"],
         "01动画/Show/Season 1/a.mkv", r"Q:\百度网盘"),
        # TXT 在根下的 01动画 子目录，内容从 Show 起 → 由 TXT 位置证明子库范围
        (r"Q:\百度网盘\01动画", "01动画_文件目录.txt", [r"Q:\百度网盘"],
         "Show/Season 1/a.mkv", r"Q:\百度网盘\01动画"),
        # TXT 从源盘复制到普通本地目录 → 复制位置不得作为播放根
        (r"D:\普通目录\清单", "01动画_文件目录.txt", [r"Q:\百度网盘"],
         "Show/Season 1/a.mkv", r"Q:\百度网盘\01动画"),
        # 115 的 根目录…_目录树.txt → 只去掉格式定义的虚拟根
        (r"D:\清单", "根目录_目录树_20260703203700.txt", [r"Q:\115网盘"],
         "动画/Show/Show.S01E01.mkv", r"Q:\115网盘"),
        # 无约定名称 anime-tree.txt → 不把任意文件名当子目录
        (r"D:\清单", "anime-tree.txt", [r"Q:\百度网盘"],
         "Show/Season 1/a.mkv", r"Q:\百度网盘"),
    ],
)
def test_offline_root_mapping_is_lexical_and_source_disk_offline_safe(
    monkeypatch, tree_parent, tree_name, configured_roots, entry_line, expected_root
):
    """虚构源根上的一切 stat/open/枚举都被哨兵拦截；映射仍按词法完成。"""

    _guard_source_io(monkeypatch, configured_roots)
    tree = Path(tree_parent) / tree_name
    resolver = TreePlaybackRootResolver(tree, configured_roots=configured_roots)

    resolution = resolver.resolve(entry_line + "\n")

    assert resolution.ok is True
    def norm(value):
        return str(PureWindowsPath(value))

    assert norm(resolution.root) == norm(expected_root)
    assert resolution.hits == 0 and resolution.total == 0
    assert "生成" in resolution.reason


def test_offline_root_mapping_preserves_original_directory_case(tmp_path, monkeypatch):
    """Windows 比较忽略大小写，但输出保留原目录文字。"""

    _guard_source_io(monkeypatch, [r"Q:\百度网盘"])
    tree = Path(r"D:\清单") / "01动画_文件目录.txt"
    resolver = TreePlaybackRootResolver(tree, configured_roots=[r"Q:\百度网盘"])

    resolution = resolver.resolve("Show/Season 1/a.mkv\n")

    assert resolution.ok is True
    assert resolution.root == r"Q:\百度网盘\01动画"


def test_unconfigured_root_fails_with_clear_message():
    tree = Path(r"D:\清单") / "01动画_文件目录.txt"
    resolver = TreePlaybackRootResolver(tree, configured_roots=[])

    resolution = resolver.resolve("Show/Season 1/a.mkv\n")

    assert resolution.ok is False
    assert resolution.root == ""
    assert "配置" in resolution.reason


# ── 11.19.8 嵌套配置根：祖先链必须收敛到最深子根 ────────────────────────────


def test_nested_configured_roots_keep_the_deepest_child_root(monkeypatch):
    """父根+子根同时配置：只保留最深子根，不能把内容归到更宽父根。"""
    _guard_source_io(monkeypatch, [r"Q:\百度网盘"])
    tree = Path(r"D:\清单") / "根目录_文件目录.txt"

    resolution = TreePlaybackRootResolver(
        tree, configured_roots=[r"Q:\百度网盘", r"Q:\百度网盘\01动画"],
    ).resolve("Show/Show.S01E01.mkv\n")

    assert resolution.ok is True
    assert PureWindowsPath(resolution.root) == PureWindowsPath(r"Q:\百度网盘\01动画")

    # 候选顺序不得影响结果：倒序输入仍收敛到同一最深子根。
    reversed_resolution = TreePlaybackRootResolver(
        tree, configured_roots=[r"Q:\百度网盘\01动画", r"Q:\百度网盘"],
    ).resolve("Show/Show.S01E01.mkv\n")

    assert reversed_resolution.ok is True
    assert PureWindowsPath(reversed_resolution.root) == PureWindowsPath(r"Q:\百度网盘\01动画")


def test_nested_roots_without_scope_evidence_stay_ambiguous(monkeypatch):
    """两个互不相关的配置根继续歧义失败，不得假装收敛。"""
    _guard_source_io(monkeypatch, [r"Q:\百度网盘"])
    tree = Path(r"D:\清单") / "根目录_文件目录.txt"

    resolution = TreePlaybackRootResolver(
        tree, configured_roots=[r"Q:\百度网盘\01动画", r"Q:\百度网盘\02动画"],
    ).resolve("Show/Show.S01E01.mkv\n")

    assert resolution.ok is False
    assert "多个" in resolution.reason


def test_nested_roots_with_scoped_entries_do_not_double_the_scope_layer(monkeypatch):
    """条目首段已带 scope 且根尾也是同一 scope：去掉该层，避免 01动画 重复。"""
    _guard_source_io(monkeypatch, [r"Q:\百度网盘"])
    tree = Path(r"D:\清单") / "01动画_文件目录.txt"

    resolution = TreePlaybackRootResolver(
        tree, configured_roots=[r"Q:\百度网盘", r"Q:\百度网盘\01动画"],
    ).resolve("01动画/Show/Show.S01E01.mkv\n")

    assert resolution.ok is True
    assert PureWindowsPath(resolution.root) == PureWindowsPath(r"Q:\百度网盘")
