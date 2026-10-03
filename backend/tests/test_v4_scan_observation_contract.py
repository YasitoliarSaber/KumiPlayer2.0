"""未知完整性文本保留基线；继承不是本轮远端观察，事实可追溯。"""

from dataclasses import replace

import pytest

from app.integrations.openlist.models import OpenListDirPage, OpenListEntry
from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.incremental import _clone_for_scan, build_tree_baseline_state, scan_openlist_incremental


def evidence(number, scan="baseline"):
    return SourceEvidence(
        evidence_id=f"{scan}-{number}", scan_id=scan, root_id="root-contract",
        source_key=f"Show/Show.S01E{number:02}.mkv", relative_path=f"Show/Show.S01E{number:02}.mkv",
        entry_kind="video", provider="local", ingest_method="local_scan",
        source_locator=f"X:/offline-fixture/Show/Show.S01E{number:02}.mkv",
        playback_locator=f"X:/offline-fixture/Show/Show.S01E{number:02}.mkv",
        observed_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def database(tmp_path):
    database = V4Database(tmp_path / "contract.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("baseline", [(item, V4Parser().parse(item)) for item in [evidence(1), evidence(2)]])
    service.confirm("baseline")
    return database


def test_inherited_observation_retains_original_time_and_links_parent():
    original = evidence(1)
    inherited = _clone_for_scan(original, "next")
    assert inherited.observed_at == original.observed_at
    assert getattr(inherited, "observation_kind", None) == "inherited"
    assert getattr(inherited, "parent_evidence_id", None) == original.evidence_id


def test_partial_txt_cannot_replace_the_previous_complete_membership(database):
    from app.media_v4.sources.observation_contract import merge_text_snapshot

    current = [replace(evidence(1, "next"), ingest_method="directory_tree")]
    with database.connect() as conn:
        conn.execute("INSERT INTO source_scans(scan_id,root_id,generation,status,started_at) "
                     "VALUES ('next','root-contract',2,'validated','2026-01-02')")
    merged = merge_text_snapshot(database, "root-contract", "next", current)
    assert {item.relative_path for item in merged} == {evidence(1).relative_path, evidence(2).relative_path}
    inherited = next(item for item in merged if item.relative_path == evidence(2).relative_path)
    assert inherited.observation_kind == "inherited"
    assert inherited.parent_evidence_id == evidence(2).evidence_id
    service = V4RevisionService(database)
    service.create_draft("next", [(item, V4Parser().parse(item)) for item in merged])
    service.confirm("next")
    current_members = V4Repository(database).list_confirmed_source_evidence("root-contract")
    assert len(current_members) == 2
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 2
        coverage = conn.execute("SELECT coverage_json FROM source_scans WHERE scan_id='next'").fetchone()[0]
        assert "snapshot_unknown" in coverage


def test_unknown_snapshot_does_not_inherit_another_root(database):
    from app.media_v4.sources.observation_contract import merge_text_snapshot

    other = replace(evidence(1, "other"), root_id="another-root")
    assert merge_text_snapshot(database, "another-root", "other", [other]) == [other]


def test_unvisited_remote_members_are_inherited_not_observed():
    class Client:
        def list_dir(self, path, **_):
            assert path == "/Anime"
            return OpenListDirPage(entries=[OpenListEntry(name="Show", is_dir=True,
                                  remote_path="/Anime/Show")], total=1)

    original = [evidence(1), evidence(2)]
    state = build_tree_baseline_state("root-contract", "/Anime", original)
    _, rows, _, _ = scan_openlist_incremental(Client(), baseline=original, state=state,
        mapping_root="/Anime", mount_root="X:/offline-fixture", scan_id="next", verification_budget=0)
    assert len(rows) == 2
    assert all(getattr(item, "observation_kind", None) == "inherited" for item in rows)
    assert {item.parent_evidence_id for item in rows} == {item.evidence_id for item in original}


def test_malformed_listing_cannot_prove_a_known_file_missing():
    class Client:
        def list_dir(self, path, page=1, **_):
            if path == "/Anime":
                return OpenListDirPage(entries=[OpenListEntry(name="Show", is_dir=True,
                                      remote_path="/Anime/Show")], total=1)
            if page == 1:
                return OpenListDirPage(entries=[OpenListEntry(name="Show.S01E01.mkv",
                    remote_path="/Anime/Show/Show.S01E01.mkv")], total=2, skipped_entries=1)
            return OpenListDirPage(entries=[], total=2)

    original = [evidence(1), evidence(2)]
    state = build_tree_baseline_state("root-contract", "/Anime", original)
    _, rows, _, stats = scan_openlist_incremental(Client(), baseline=original, state=state,
        mapping_root="/Anime", mount_root="X:/offline-fixture", scan_id="next", verification_budget=1)
    assert len(rows) == 2
    assert stats["skipped_entries"] == 1


@pytest.mark.parametrize("repeated_page", [False, True])
def test_truncated_listing_cannot_prove_a_known_file_missing(repeated_page):
    class Client:
        def list_dir(self, path, page=1, **_):
            if path == "/Anime":
                return OpenListDirPage(entries=[OpenListEntry(name="Show", is_dir=True,
                                      remote_path="/Anime/Show")], total=1)
            if page == 1 or repeated_page:
                return OpenListDirPage(entries=[OpenListEntry(name="Show.S01E01.mkv",
                    remote_path="/Anime/Show/Show.S01E01.mkv")], total=2)
            return OpenListDirPage(entries=[], total=2)

    original = [evidence(1), evidence(2)]
    state = build_tree_baseline_state("root-contract", "/Anime", original)
    _, rows, state, _ = scan_openlist_incremental(Client(), baseline=original, state=state,
        mapping_root="/Anime", mount_root="X:/offline-fixture", scan_id="next", verification_budget=1)
    assert len(rows) == 2
    assert state["directories"]["Show"]["verification_state"] == "unknown"


@pytest.mark.parametrize("parent", ["nonexistent-parent", "next-1", "baseline-2"])
def test_inherited_parent_must_be_existing_same_slot_and_not_self(database, parent):
    with database.connect() as conn:
        conn.execute("INSERT INTO source_scans(scan_id,root_id,generation,status) "
                     "VALUES ('next','root-contract',2,'completed')")
    item = replace(evidence(1, "next"), observation_kind="inherited", parent_evidence_id=parent)
    with pytest.raises(ValueError, match="继承"):
        V4Repository(database).save_source_evidence(item)


def test_new_schema_persists_provenance_and_v23_migration_preserves_facts(database):
    with database.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_evidence)")}
        assert {"observation_kind", "parent_evidence_id"} <= columns
        before = [tuple(row)[:21] for row in conn.execute("SELECT * FROM source_evidence ORDER BY evidence_id")]
        assets = [tuple(row) for row in conn.execute("SELECT * FROM assets ORDER BY asset_id")]
        conn.execute("ALTER TABLE source_evidence DROP COLUMN observation_kind")
        conn.execute("ALTER TABLE source_evidence DROP COLUMN parent_evidence_id")
        conn.execute("ALTER TABLE source_scans DROP COLUMN coverage_json")
        conn.execute("ALTER TABLE source_scan_requests DROP COLUMN resume_after")
        conn.execute("ALTER TABLE source_scan_requests DROP COLUMN budget_cooldowns")
        conn.execute("ALTER TABLE source_roots DROP COLUMN content_scope")
        conn.execute("PRAGMA user_version = 23")
    database.initialize()
    with database.connect() as conn:
        from app.media_v4.persistence.schema_v4 import V4_SCHEMA_VERSION

        assert conn.execute("PRAGMA user_version").fetchone()[0] == V4_SCHEMA_VERSION
        assert [tuple(row)[:21] for row in conn.execute("SELECT * FROM source_evidence ORDER BY evidence_id")] == before
        assert [tuple(row) for row in conn.execute("SELECT * FROM assets ORDER BY asset_id")] == assets
        assert {row[0] for row in conn.execute("SELECT observation_kind FROM source_evidence")} == {"legacy"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
