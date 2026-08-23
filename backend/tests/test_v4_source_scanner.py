"""V4 source scan adapter contract。"""

from __future__ import annotations


def test_local_scan_emits_only_source_evidence(tmp_path):
    from app.media_v4.sources.scanner import scan_local_directory

    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "Show.S01E01.mkv").write_bytes(b"video")
    (tmp_path / "Show" / "poster.jpg").write_bytes(b"image")

    root_id, scan_id, evidence = scan_local_directory(tmp_path)

    assert root_id.startswith("root_")
    assert scan_id.startswith("scan_")
    assert len(evidence) == 1
    assert evidence[0].relative_path == "Show/Show.S01E01.mkv"
    assert evidence[0].source_locator.endswith("Show\\Show.S01E01.mkv")


def test_directory_tree_adapter_preserves_remote_path_without_parsing_identity(tmp_path):
    from app.media_v4.sources.scanner import parse_directory_tree_file

    tree = tmp_path / "tree.txt"
    tree.write_text("Anime/Show/Show.S01E01.mkv\nAnime/Show/poster.jpg\n", encoding="utf-8")

    scan_id, evidence = parse_directory_tree_file(
        tree,
        root_id="root-remote",
        provider="pan115",
    )

    assert scan_id.startswith("scan_")
    assert len(evidence) == 1
    assert evidence[0].provider == "pan115"
    assert evidence[0].relative_path == "Anime/Show/Show.S01E01.mkv"
