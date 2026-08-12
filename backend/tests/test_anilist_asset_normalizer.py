# -*- coding: utf-8 -*-
"""AniList artwork normalization tests."""

import sys
from pathlib import Path

scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))
from repair.normalize_anilist_assets import normalize_anilist_assets


def test_normalize_anilist_assets_uses_tmdb_canonical_names(tmp_path):
    work_dir = tmp_path / "摇曳露营"
    work_dir.mkdir()
    (work_dir / "coverImage.jpg").write_bytes(b"poster")
    (work_dir / "bannerImage.png").write_bytes(b"fanart")

    results = normalize_anilist_assets(tmp_path)

    assert (work_dir / "poster.jpg").read_bytes() == b"poster"
    assert (work_dir / "fanart.png").read_bytes() == b"fanart"
    assert not (work_dir / "coverImage.jpg").exists()
    assert not (work_dir / "bannerImage.png").exists()
    assert {result["target_name"] for result in results} == {"poster.jpg", "fanart.png"}


def test_normalize_anilist_assets_does_not_overwrite_existing_tmdb_assets(tmp_path):
    work_dir = tmp_path / "莉可丽丝"
    work_dir.mkdir()
    (work_dir / "poster.jpg").write_bytes(b"tmdb-poster")
    (work_dir / "anilist-cover.jpg").write_bytes(b"anilist-poster")
    (work_dir / "fanart.jpg").write_bytes(b"tmdb-fanart")
    (work_dir / "anilist-banner.jpg").write_bytes(b"anilist-fanart")

    results = normalize_anilist_assets(tmp_path)

    assert results == []
    assert (work_dir / "poster.jpg").read_bytes() == b"tmdb-poster"
    assert (work_dir / "fanart.jpg").read_bytes() == b"tmdb-fanart"
    assert (work_dir / "anilist-cover.jpg").exists()
    assert (work_dir / "anilist-banner.jpg").exists()
