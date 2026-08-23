"""V4 来源适配器合同。"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_source_entry_adapter_emits_provider_and_ingest_method_separately():
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    entry = SourceEntry(
        root_id="root-1",
        scan_id="scan-1",
        provider="pan115",
        ingest_method="txt_tree",
        relative_path="动画\\Show\\Show.S01E01.mkv",
        source_locator="115://folder/Show/Show.S01E01.mkv",
        raw_file_id="115-file-1",
        size=10,
        mtime=20.0,
    )

    evidence = to_source_evidence(entry)

    assert evidence.provider == "pan115"
    assert evidence.ingest_method == "txt_tree"
    assert evidence.relative_path == "动画/Show/Show.S01E01.mkv"
    assert evidence.source_locator == entry.source_locator
    assert evidence.raw_file_id == "115-file-1"
    assert evidence.evidence_id


def test_source_entry_adapter_keeps_stable_key_and_does_not_parse_media_identity():
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    entry = SourceEntry(
        root_id="root-1",
        scan_id="scan-1",
        provider="local",
        ingest_method="local_scan",
        relative_path="Show/Season 1/Show S01E01.mkv",
        source_key="local-id:show-1",
    )

    first = to_source_evidence(entry)
    second = to_source_evidence(entry)

    assert first == second
    assert first.source_key == "local-id:show-1"
    assert first.fingerprint == ""


def test_quark_remains_a_content_provider_instead_of_becoming_openlist():
    from app.media_v4.sources.adapters import provider_to_source

    assert provider_to_source("quark") == "quark"


def test_local_scan_rejects_a_cloud_mount_presented_as_a_fixed_drive(tmp_path, monkeypatch):
    from app.media_v4.sources import scanner

    monkeypatch.setattr(scanner, "_windows_volume_profile", lambda _path: ("fixed", "FUSE-rclone"))

    with pytest.raises(ValueError, match="网盘挂载或虚拟文件系统"):
        scanner.scan_local_directory(tmp_path)


def test_directory_tree_reader_only_requires_a_readable_file_on_virtual_volumes(tmp_path, monkeypatch):
    """挂载盘可能支持普通读取，但不支持 Windows 最终路径和完整元数据查询。"""

    from app.media_v4.sources.scanner import parse_directory_tree_file

    tree_file = tmp_path / "动画_文件目录_20260810194440.txt"
    tree_file.write_text("作品/作品.S01E01.mkv\n", encoding="utf-8")
    original_resolve = Path.resolve
    original_is_file = Path.is_file

    def reject_final_path(path: Path, *args, **kwargs):
        if path == tree_file:
            raise OSError(1005, "虚拟卷不支持最终路径解析")
        return original_resolve(path, *args, **kwargs)

    def reject_metadata_probe(path: Path):
        if path == tree_file:
            raise OSError(1005, "虚拟卷不支持文件类型探测")
        return original_is_file(path)

    monkeypatch.setattr(Path, "resolve", reject_final_path)
    monkeypatch.setattr(Path, "is_file", reject_metadata_probe)

    scan_id, evidence = parse_directory_tree_file(
        tree_file,
        root_id="root-tree",
        provider="baidu",
    )

    assert scan_id.startswith("scan_")
    assert [item.relative_path for item in evidence] == ["作品/作品.S01E01.mkv"]
