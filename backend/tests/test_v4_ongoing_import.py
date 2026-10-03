"""显式追更来源共用 V4；少量文件也只能产生正片剧集。"""

from dataclasses import replace

import pytest

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.parser import V4Parser


@pytest.mark.parametrize("filename", ["Show.S01E01.mkv", "Show.SP01.mkv", "Show.OVA01.mkv", "Show.Movie.01.mkv",
                                       "Show - 01.mkv", "01.mkv", "Show 第01集.mkv"])
def test_explicit_ongoing_scope_only_produces_regular_tv(filename):
    from app.media_v4.tracking.ongoing import parse_for_scope

    item = SourceEvidence(evidence_id="ev", scan_id="scan", root_id="root", source_key=filename,
                          relative_path="Show/" + filename, entry_kind="video")
    facts = parse_for_scope(V4Parser(), item, content_scope="ongoing")
    assert facts.media_type == "tv"
    assert facts.group_type == "season"
    assert facts.content_class == "regular"
    assert facts.special_candidate is False
    assert facts.special_number is None
    assert facts.is_importable is True
    assert facts.season_candidate == 1
    assert facts.episode_candidate == 1


def test_ongoing_scope_does_not_fabricate_an_unknown_episode_number():
    from app.media_v4.tracking.ongoing import parse_for_scope

    item = SourceEvidence(evidence_id="ev", scan_id="scan", root_id="root", source_key="Show.mkv",
                          relative_path="Show/Show.mkv", entry_kind="video")
    facts = parse_for_scope(V4Parser(), item, content_scope="ongoing")
    assert facts.episode_candidate is None
    assert facts.needs_review
    completed = parse_for_scope(V4Parser(), replace(item, relative_path="Show/Show.SP01.mkv"), content_scope="completed")
    assert completed.special_candidate


def test_ongoing_does_not_import_non_video_and_keeps_numbering_trace_consistent():
    from app.media_v4.tracking.ongoing import parse_for_scope

    item = SourceEvidence(evidence_id="ev", scan_id="scan", root_id="root", source_key="Show.nfo",
                          relative_path="Show/Show.S01E01.nfo", entry_kind="nfo")
    assert not parse_for_scope(V4Parser(), item, content_scope="ongoing").is_importable
    facts = parse_for_scope(V4Parser(), replace(item, entry_kind="video", relative_path="Show/Show.SP01.mkv"),
                            content_scope="ongoing")
    trace = next(row for row in facts.decision_trace if row.field == "numbering")
    assert trace.value["season"] == facts.season_candidate
    assert trace.value["episode"] == facts.episode_candidate
    assert facts.numbering.episode_origin != "unknown"


