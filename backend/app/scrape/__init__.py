# -*- coding: utf-8 -*-
"""刮削包"""

from app.scrape.models import ScrapeTarget, ScrapeCandidate, ScrapeMapItem, ScrapeMap
from app.scrape.target_builder import build_scrape_targets
from app.scrape.store import load_scrape_map, save_scrape_map, upsert_scrape_map_item
from app.scrape.tmdb_client import TMDBClient
from app.scrape.nfo import generate_tvshow_nfo, generate_movie_nfo
from app.scrape.service import get_targets, search_candidates, execute_scrape

__all__ = [
    "ScrapeTarget", "ScrapeCandidate", "ScrapeMapItem", "ScrapeMap",
    "build_scrape_targets",
    "load_scrape_map", "save_scrape_map", "upsert_scrape_map_item",
    "TMDBClient",
    "generate_tvshow_nfo", "generate_movie_nfo",
    "get_targets", "search_candidates", "execute_scrape",
]
