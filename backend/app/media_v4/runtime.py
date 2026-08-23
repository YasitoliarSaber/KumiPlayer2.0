"""V4 application runtime ownership."""

from __future__ import annotations

from functools import lru_cache

from app.core.paths import get_data_dir
from app.media_v4.persistence.database import V4Database


@lru_cache(maxsize=1)
def get_database() -> V4Database:
    database = V4Database(get_data_dir() / "kumiplayer.db")
    database.initialize()
    return database


def initialize_runtime() -> V4Database:
    """Initialize the only media-state authority used by the application."""

    return get_database()