def test_remote_top_root_conflicts_only_in_the_same_account_namespace(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.tracking.ongoing import set_source_scope

    database = V4Database(tmp_path / "remote.db")
    database.initialize()
    with database.connect() as conn:
        for root_id, locator in [("finished", "/"), ("nested", "/Anime")]:
            conn.execute("INSERT INTO source_roots(root_id,provider,ingest_method,source_locator,created_at,updated_at) "
                         "VALUES (?,'baidu','openlist_scan',?,'now','now')", (root_id, locator))
    set_source_scope(database, "finished", "completed")
    with pytest.raises(ValueError, match="独立"):
        set_source_scope(database, "nested", "ongoing")


@pytest.mark.parametrize("filename", ["PV01.mkv", "NCOP01.mkv", "OP01.mkv", "Trailer01.mkv"])
def test_ongoing_source_still_excludes_auxiliary_videos(filename):
    from app.media_v4.tracking.ongoing import parse_for_scope

    item = SourceEvidence(evidence_id="ev", scan_id="scan", root_id="root", source_key=filename,
                          relative_path="Show/" + filename, entry_kind="video")
    facts = parse_for_scope(V4Parser(), item, content_scope="ongoing")
    assert facts.is_auxiliary
    assert not facts.is_importable


def test_missing_legacy_location_is_not_a_claim_to_the_entire_remote_root(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.tracking.ongoing import set_source_scope

    database = V4Database(tmp_path / "unknown.db")
    database.initialize()
    with database.connect() as conn:
        for root_id, locator in [("legacy", ""), ("ongoing", "/Ongoing")]:
            conn.execute("INSERT INTO source_roots(root_id,provider,ingest_method,source_locator,created_at,updated_at) "
                         "VALUES (?,'baidu','openlist_scan',?,'now','now')", (root_id, locator))
    set_source_scope(database, "ongoing", "ongoing")


def test_ongoing_must_have_a_separate_folder(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.tracking.ongoing import set_source_scope

    database = V4Database(tmp_path / "scope.db")
    database.initialize()
    with database.connect() as conn:
        for root_id, path in [("completed", "X:/offline/Finished"), ("nested", "X:/offline/Finished/New"),
                              ("sibling", "X:/offline/Ongoing")]:
            conn.execute("INSERT INTO source_roots(root_id,provider,ingest_method,source_locator,"
                         "playback_locator,created_at,updated_at) VALUES (?,'local','local_scan',?,?,'now','now')",
                         (root_id, path, path))
    set_source_scope(database, "completed", "completed")
    with database.connect() as conn:
        conn.execute("UPDATE source_roots SET enabled=0 WHERE root_id='completed'")
    with pytest.raises(ValueError, match="独立"):
        set_source_scope(database, "nested", "ongoing")
    set_source_scope(database, "sibling", "ongoing")
    with database.connect() as conn:
        assert conn.execute("SELECT content_scope FROM source_roots WHERE root_id='sibling'").fetchone()[0] == "ongoing"


@pytest.mark.parametrize("new_name,expected", [("Show.S01E02.mkv", "append"),
                                              ("Show.S01E01.mkv", "unchanged"),
                                              ("Other.S01E02.mkv", "review"),
                                              ("Show.S02E01.mkv", "review"),
                                              ("Show.mkv", "review")])
def test_only_known_unambiguous_regular_episode_append_can_auto_confirm(tmp_path, new_name, expected):
    import json

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService
    from app.media_v4.tracking.ongoing import assess_update, parse_for_scope

    database = V4Database(tmp_path / "append.db")
    database.initialize()
    service = V4RevisionService(database)
    def item(name, scan):
        path = ("Other/" if name.startswith("Other.") else "Show/") + name
        return SourceEvidence(evidence_id=scan + name, scan_id=scan, root_id="ongoing", source_key=path,
                              relative_path=path, entry_kind="video", source_locator="X:/offline/" + path,
                              playback_locator="X:/offline/" + path)
    old = item("Show.S01E01.mkv", "first")
    service.create_draft("first", [(old, parse_for_scope(V4Parser(), old, content_scope="ongoing"))])
    service.confirm("first")
    with database.connect() as conn:
        conn.execute("UPDATE source_roots SET content_scope='ongoing' WHERE root_id='ongoing'")
        work_id = conn.execute("SELECT work_id FROM works").fetchone()[0]
        conn.execute("INSERT INTO provider_bindings(work_id,provider,media_type,provider_id) VALUES (?,'tmdb','tv','123')",
                     (work_id,))
        conn.execute("INSERT INTO scrape_bindings(binding_id,revision_id,work_id,provider,provider_id,status,"
                     "metadata_json,created_at,updated_at) VALUES ('binding','first',?,'tmdb','123','confirmed',?,'now','now')",
                     (work_id, json.dumps({"metadata_state": "ready"})))
    current = [item("Show.S01E01.mkv", "next")]
    if new_name != "Show.S01E01.mkv":
        current.append(item(new_name, "next"))
    service.create_draft("next", [(row, parse_for_scope(V4Parser(), row, content_scope="ongoing")) for row in current])
    assert assess_update(database, "next") == expected
    assert service.get_status("next") == "draft"


def test_v24_scope_migration_preserves_facts_and_rolls_back_on_validation_failure(tmp_path, monkeypatch):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "v24.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute("INSERT INTO source_roots(root_id,provider,ingest_method,created_at,updated_at) "
                     "VALUES ('legacy','local','local_scan','before','before')")
        conn.execute("ALTER TABLE source_roots DROP COLUMN content_scope")
        conn.execute("PRAGMA user_version = 24")
        original = tuple(conn.execute("SELECT * FROM source_roots").fetchone())
    validate = database._validate_physical_schema
    monkeypatch.setattr(database, "_validate_physical_schema", lambda conn: (_ for _ in ()).throw(ValueError("fixture")))
    with pytest.raises(ValueError, match="fixture"):
        database.initialize()
    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 24
        assert "content_scope" not in {row[1] for row in conn.execute("PRAGMA table_info(source_roots)")}
        assert tuple(conn.execute("SELECT * FROM source_roots").fetchone()) == original
    monkeypatch.setattr(database, "_validate_physical_schema", validate)
    database.initialize()
    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
        row = conn.execute("SELECT * FROM source_roots").fetchone()
        assert tuple(row)[:-1] == original
        assert row["content_scope"] == ""
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
