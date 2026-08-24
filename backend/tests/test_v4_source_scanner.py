"""V4 source scan adapter contract。"""

from __future__ import annotations

import pytest


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


def test_local_scan_rejects_a_configured_cloud_mount_even_when_it_reports_ntfs(tmp_path, monkeypatch):
    from app.media_v4.sources import scanner

    monkeypatch.setattr(scanner, "_windows_volume_profile", lambda _path: ("fixed", "NTFS"))

    with pytest.raises(ValueError, match="网盘挂载"):
        scanner.scan_local_directory(tmp_path, excluded_roots=[tmp_path])


def test_tree_root_identity_separates_libraries_but_ignores_export_timestamps():
    from app.media_v4.sources.scanner import tree_root_id

    first = tree_root_id("baidu", "K:\\百度网盘", "动画_文件目录_20260810194440.txt")
    refreshed = tree_root_id("baidu", "K:\\百度网盘", "动画_文件目录_20260824120000.txt")
    movies = tree_root_id("baidu", "K:\\百度网盘", "电影_文件目录_20260824120000.txt")

    assert first == refreshed
    assert first != movies


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


def test_unicode_tree_adapter_reconstructs_parent_directories_and_playback_path(tmp_path):
    from app.media_v4.sources.scanner import parse_directory_tree_file

    tree = tmp_path / "tree.txt"
    tree.write_text(
        "├── 动画\n"
        "│   ├── Show (2024) {tmdb-42}\n"
        "│   │   ├── Season 1\n"
        "│   │   │   ├── Show.S01E01.2160p.mkv\n",
        encoding="utf-8",
    )
    mount = tmp_path / "mounted"

    _scan_id, evidence = parse_directory_tree_file(
        tree,
        root_id="root-baidu",
        provider="baidu",
        source_root=str(mount),
    )

    assert [item.relative_path for item in evidence] == [
        "动画/Show (2024) {tmdb-42}/Season 1/Show.S01E01.2160p.mkv"
    ]
    assert evidence[0].playback_locator == str(
        mount / "动画" / "Show (2024) {tmdb-42}" / "Season 1" / "Show.S01E01.2160p.mkv"
    )


def test_115_ascii_tree_adapter_reconstructs_parent_directories(tmp_path):
    from app.media_v4.sources.scanner import parse_directory_tree_file

    tree = tmp_path / "115.txt"
    tree.write_text(
        "|——动画\n"
        "| |-Show\n"
        "| | |-Season 1\n"
        "| | | |-Show.S01E01.mkv\n",
        encoding="utf-8",
    )

    _scan_id, evidence = parse_directory_tree_file(
        tree,
        root_id="root-115",
        provider="pan115",
    )

    assert [item.relative_path for item in evidence] == ["Show/Season 1/Show.S01E01.mkv"]
    assert evidence[0].source_locator == "Show/Season 1/Show.S01E01.mkv"
    assert evidence[0].playback_locator == "Show/Season 1/Show.S01E01.mkv"


def test_openlist_scan_recursively_emits_the_same_source_evidence_contract(tmp_path):
    from app.integrations.openlist.models import OpenListDirPage, OpenListEntry
    from app.media_v4.sources.scanner import scan_openlist_directory

    class FakeClient:
        def list_dir(self, path, page=1, per_page=100, refresh=False):
            del page, per_page, refresh
            entries = {
                "/Anime": [OpenListEntry(name="Show", is_dir=True, remote_path="/Anime/Show")],
                "/Anime/Show": [
                    OpenListEntry(name="Season 1", is_dir=True, remote_path="/Anime/Show/Season 1")
                ],
                "/Anime/Show/Season 1": [
                    OpenListEntry(
                        name="Show.S01E01.mkv",
                        is_dir=False,
                        size=123,
                        remote_path="/Anime/Show/Season 1/Show.S01E01.mkv",
                    )
                ],
            }
            return OpenListDirPage(entries=entries[path], total=len(entries[path]))

    mount = tmp_path / "mount"
    directories = {}
    _scan_id, evidence = scan_openlist_directory(
        FakeClient(),
        remote_root="/Anime",
        mapping_root="/",
        mount_root=str(mount),
        root_id="root-openlist",
        default_provider="openlist",
        directory_observations=directories,
    )

    assert [item.relative_path for item in evidence] == ["Show/Season 1/Show.S01E01.mkv"]
    assert evidence[0].ingest_method == "openlist_api"
    assert evidence[0].source_locator == "/Anime/Show/Season 1/Show.S01E01.mkv"
    assert evidence[0].playback_locator == str(
        mount / "Anime" / "Show" / "Season 1" / "Show.S01E01.mkv"
    )
    assert set(directories) == {"", "Show", "Show/Season 1"}
