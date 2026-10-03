"""启动与定时只复用追更调度入口，默认不启用，配置即时生效。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


def test_scheduler_defaults_to_no_updates_and_honors_startup_interval(tmp_path, monkeypatch):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.tracking import scheduler

    database = V4Database(tmp_path / "schedule.db")
    database.initialize()
    config = SimpleNamespace(ongoing_update_on_startup=False, ongoing_update_interval_minutes=0)
    monkeypatch.setattr(scheduler, "load_config", lambda: config)
    calls = []
    monkeypatch.setattr(scheduler, "trigger_updates", lambda database: calls.append(database) or {})
    instance = scheduler.OngoingScheduler(database)
    now = datetime(2026, 10, 3, tzinfo=UTC)
    instance.tick(startup=True, now=now)
    instance.tick(now=now + timedelta(hours=2))
    assert calls == []
    config.ongoing_update_on_startup = True
    config.ongoing_update_interval_minutes = 60
    instance.tick(startup=True, now=now + timedelta(hours=3))
    assert len(calls) == 1
    instance.tick(now=now + timedelta(hours=3, minutes=59))
    assert len(calls) == 1
    instance.tick(now=now + timedelta(hours=4))
    assert len(calls) == 2
    config.ongoing_update_interval_minutes = 0
    instance.tick(now=now + timedelta(days=1))
    assert len(calls) == 2


def test_restart_does_not_repeat_the_same_timer_window(tmp_path, monkeypatch):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.tracking import scheduler

    database = V4Database(tmp_path / "schedule.db")
    database.initialize()
    monkeypatch.setattr(scheduler, "load_config", lambda: SimpleNamespace(
        ongoing_update_on_startup=False, ongoing_update_interval_minutes=60))
    calls = []
    monkeypatch.setattr(scheduler, "trigger_updates", lambda database: calls.append(database) or {})
    now = datetime(2026, 10, 3, tzinfo=UTC)
    scheduler.OngoingScheduler(database).tick(now=now)
    scheduler.OngoingScheduler(database).tick(now=now + timedelta(minutes=30))
    assert len(calls) == 1


def test_update_configuration_preserves_legacy_defaults_and_rejects_tight_intervals():
    import pytest
    from pydantic import ValidationError

    from app.api.config import ConfigPatch
    from app.core.config import AppConfig

    config = AppConfig()
    assert config.ongoing_update_on_startup is False
    assert config.ongoing_update_interval_minutes == 0
    with pytest.raises(ValidationError):
        ConfigPatch(ongoing_update_interval_minutes=1)
    assert ConfigPatch(ongoing_update_interval_minutes=60).ongoing_update_interval_minutes == 60
