"""P-001 第二次返工回归（7.8 主链合同）。

覆盖：跨 revision Work 复用、候选评分/同名年份、必经完整性门控、
NFO 关联与候选输入、候选 revision 归属。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace


def _entry(evidence_id: str, *, work_title: str, year: int = 2024, season: int = 1, episode: int = 1):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-rework2",
        root_id="root-rework2",
        source_key=evidence_id,
        relative_path=f"{work_title}/Season {season}/{work_title}.S{season:02d}E{episode:02d}.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=work_title,
        title_candidates=(work_title,),
        year_candidate=year,
        media_type="tv",
        group_type="season",
        season_candidate=season,
        episode_candidate=episode,
        confidence="high",
    )
    return evidence, facts


def _patch_database(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "p001-rw2.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    return database


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import library_v4, media_v4

    _patch_database(tmp_path, monkeypatch)
    application = FastAPI()
    application.include_router(media_v4.router)
    application.include_router(library_v4.router)
    return TestClient(application)


def _candidate(query: str, provider_id: int, title: str, year: int | None = None, original: str = ""):
    return {
        "match_query": query,
        "provider_id": provider_id,
        "title": title,
        "original_title": original,
        "year": year,
    }


def _search_fn(*rules):
    """按查询返回候选；默认把每个候选都标记为 exact 标题命中。"""

    from app.media_v4.resolution.candidates import WorkCandidate

    def search(work_key: str, queries: list[str], year, media_type) -> list[WorkCandidate]:
        result = []
        for query in queries:
            for rule in rules:
                if rule.get("match_query") != query:
                    continue
                result.append(WorkCandidate(
                    work_key=work_key,
                    provider="tmdb",
                    provider_id=str(rule["provider_id"]),
                    media_type="tv",
                    title=rule["title"],
                    year=rule.get("year"),
                    evidence="online_search",
                    confidence="high",
                    status="proposed",
                ))
        return result

    return search


# ---------------------------------------------------------------------------
# R6：跨 revision 合并 —— 先中文后英文、两次无显式 hint、同一候选 ID → 一个 Work。
# ---------------------------------------------------------------------------


def test_cross_revision_merge_with_frozen_candidate_identity(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    search = _search_fn(
        _candidate("摇曳露营", 42, "摇曳露营", 2024),
        _candidate("Yuru Camp", 42, "Yuru Camp", 2024),
    )

    service.create_draft("rev-zh", [_entry("zh", work_title="摇曳露营")], candidate_search=search)
    service.confirm("rev-zh")
    with database.connect() as conn:
        zh_work = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()

    service.create_draft("rev-en", [_entry("en", work_title="Yuru Camp")], candidate_search=search)
    service.confirm("rev-en")
    with database.connect() as conn:
        works = conn.execute("SELECT work_id, preferred_title FROM works ORDER BY preferred_title").fetchall()

    assert len(works) == 1
    assert works[0]["work_id"] == zh_work["work_id"]


def test_same_title_different_year_is_not_auto_merged_by_candidates(tmp_path, monkeypatch):
    from app.media_v4.resolution import candidates as candidates_module

    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    search = _search_fn(
        _candidate("Show", 42, "Show", 2010),
        _candidate("Show", 43, "Show", 2024),
    )
    monkeypatch.setattr(candidates_module, "default_candidate_search", search)

    graph = service.create_draft("rev-year", [_entry("year", work_title="Show", year=None)], candidate_search=search)

    # 两个候选都属于同名但年份冲突 → 不自动合并，保持 review。
    assert any(issue.code == "candidate_ambiguous" for issue in graph.issues)


def test_candidate_title_prefix_is_not_treated_as_exact_identity(tmp_path, monkeypatch):
    """Show 与 Showdown 不能仅因前缀相同自动确认同一 Provider 身份。"""

    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    search = _search_fn(_candidate("Show", 99, "Showdown", 2024))

    service.create_draft("rev-prefix", [_entry("prefix", work_title="Show")], candidate_search=search)

    with database.connect() as conn:
        row = conn.execute(
            "SELECT confidence, status FROM revision_work_candidates WHERE revision_id = 'rev-prefix'"
        ).fetchone()
    assert row is not None
    assert row["confidence"] == "medium"
    assert row["status"] == "proposed"


def test_spinoff_series_group_is_not_used_as_candidate_identity_query(tmp_path, monkeypatch):
    """外传的父系列只用于关系，不能作为外传自身的候选身份查询。"""

    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    evidence, facts = _entry("spinoff-query", work_title="Heya Camp")
    facts = replace(facts, series_group="Yuru Camp", relation_type="spin_off")
    seen_queries: list[str] = []

    def search(_work_key, queries, _year, _media_type):
        seen_queries.extend(queries)
        return []

    V4RevisionService(database).create_draft(
        "rev-spinoff-query", [(evidence, facts)], candidate_search=search
    )

    assert "Heya Camp" in seen_queries
    assert "Yuru Camp" not in seen_queries


# ---------------------------------------------------------------------------
# R7：完整性门控必经 —— 手动确认候选但无镜像产物不得发布。
# ---------------------------------------------------------------------------


def test_manual_confirm_without_mirror_artifacts_must_not_publish(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    service.create_draft("rev-manual2", [_entry("m2", work_title="Show")], candidate_search=lambda *a, **k: [])
    service.confirm("rev-manual2")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]

    from app.media_v4.jobs.metadata import _candidate_summary

    monkeypatch.setattr(
        "app.media_v4.jobs.metadata.search_tmdb_candidates",
        lambda *a, **k: [_candidate_summary({"id": 555, "name": "Show", "first_air_date": "2024-01-01"})],
    )
    searched = client.post("/api/v4/metadata/search", json={"work_id": work_id, "query": "Show"})
    assert searched.status_code == 200, searched.text
    candidate_id = searched.json()["candidates"][0]["candidate_id"]


    def fake_provider(target):
        return {
            "provider": "tmdb",
            "provider_id": "555",
            "media_type": "tv",
            "title": "Show",
            "metadata_state": "ready",
            "poster_url": "https://image.tmdb.org/t/p/w780/p.jpg",
            "fanart_url": "https://image.tmdb.org/t/p/original/f.jpg",
        }

    monkeypatch.setattr("app.media_v4.jobs.metadata.default_metadata_provider", fake_provider)

    confirmed = client.post("/api/v4/metadata/confirm", json={"work_id": work_id, "candidate_id": candidate_id})
    assert confirmed.status_code == 200, confirmed.text

    with database.connect() as conn:
        binding = conn.execute(
            "SELECT provider, provider_id, status FROM scrape_bindings WHERE work_id = ?",
            (work_id,),
        ).fetchone()
    assert binding is not None
    assert binding["status"] != "confirmed"

    library = client.get("/api/library")
    assert library.status_code == 200
    assert library.json()["works"] == []


# ---------------------------------------------------------------------------
# R8：完整性权威 —— 删除本地 NFO 后 rebuild 不得保持 ready。
# ---------------------------------------------------------------------------


def test_projection_rechecks_artifact_files_not_only_metadata_json(tmp_path, monkeypatch):
    from app.media_v4.jobs import completeness as completeness_module
    from app.media_v4.jobs import metadata_artifacts as artifacts_module
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    media = tmp_path / "Show.S01E01.mkv"
    media.write_bytes(b"video")
    evidence, facts = _entry("c2", work_title="Show")
    evidence = replace(evidence, source_locator=str(media), playback_locator=str(media))
    service.create_draft("rev-comp2", [(evidence, facts)], candidate_search=lambda *a, **k: [])
    service.confirm("rev-comp2")
    monkeypatch.setattr(artifacts_module, "load_config", lambda: SimpleNamespace(
        artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    monkeypatch.setattr(completeness_module, "load_config", lambda: SimpleNamespace(
        artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    scrape = V4ScrapeService(database)
    job = next(item for item in service.list_jobs("rev-comp2") if item["job_type"] == "scrape_work")
    mirror_root = tmp_path / "mirror"
    scrape.process(job["job_id"], lambda _w: {
        "provider": "tmdb", "provider_id": "42", "media_type": "tv", "title": "Show",
        "metadata_state": "ready",
        "poster_url": "https://image.tmdb.org/t/p/w780/p.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/original/f.jpg",
    }, mirror_root=mirror_root)
    with database.connect() as conn:
        binding = conn.execute(
            "SELECT status, metadata_json FROM scrape_bindings WHERE work_id = (SELECT work_id FROM works LIMIT 1)"
        ).fetchone()
    assert binding["status"] == "confirmed"

    # 删除本地 NFO 文件：rebuild 必须复查 artifact 文件，不能仍 ready。
    for nfo in mirror_root.rglob("*.nfo"):
        nfo.unlink()

    snapshot = V4LibraryProjection(database).rebuild()
    card = snapshot.cards[0]
    assert card["metadata"].get("metadata_state") != "ready"


# ---------------------------------------------------------------------------
# R9：NFO 关联 —— sidecar NFO 提供候选身份且不改变本地编号。
# ---------------------------------------------------------------------------


def test_sidecar_nfo_title_enters_candidate_inputs_without_overriding_episode(tmp_path, monkeypatch):
    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.parsing.parser import V4Parser
    from app.media_v4.resolution.candidates import WorkCandidate
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    video = SourceEvidence(
        evidence_id="video-only",
        scan_id="scan-nfo",
        root_id="root-nfo",
        source_key="video-only",
        relative_path="摇曳露营/Season 1/S01E01.mkv",
        entry_kind="video",
        provider="local",
    )
    nfo = SourceEvidence(
        evidence_id="nfo-only",
        scan_id="scan-nfo",
        root_id="root-nfo",
        source_key="nfo-only",
        relative_path="摇曳露营/摇曳露营.nfo",
        entry_kind="metadata",
        provider="local",
    )
    video_facts = V4Parser().parse(video)
    nfo_facts = V4Parser().parse(nfo)
    assert video_facts.is_importable is True
    assert nfo_facts.is_importable is False

    captured = {"queries": []}

    def search(work_key, queries, year, media_type):
        captured["queries"] = queries
        return [
            WorkCandidate(work_key=work_key, provider="tmdb", provider_id="77", media_type="tv",
                          title="摇曳露营", year=2024, evidence="online_search", confidence="high",
                          status="proposed"),
        ]

    service = V4RevisionService(database)
    graph = service.create_draft("rev-nfo", [(video, video_facts), (nfo, nfo_facts)], candidate_search=search)

    assert captured["queries"], "NFO 标题必须进入候选查询输入"
    assert any("摇曳露营" in query for query in captured["queries"])
    # 本地集号不被 NFO 覆盖。
    assert any(ep.local_season_number == 1 and ep.local_episode_number == 1 for ep in graph.episodes)
    # 冻结候选记录必须保存 sidecar_nfo 证据。
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT evidence FROM revision_work_candidates WHERE revision_id = 'rev-nfo'"
        ).fetchall()
    assert any("sidecar_nfo" in str(row["evidence"]) for row in rows)


# ---------------------------------------------------------------------------
# R10：手动候选必须归属当前 active confirmed revision。
# ---------------------------------------------------------------------------


def test_manual_candidate_belongs_to_active_confirmed_revision(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    service.create_draft("rev-a1", [_entry("a1", work_title="Show")], candidate_search=lambda *a, **k: [])
    service.confirm("rev-a1")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]

    from app.media_v4.jobs.metadata import _candidate_summary

    monkeypatch.setattr(
        "app.media_v4.jobs.metadata.search_tmdb_candidates",
        lambda *a, **k: [_candidate_summary({"id": 888, "name": "Show", "first_air_date": "2024-01-01"})],
    )
    assert client.post("/api/v4/metadata/search", json={"work_id": work_id}).status_code == 200
    with database.connect() as conn:
        row = conn.execute(
            "SELECT revision_id FROM revision_work_candidates WHERE work_id = ? AND evidence = 'manual_search'",
            (work_id,),
        ).fetchone()
    assert row is not None
    assert row["revision_id"] == "rev-a1"


def test_manual_confirm_rejects_candidate_from_superseded_revision(tmp_path, monkeypatch):
    """候选必须属于当前 active confirmed revision，不能跨 revision 复用。"""

    import uuid
    from datetime import UTC, datetime

    client = _client(tmp_path, monkeypatch)
    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    service.create_draft("rev-old", [_entry("old", work_title="Show")], candidate_search=lambda *a, **k: [])
    service.confirm("rev-old")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]

    fresh_evidence, fresh_facts = _entry("new", work_title="Show")
    fresh_evidence = replace(fresh_evidence, root_id="root-new", scan_id="scan-new")
    service.create_draft("rev-new", [(fresh_evidence, fresh_facts)], candidate_search=lambda *a, **k: [])
    service.confirm("rev-new")

    candidate_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO revision_work_candidates(
                candidate_id, revision_id, work_id, draft_work_key, provider,
                provider_id, media_type, title, evidence, confidence, status,
                created_at, updated_at
            ) VALUES (?, 'rev-old', ?, 'old-key', 'tmdb', '42', 'tv', 'Show',
                      'manual_search', 'high', 'proposed', ?, ?)
            """,
            (candidate_id, work_id, now, now),
        )

    response = client.post(
        "/api/v4/metadata/confirm",
        json={"work_id": work_id, "candidate_id": candidate_id},
    )
    assert response.status_code == 409


