# -*- coding: utf-8 -*-
"""仅补全缺失分级，不重跑完整媒体刮削。"""

from pathlib import Path
from xml.etree import ElementTree

from app.core.config import load_config
from app.scrape.certification import extract_certification
from app.scrape.store import load_scrape_map
from app.scrape.tmdb_client import TMDBClient


def update_nfo_certification(path: Path, value: str, country: str) -> bool:
    if not path.exists() or not value:
        return False
    tree = ElementTree.parse(path)
    root = tree.getroot()
    current = root.find("mpaa")
    if current is not None and (current.text or "").strip():
        return False
    if current is None:
        current = ElementTree.SubElement(root, "mpaa")
    current.text = value
    region = root.find("certificationcountry")
    if region is None:
        region = ElementTree.SubElement(root, "certificationcountry")
    region.text = country
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return True


def backfill_missing_certifications(progress_callback=None) -> dict:
    config = load_config()
    regions = [part.strip().upper() for part in config.tmdb_certification_regions.split(",") if part.strip()]
    bindings = [item for item in load_scrape_map().items if item.tmdb_id and item.nfo_path]
    checked = updated = skipped = failed = 0
    with TMDBClient() as client:
        for index, item in enumerate(bindings, start=1):
            checked += 1
            try:
                path = Path(item.nfo_path)
                if _nfo_has_certification(path):
                    skipped += 1
                    continue
                detail = client.get_movie_detail(item.tmdb_id) if item.tmdb_type == "movie" else client.get_tv_detail(item.tmdb_id)
                rating = extract_certification(detail, item.tmdb_type or "tv", regions)
                if update_nfo_certification(path, rating.value, rating.country):
                    updated += 1
                else:
                    skipped += 1
            except Exception:
                failed += 1
            finally:
                if progress_callback:
                    progress_callback(int(index * 95 / max(1, len(bindings))), f"检查分级 {index}/{len(bindings)}")

    if updated:
        from app.library.service import rescan_library
        rescan_library(None)
    return {"checked": checked, "updated": updated, "skipped": skipped, "failed": failed}


def _nfo_has_certification(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        value = ElementTree.parse(path).getroot().findtext("mpaa", default="")
        return bool(value.strip())
    except (ElementTree.ParseError, OSError):
        return False
