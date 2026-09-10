"""V4 来源根与 durable scan 登记的原子发布合同。"""

from __future__ import annotations

import pytest


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "scan-registration.db")
    database.initialize()
    return database


def _root_spec():
    return {
        "root_id": "root-registration",
        "provider": "openlist",
        "ingest_method": "openlist_scan",
        "source_locator": "/Anime",
        "playback_locator": "K:\\OpenList\\Anime",
        "route_id": "route-anime",
        "root_container": "Anime",
        "source_mode": "openlist_full",
        "last_scan_mode": "full",
    }


def _scan_spec():
    return {
        "scan_id": "scan-registration",
        "root_id": "root-registration",
        "scan_kind": "openlist_full",
        "source_mode": "openlist_full",
        "request": {"remote_root": "/Anime"},
    }


def test_durable_registration_rolls_back_root_when_scan_insert_fails(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.sources import source_scan_runner

    database = _database(tmp_path)

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("登记失败")

    monkeypatch.setattr(source_scan_runner, "register_source_scan", fail_registration)

    with pytest.raises(RuntimeError, match="登记失败"):
        media_v4._register_durable_source_scan(
            database,
            root=_root_spec(),
            scan=_scan_spec(),
        )

    with database.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM source_roots WHERE root_id = 'root-registration'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM source_scans WHERE scan_id = 'scan-registration'"
        ).fetchone() is None


def test_durable_registration_publishes_separate_source_and_playback_locators(tmp_path):
    from app.api import media_v4

    database = _database(tmp_path)
    media_v4._register_durable_source_scan(
        database,
        root=_root_spec(),
        scan=_scan_spec(),
    )

    with database.connect() as conn:
        root = conn.execute(
            "SELECT source_locator, playback_locator FROM source_roots "
            "WHERE root_id = 'root-registration'"
        ).fetchone()
        scan = conn.execute(
            "SELECT root_id, status FROM source_scans WHERE scan_id = 'scan-registration'"
        ).fetchone()
    assert tuple(root) == ("/Anime", "K:\\OpenList\\Anime")
    assert tuple(scan) == ("root-registration", "queued")
