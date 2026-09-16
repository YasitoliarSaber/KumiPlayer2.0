"""O15：读端点默认不能被"顺手"拉成全量载荷。

`/sources/scans/{scan_id}` 默认带证据条目时，任何新调用方只要漏传参数就会拿到
数万条证据（数十 MB JSON）；`/imports/{revision_id}/evidence` 是识别预览的完整
列表，默认保持全量，但必须给出 ``total`` 与可用的分页参数，让程序化调用方能自我约束。
"""

from __future__ import annotations


def _app(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    database = V4Database(tmp_path / "bounds.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(media_v4.router)
    return TestClient(application), database


def _evidence(scan_id: str, relative: str):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return to_source_evidence(SourceEntry(
        root_id="root-bounds",
        scan_id=scan_id,
        provider="baidu",
        ingest_method="directory_tree",
        relative_path=relative,
        source_key=relative,
    ))


def _seed_scan(database, *, scan_id: str = "scan-bounds", evidence_count: int = 1) -> None:
    from app.media_v4.persistence.repositories import V4Repository

    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-bounds', 'baidu', 'directory_tree', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES (?, 'root-bounds', 1, 'completed', 'ready', 'now', 'now')",
            (scan_id,),
        )
    if evidence_count:
        V4Repository(database).save_scan_evidence_bulk([
            _evidence(scan_id, f"Show/F{index}.mkv") for index in range(evidence_count)
        ])


def test_scan_detail_omits_entries_by_default(tmp_path, monkeypatch):
    client, database = _app(tmp_path, monkeypatch)
    _seed_scan(database, evidence_count=3)

    body = client.get("/api/v4/sources/scans/scan-bounds").json()

    assert body["entries"] == [], "默认不得返回证据条目"
    assert body["evidence_count"] == 3, "条数仍然要如实报告"
    assert body["status"] == "completed"


def test_scan_detail_returns_entries_when_explicitly_requested(tmp_path, monkeypatch):
    client, database = _app(tmp_path, monkeypatch)
    _seed_scan(database, evidence_count=3)

    body = client.get("/api/v4/sources/scans/scan-bounds?include_entries=true").json()

    assert len(body["entries"]) == 3


def test_revision_evidence_reports_total_and_supports_paging(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    client, database = _app(tmp_path, monkeypatch)
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-bounds", [_entry(index) for index in range(5)])

    full = client.get("/api/v4/imports/rev-bounds/evidence").json()
    assert len(full["entries"]) == 5, "默认保持全量，识别预览需要完整列表"
    assert full["total"] == 5
    assert full["returned"] == 5

    page = client.get("/api/v4/imports/rev-bounds/evidence?limit=2&offset=2").json()
    assert len(page["entries"]) == 2
    assert page["total"] == 5, "total 必须是完整条数，便于调用方判断是否还有更多"
    assert page["returned"] == 2
    assert [item["relative_path"] for item in page["entries"]] == [
        item["relative_path"] for item in full["entries"][2:4]
    ]


def _entry(index: int):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence_id = f"ev-{index}"
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-bounds",
        root_id="root-bounds",
        source_key=f"Show/F{index}.mkv",
        relative_path=f"Show/F{index}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://{index}",
        fingerprint=f"sha256:{index}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{index}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        year_candidate=2024,
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=index + 1,
    )
    return evidence, facts
