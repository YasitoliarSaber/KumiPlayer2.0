# -*- coding: utf-8 -*-
r"""Normalize AniList artwork filenames to the TMDB/Kodi convention.

Usage:
    python scripts/normalize_anilist_assets.py D:\01_Software\KumiPlayer2.0\data\mirror

The script is intentionally conservative: existing poster/fanart files are
treated as canonical and are never overwritten.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

POSTER_SOURCE_NAMES = {
    "cover",
    "coverimage",
    "anilist-cover",
    "anilist_cover",
    "anilist-poster",
    "anilist_poster",
}
FANART_SOURCE_NAMES = {
    "banner",
    "bannerimage",
    "anilist-banner",
    "anilist_banner",
    "anilist-fanart",
    "anilist_fanart",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def normalize_anilist_assets(root: str | Path, *, dry_run: bool = False) -> list[dict[str, str]]:
    """Rename known AniList artwork names under root to poster/fanart names."""
    root_path = Path(root)
    if not root_path.exists():
        return []

    operations: list[dict[str, str]] = []
    for path in _iter_candidate_images(root_path):
        role = _asset_role(path)
        if not role:
            continue
        target = path.with_name(f"{role}{path.suffix.lower()}")
        if _has_existing_canonical_asset(path.parent, role):
            continue
        operations.append({
            "source": str(path),
            "target": str(target),
            "target_name": target.name,
        })
        if not dry_run:
            path.rename(target)
    return operations


def _iter_candidate_images(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def _asset_role(path: Path) -> str:
    stem = path.stem.casefold()
    if stem in POSTER_SOURCE_NAMES:
        return "poster"
    if stem in FANART_SOURCE_NAMES:
        return "fanart"
    return ""


def _has_existing_canonical_asset(directory: Path, role: str) -> bool:
    for extension in IMAGE_EXTENSIONS:
        if (directory / f"{role}{extension}").exists():
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize AniList artwork filenames.")
    parser.add_argument("root", help="Mirror/library directory to scan")
    parser.add_argument("--dry-run", action="store_true", help="Report operations without renaming")
    args = parser.parse_args()

    operations = normalize_anilist_assets(args.root, dry_run=args.dry_run)
    print(json.dumps(operations, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
