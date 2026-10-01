"""删除来源后，历史证据可审计，但不能再用作增量扫描基线。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.maintenance.source_deletion import delete_source_library
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.revisions.service import V4RevisionService


@pytest.fixture
def confirmed_source(tmp_path):
    database = V4Database(tmp_path / "baseline.db")
    database.initialize()
    evidence = SourceEvidence(
        evidence_id="ev-baseline", scan_id="scan-baseline", root_id="root-baseline",
        source_key="/Anime/Show.S01E01.mkv", relative_path="Show.S01E01.mkv",
        entry_kind="video", provider="quark", ingest_method="openlist_api",
        source_locator="/Anime/Show.S01E01.mkv", playback_locator="X:/Show.S01E01.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-baseline", evidence_id=evidence.evidence_id,
        parser_version="fixture", work_title="Show", title_candidates=("Show",),
        media_type="tv", group_type="season", season_candidate=1, episode_candidate=1,
    )
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-baseline", [(evidence, facts)])
    revisions.confirm("rev-baseline")
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'cancelled'")
    return database, revisions, evidence, facts


def _retire(database, tmp_path):
    result = delete_source_library(database, "root-baseline", mirror_root=tmp_path / "mirror")
    assert result["ok"] is True
    assert result["retired"] is True


def test_deleted_source_has_no_baseline_without_destroying_history(confirmed_source, tmp_path):
    database, _, evidence, _ = confirmed_source
    repository = V4Repository(database)
    assert repository.list_confirmed_source_evidence(evidence.root_id) == [evidence]
    with database.connect() as conn:
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("source_evidence", "parsed_facts", "revision_evidence", "revision_bindings", "import_revisions")
        }
    _retire(database, tmp_path)
    with database.connect() as conn:
        after = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in before
        }
    assert after == before
    assert repository.list_confirmed_source_evidence(evidence.root_id) == []


def test_hidden_card_still_has_a_baseline(confirmed_source):
    from app.media_v4.projection.source_libraries import hide_source_card

    database, _, evidence, _ = confirmed_source
    hide_source_card(database, evidence.root_id)
    assert V4Repository(database).list_confirmed_source_evidence(evidence.root_id) == [evidence]


def test_reimport_draft_does_not_reactivate_baseline_until_confirmed(confirmed_source, tmp_path):
    database, revisions, evidence, facts = confirmed_source
    _retire(database, tmp_path)
    new_evidence = replace(evidence, evidence_id="ev-new", scan_id="scan-new")
    new_facts = replace(facts, evidence_id=new_evidence.evidence_id, parsed_fact_id="facts-new")
    revisions.create_draft("rev-new", [(new_evidence, new_facts)])
    repository = V4Repository(database)
    assert repository.list_confirmed_source_evidence(evidence.root_id) == []
    revisions.confirm("rev-new")
    assert repository.list_confirmed_source_evidence(evidence.root_id) == [new_evidence]


@pytest.mark.parametrize("entrypoint", ["status", "sync", "durable"])
def test_deleted_openlist_baseline_is_rejected_before_network(confirmed_source, tmp_path, monkeypatch, entrypoint):
    from app.api import media_v4, openlist_v4
    from app.integrations.openlist.providers import OpenListRouteConfig

    database, _, evidence, _ = confirmed_source
    _retire(database, tmp_path)
    config = SimpleNamespace(
        openlist_server_url="https://example.test", openlist_remote_root="/",
        openlist_mount_root="X:/", openlist_routes=[OpenListRouteConfig(
            route_id="route-anime", remote_prefix="/Anime", provider_id="quark",
        )],
    )
    monkeypatch.setattr(media_v4, "_database", database)
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("fixture", "fixture", "available"))
    monkeypatch.setattr(media_v4, "openlist_root_id", lambda *_, **_kwargs: evidence.root_id)

    def no_network(*_args, **_kwargs):
        pytest.fail("退役来源不能使用旧基线或发起增量网络枚举")

    monkeypatch.setattr(openlist_v4, "_client", no_network)
    if entrypoint == "status":
        assert media_v4.openlist_status("/Anime")["has_confirmed_baseline"] is False
    else:
        request = media_v4.SourceScanRequest(
            source="openlist", root_path="/Anime", provider="quark", scan_mode="incremental",
        )
        with pytest.raises(HTTPException) as caught:
            if entrypoint == "sync":
                media_v4.scan_source(request)
            else:
                media_v4.start_durable_scan(request)
        assert caught.value.status_code == 409
        assert "尚无已确认基线" in caught.value.detail
