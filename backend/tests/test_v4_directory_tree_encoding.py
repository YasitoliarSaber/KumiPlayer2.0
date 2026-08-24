"""P-007 目录树 TXT 编码兼容：单次读取、BOM 优先严格解码、内容校验。

旧实现只支持 utf-8-sig / gb18030，UTF-16 BOM 目录树会抛
`'gb18030' codec can't decode byte 0xff`。本测试锁定产品级编码矩阵。
"""

from __future__ import annotations

import pytest

from app.media_v4.sources.scanner import (
    DirectoryTreeReadError,
    build_directory_tree_evidence,
    parse_directory_tree_file,
    read_directory_tree_text,
)

_SIMPLE_LINES = [
    "Show (2024)/Season 1/Show.S01E01.mkv",
    "Show (2024)/Season 1/Show.S01E02.mkv",
    "Show (2024)/movie/Show.movie.mkv",
]

_UNICODE_TREE = (
    "├── 动画\n"
    "│   ├── 摇曳露营\n"
    "│   │   ├── Season 1\n"
    "│   │   │   ├── 摇曳露营.S01E01.mkv\n"
    "│   │   │   └── 摇曳露营.S01E02.mkv\n"
)


@pytest.mark.parametrize("encoding", [
    "utf-8",
    "utf-8-sig",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "gb18030",
])
def test_simple_relative_path_tree_is_identical_across_encodings(tmp_path, encoding):
    tree = tmp_path / "tree.txt"
    tree.write_text("\n".join(_SIMPLE_LINES), encoding=encoding)

    _scan_id, evidence = parse_directory_tree_file(
        tree,
        root_id="root-enc",
        provider="pan115",
        source_root=str(tmp_path / "mount"),
    )

    assert [item.relative_path for item in evidence] == _SIMPLE_LINES
    assert [item.playback_locator for item in evidence] == [
        str(tmp_path / "mount" / line) for line in _SIMPLE_LINES
    ]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "gb18030"])
def test_unicode_tree_parent_hierarchy_survives_encoding(tmp_path, encoding):
    tree = tmp_path / "baidu.txt"
    tree.write_text(_UNICODE_TREE, encoding=encoding)

    _scan_id, evidence = parse_directory_tree_file(
        tree,
        root_id="root-baidu-enc",
        provider="baidu",
    )

    assert [item.relative_path for item in evidence] == [
        "动画/摇曳露营/Season 1/摇曳露营.S01E01.mkv",
        "动画/摇曳露营/Season 1/摇曳露营.S01E02.mkv",
    ]


def test_plain_path_list_keeps_the_selected_provider(tmp_path):
    tree = tmp_path / "quark.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")

    _scan_id, evidence = parse_directory_tree_file(
        tree,
        root_id="root-quark",
        provider="quark",
    )

    assert evidence[0].provider == "quark"


def test_utf16_bom_decodes_before_any_fallback(tmp_path):
    tree = tmp_path / "utf16.txt"
    tree.write_text("\n".join(_SIMPLE_LINES), encoding="utf-16")

    text = read_directory_tree_text(tree)

    assert text.splitlines() == _SIMPLE_LINES


def test_virtual_volume_tree_does_not_require_resolve_or_stat(tmp_path, monkeypatch):
    """WinFSP/FUSE 虚拟卷不实现最终路径解析时仍可读取 UTF-16 目录树。"""

    tree = tmp_path / "virtual.txt"
    tree.write_text("\n".join(_SIMPLE_LINES), encoding="utf-16")
    from pathlib import Path

    def fail_resolve(*_args, **_kwargs):
        raise OSError("WinError 1005: 虚拟卷不支持最终路径解析")

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    monkeypatch.setattr(Path, "is_file", fail_resolve)
    monkeypatch.setattr(Path, "stat", fail_resolve)

    _scan_id, evidence = parse_directory_tree_file(
        tree,
        root_id="root-virtual",
        provider="pan115",
    )

    assert len(evidence) == 3


def test_tree_file_is_opened_exactly_once(tmp_path, monkeypatch):
    import builtins

    from app.media_v4.sources import scanner

    tree = tmp_path / "once.txt"
    tree.write_text("\n".join(_SIMPLE_LINES), encoding="utf-16")
    counter = {"opens": 0}
    real_open = builtins.open

    def counting_open(*args, **kwargs):
        counter["opens"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(scanner.builtins, "open", counting_open)

    read_directory_tree_text(tree)

    assert counter["opens"] == 1


def test_empty_tree_raises_stable_chinese_error(tmp_path):
    tree = tmp_path / "empty.txt"
    tree.write_bytes(b"")

    with pytest.raises(DirectoryTreeReadError) as exc_info:
        read_directory_tree_text(tree)

    assert exc_info.value.kind == "empty"
    assert "为空" in str(exc_info.value)


def test_random_binary_reports_not_a_tree_or_encoding_without_codec_leak(tmp_path):
    tree = tmp_path / "binary.bin"
    tree.write_bytes(bytes(range(256)))

    with pytest.raises(DirectoryTreeReadError) as exc_info:
        read_directory_tree_text(tree)

    message = str(exc_info.value)
    assert "codec can't decode" not in message
    assert "UnicodeDecodeError" not in message
    assert exc_info.value.kind in {"not_a_tree", "encoding"}


def test_truncated_utf16_bom_is_rejected_strictly(tmp_path):
    tree = tmp_path / "truncated.txt"
    tree.write_bytes(b"\xff\xfeS\x00h")

    with pytest.raises(DirectoryTreeReadError) as exc_info:
        read_directory_tree_text(tree)

    assert "codec can't decode" not in str(exc_info.value)


def test_oversized_tree_is_rejected(tmp_path, monkeypatch):
    tree = tmp_path / "big.txt"
    tree.write_bytes(b"\xff\xfe" + b"\x00\x00" * (64 * 1024 * 1024 // 2))

    with pytest.raises(DirectoryTreeReadError) as exc_info:
        read_directory_tree_text(tree)

    assert exc_info.value.kind == "too_large"


def test_build_evidence_reuses_predecoded_text_without_second_read(tmp_path, monkeypatch):
    import builtins

    from app.media_v4.sources import scanner

    text = "\n".join(_SIMPLE_LINES)
    counter = {"opens": 0}
    real_open = builtins.open

    def counting_open(*args, **kwargs):
        counter["opens"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(scanner.builtins, "open", counting_open)

    _scan_id, evidence = build_directory_tree_evidence(
        text,
        root_id="root-reuse",
        provider="baidu",
        source_root=str(tmp_path / "mount"),
    )

    assert counter["opens"] == 0
    assert len(evidence) == 3