def test_manual_confirm_reports_conflict_when_provider_identity_belongs_to_other_work(tmp_path, monkeypatch):
    """Provider 全局唯一冲突必须返回可恢复的 409，而非暴露 SQLite 异常。"""

    import uuid
    from datetime import UTC, datetime

    client = _client(tmp_path, monkeypatch)
    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    owner_evidence, owner_facts = _entry("owner", work_title="Owner Show")
    service.create_draft("rev-owner", [(owner_evidence, owner_facts)], candidate_search=lambda *a, **k: [])
    service.confirm("rev-owner")

    target_evidence, target_facts = _entry("target", work_title="Target Show")
    target_evidence = replace(target_evidence, root_id="root-target", scan_id="scan-target")
    service.create_draft("rev-target", [(target_evidence, target_facts)], candidate_search=lambda *a, **k: [])
    service.confirm("rev-target")

    now = datetime.now(UTC).isoformat()
    candidate_id = str(uuid.uuid4())
    with database.connect() as conn:
        owner_id = conn.execute(
            "SELECT work_id FROM works WHERE preferred_title = 'Owner Show'"
        ).fetchone()["work_id"]
        target_id = conn.execute(
            "SELECT work_id FROM works WHERE preferred_title = 'Target Show'"
        ).fetchone()["work_id"]
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES (?, 'tmdb', 'tv', '42')",
            (owner_id,),
        )
        conn.execute(
            """
            INSERT INTO revision_work_candidates(
                candidate_id, revision_id, work_id, draft_work_key, provider,
                provider_id, media_type, title, evidence, confidence, status,
                created_at, updated_at
            ) VALUES (?, 'rev-target', ?, 'target-key', 'tmdb', '42', 'tv',
                      'Target Show', 'manual_search', 'high', 'proposed', ?, ?)
            """,
            (candidate_id, target_id, now, now),
        )

    response = client.post(
        "/api/v4/metadata/confirm",
        json={"work_id": target_id, "candidate_id": candidate_id},
    )
    assert response.status_code == 409
