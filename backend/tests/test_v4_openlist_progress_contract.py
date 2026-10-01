from app.media_v4.persistence.database import V4Database
from app.media_v4.sources import scan_frontier
from app.media_v4.sources.durable_scan import _update_scan_progress, get_durable_scan


def test_remote_discovery_never_turns_discovered_count_into_total(tmp_path):
    database = V4Database(tmp_path / "progress.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute("INSERT INTO source_roots(root_id,provider,ingest_method,source_locator,playback_locator,created_at,updated_at) VALUES ('r','quark','openlist_api','/Anime','','now','now')")
        conn.execute("INSERT INTO source_scans(scan_id,root_id,generation,status,stage,started_at) VALUES ('s','r',1,'running','reading_source','now')")
    scan_frontier.ensure_directories(database, scan_id="s", remote_paths=["/Anime/Show", "/Anime/Other"])
    scan_frontier.next_pending_directory(database, scan_id="s")
    _update_scan_progress(database, "s", stage="reading_source", processed_count=3472, total_count=0)
    status = get_durable_scan(database, "s", include_entries=False)
    assert status["total_count"] == 0
    assert status["progress"] is None
    assert status["discovered_count"] == 3472
    assert status["directories_pending"] == 2
    assert status["current_directory"] in {"Show", "Other"}
    _update_scan_progress(database, "s", stage="parsing", processed_count=16, total_count=3472)
    assert get_durable_scan(database, "s", include_entries=False)["progress"] == 16 / 3472
    _update_scan_progress(database, "s", stage="preparing_preview", processed_count=3472, total_count=3472)
    assert get_durable_scan(database, "s", include_entries=False)["progress"] is None
