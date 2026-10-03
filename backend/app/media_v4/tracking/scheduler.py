"""可选的启动/定时更新，只登记现有持久追更任务。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import load_config
from app.media_v4.persistence.database import V4Database


def trigger_updates(database: V4Database):
    from app.media_v4.tracking.refresh import refresh_all_sources

    return refresh_all_sources(database)


class OngoingScheduler:
    def __init__(self, database: V4Database):
        self.database = database
        self.startup_checked = False

    def tick(self, *, startup: bool = False, now: datetime | None = None) -> None:
        config = load_config()
        stamp = now or datetime.now(UTC)
        start = startup and not self.startup_checked and config.ongoing_update_on_startup
        if startup:
            self.startup_checked = True
        interval = int(config.ongoing_update_interval_minutes or 0)
        if not start and not 15 <= interval <= 10080:
            return
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM v4_meta WHERE key='ongoing_last_scheduled_at'").fetchone()
            try:
                previous = datetime.fromisoformat(row[0]) if row else None
            except (ValueError, TypeError):
                previous = None
            if not start and previous is not None and stamp < previous + timedelta(minutes=interval):
                return
            conn.execute("INSERT INTO v4_meta(key,value) VALUES ('ongoing_last_scheduled_at',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (stamp.isoformat(),))
        trigger_updates(self.database)
