"""P-008 目录树精确播放根解析：总根/子库作用域候选 + 有界抽样验证。"""

from __future__ import annotations

from pathlib import Path

from app.media_v4.sources.tree_root import (
    TreePlaybackRootResolver,
    consume_scan_metadata,
    load_scan_metadata,
    stage_scan_metadata,
    tree_scope_name,
)


def _write_tree(root: Path, name: str, lines: list[str]) -> Path:
    tree = root / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    tree.write_text("\n".join(lines), encoding="utf-8")
    return tree


def _make_show(root: Path, name: str = "Show.S01E01.mkv") -> Path:
    media = root / name
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"video")
    return media


def test_tree_scope_name_extracts_the_media_sub_library():
    assert tree_scope_name("01动画_文件目录_20260810194440.txt") == "01动画"
    assert tree_scope_name("电影_文件目录.txt") == "电影"
    assert tree_scope_name("根目录_文件目录_20260810194440.txt") == ""
    assert tree_scope_name("plain-name.txt") == "plain-name"


def test_txt_parent_is_used_when_it_is_a_sub_library_of_the_configured_root(tmp_path):
    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    media = _make_show(library / "Show")
    tree = _write_tree(library, "01动画_文件目录.txt", ["Show/Show.S01E01.mkv"])

    resolution = TreePlaybackRootResolver(tree, configured_roots=[mount]).resolve()

    assert resolution.ok is True
    assert Path(resolution.root) == library
    assert resolution.hits == 1 and resolution.total == 1
    assert media.exists()


def test_total_root_alone_works_for_a_full_mount_export(tmp_path):
    mount = tmp_path / "115网盘"
    _make_show(mount / "动画" / "Show")
    tree = _write_tree(mount, "根目录_文件目录.txt", ["动画/Show/Show.S01E01.mkv"])

    resolution = TreePlaybackRootResolver(tree, configured_roots=[mount]).resolve()

    assert resolution.ok is True
    assert Path(resolution.root) == mount


def test_txt_copied_outside_mount_uses_scope_candidate(tmp_path):
    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    _make_show(library / "Show")
    # TXT 被复制到本地清单目录，不在挂载盘内
    tree = _write_tree(tmp_path / "清单", "01动画_文件目录.txt", ["Show/Show.S01E01.mkv"])

    resolution = TreePlaybackRootResolver(tree, configured_roots=[mount]).resolve()

    assert resolution.ok is True
    assert Path(resolution.root) == library


def test_zero_hit_root_is_rejected(tmp_path):
    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    library.mkdir(parents=True)  # 目录存在但视频不存在
    tree = _write_tree(library, "01动画_文件目录.txt", ["Show/Show.S01E01.mkv"])

    resolution = TreePlaybackRootResolver(tree, configured_roots=[mount]).resolve()

    assert resolution.ok is False
    assert resolution.root == ""
    assert resolution.reason


def test_ambiguous_multiple_all_hit_roots_are_rejected(tmp_path):
    mount = tmp_path / "百度网盘"
    sub_a = mount / "01动画"
    sub_b = mount / "02动画"
    # 两个候选根下都有同一相对路径的视频 → 歧义
    _make_show(sub_a / "Show")
    _make_show(sub_b / "Show")
    tree = _write_tree(tmp_path / "清单", "动画_文件目录.txt", ["Show/Show.S01E01.mkv"])

    resolution = TreePlaybackRootResolver(
        tree,
        configured_roots=[sub_a, sub_b],
        extra_candidates=[],
    ).resolve()

    assert resolution.ok is False
    assert "多个候选根" in resolution.reason


def test_head_middle_tail_sampling_covers_large_trees(tmp_path):
    mount = tmp_path / "百度网盘"
    library = mount / "动画"
    lines = []
    for index in range(60):
        media = _make_show(library / "Show", f"Show.S01E{index + 1:02d}.mkv")
        lines.append(str(media.relative_to(library)).replace("\\", "/"))
    tree = _write_tree(tmp_path / "清单", "动画_文件目录.txt", lines)

    resolution = TreePlaybackRootResolver(tree, configured_roots=[mount]).resolve()

    assert resolution.ok is True
    assert Path(resolution.root) == library
    assert resolution.total == 3  # 头/中/尾
    assert resolution.hits == 3


def test_hybrid_route_local_root_is_accepted_without_duplicate_layer(tmp_path):
    mount = tmp_path / "OpenList"
    local_root = mount / "Anime" / "TV"
    media = _make_show(local_root / "Show")
    tree = _write_tree(tmp_path / "清单", "anime-tree.txt", ["Show/Show.S01E01.mkv"])

    resolution = TreePlaybackRootResolver(tree, configured_roots=[local_root]).resolve()

    assert resolution.ok is True
    assert Path(resolution.root) == local_root
    assert media.exists()


def test_scan_metadata_round_trip_and_consume(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.media_v4.sources.tree_root.get_data_dir",
        lambda: tmp_path,
    )
    scan_id = "scan-meta-1"
    metadata = {"ok": True, "root": str(tmp_path / "root")}

    assert load_scan_metadata(scan_id) is None
    stage_scan_metadata(scan_id, metadata)
    assert load_scan_metadata(scan_id) == metadata
    consume_scan_metadata(scan_id)
    assert load_scan_metadata(scan_id) is None
