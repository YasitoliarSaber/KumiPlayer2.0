"""Shared scrape-output completeness checks."""

import logging
import re
from pathlib import Path

from app.import_plan.models import ImportPlan
from app.scrape.models import ScrapeTarget

logger = logging.getLogger(__name__)


def target_already_scraped(
    target: ScrapeTarget,
    scrape_index: dict[str, object],
    include_episode: bool = True,
    plan: ImportPlan | None = None,
) -> bool:
    """Return whether every output required by one scrape target is usable."""
    item = scrape_index.get(target.scrape_target_id)
    if not item or not getattr(item, "tmdb_id", None):
        return False
    if _has_stale_plain_series_season_mapping(target, item):
        return False

    nfo_candidates = [
        getattr(item, "nfo_path", ""),
        target.target_nfo_path,
        str(Path(target.target_dir) / ("movie.nfo" if target.scrape_type == "movie" else "tvshow.nfo")),
    ]
    if not any(Path(path).is_file() for path in nfo_candidates if path):
        return False
    if not required_artwork_complete(target, item):
        return False
    if _needs_episode_nfo_repair(target, include_episode, plan):
        return False
    return True


def required_artwork_complete(target: ScrapeTarget, item: object) -> bool:
    """The poster wall and detail page both require poster and fanart."""
    target_dir = Path(target.target_dir) if target.target_dir else None

    def available(kind: str, filename: str) -> bool:
        values = [
            getattr(item, f"{kind}_path", ""),
            getattr(target, f"target_{kind}_path", ""),
            str(target_dir / filename) if target_dir else "",
        ]
        for value in values:
            if not value:
                continue
            if str(value).lower().startswith(("http://", "https://")):
                return True
            path = Path(value)
            if path.is_file() and path.stat().st_size > 0:
                return True
        return False

    return available("poster", "poster.jpg") and available("fanart", "fanart.jpg")


def _has_stale_plain_series_season_mapping(target: ScrapeTarget, item: object) -> bool:
    local_season = target.local_season_number or 1
    if local_season <= 1:
        return False
    if target.group_type != "season" or target.card_type != "main_series":
        return False
    if (target.source_subwork_dir or "").strip():
        return False
    if not target.tmdb_hint_id or getattr(item, "tmdb_id", None) != target.tmdb_hint_id:
        return False
    if getattr(item, "tmdb_season_number", None) != 1:
        return False

    series_key = _plain_title_key(target.series_group)
    if not series_key:
        return False
    local_key = _plain_title_key(target.local_title or target.scrape_title)
    scrape_key = _plain_title_key(target.scrape_title or target.local_title)
    return (not local_key or local_key == series_key) and (not scrape_key or scrape_key == series_key)


def _plain_title_key(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _needs_episode_nfo_repair(
    target: ScrapeTarget,
    include_episode: bool,
    plan: ImportPlan | None,
) -> bool:
    if not include_episode:
        return False
    if target.scrape_type != "tv" or target.group_type not in {"season", "special"}:
        return False
    if not target.item_ids:
        return False

    target_dir = Path(target.target_dir)
    if not target_dir.is_dir():
        return True

    existing = [
        path for path in target_dir.glob("S??E*.nfo")
        if path.name.lower() != "tvshow.nfo"
    ]
    expected_names = _expected_episode_nfo_names(target, plan)
    if expected_names:
        existing_names = {path.name.casefold() for path in existing}
        return not expected_names.issubset(existing_names)
    return len(existing) < len(target.item_ids)


def _expected_episode_nfo_names(
    target: ScrapeTarget,
    plan: ImportPlan | None,
) -> set[str]:
    if plan is None:
        try:
            from app.import_plan.store import load_import_plan

            plan = load_import_plan(plan_id=target.import_plan_id)
        except Exception:
            logger.debug("load import plan for episode repair failed", exc_info=True)
            return set()
    if not plan:
        return set()

    item_ids = set(target.item_ids)
    expected: set[str] = set()
    for item in plan.items:
        if item.id not in item_ids or item.group_type not in {"season", "special"}:
            continue
        season = item.season_number
        if season is None:
            season = target.local_season_number
        episode = item.episode_number
        if episode is None:
            episode = item.special_number
        if season is None or episode is None:
            continue
        expected.add(f"s{int(season):02d}e{int(episode):02d}.nfo")
    return expected
