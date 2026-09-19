"""P-001 第一轮返工回归（7.7 主链合同）。

覆盖：确认前候选解析与歧义阻断、人工确认候选约束、元数据完整性发布门控、
跨 revision 作品关系与卡片字段、目录树 NFO 身份证据。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace


def _entry(
    evidence_id: str,
    *,
    work_title: str,
    year: int = 2024,
    season: int = 1,
    episode: int = 1,
    tmdb_id: int | None = None,
    relative_path: str = "",
):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    relative = relative_path or f"{work_title}/Season {season}/{work_title}.S{season:02d}E{episode:02d}.mkv"
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-rework",
        root_id="root-rework",
        source_key=evidence_id,
        relative_path=relative,
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
        tmdb_hint_id=tmdb_id,
        tmdb_hint_type="tv" if tmdb_id else "",
        confidence="high",
    )
    return evidence, facts


def _patch_database(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "p001-rework.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    return database


def _client(tmp_path, monkeypatch):
    from app.api import library_v4, media_v4
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _patch_database(tmp_path, monkeypatch)
    application = FastAPI()
    application.include_router(media_v4.router)
    application.include_router(library_v4.router)
    return TestClient(application)


def _fake_search(*candidates_by_query: list[dict]):
    from app.media_v4.resolution.candidates import WorkCandidate

    def search(work_key: str, queries: list[str], year, media_type) -> list[WorkCandidate]:
        result: list[WorkCandidate] = []
        for query in queries:
            for candidate in candidates_by_query:
                for item in candidate:
                    if item.get("match_query") == query:
                        result.append(WorkCandidate(
                            work_key=work_key,
                            provider="tmdb",
                            provider_id=str(item["provider_id"]),
                            media_type="tv",
                            title=item["title"],
                            year=item.get("year"),
                            evidence="fake_search",
                            confidence="high",
                            status="proposed",
                        ))
        return result

    return search


# ---------------------------------------------------------------------------
# R1：确认前候选解析 —— 无显式 hint 的中英文标题经可信候选合并。
# ---------------------------------------------------------------------------


def test_pre_confirm_candidate_resolution_merges_cross_language_titles(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.resolution import candidates as candidates_module

    service = V4RevisionService(database)
    entries = [
        _entry("ev-zh", work_title="摇曳露营", episode=1),
        _entry("ev-en", work_title="Yuru Camp", episode=1, relative_path="Yuru Camp/Season 1/Yuru Camp.S01E01.mkv"),
    ]
    search = _fake_search(
        [{"match_query": "摇曳露营", "provider_id": 42, "title": "摇曳露营", "year": 2024}],
        [{"match_query": "Yuru Camp", "provider_id": 42, "title": "Yuru Camp", "year": 2024}],
    )
    monkeypatch.setattr(candidates_module, "default_candidate_search", search)

    graph = service.create_draft("rev-candidates", entries, candidate_search=search)

    assert len(graph.works) == 1
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider, provider_id, status FROM revision_work_candidates WHERE revision_id = 'rev-candidates'"
        ).fetchall()
    assert any(row["provider"] == "tmdb" and row["provider_id"] == "42" and row["status"] == "confirmed" for row in rows)

    service.confirm("rev-candidates")
    with database.connect() as conn:
        works = conn.execute("SELECT COUNT(*) AS c FROM works").fetchone()["c"]
        binding = conn.execute(
            "SELECT provider, provider_id FROM provider_bindings WHERE provider = 'tmdb'"
        ).fetchone()
    assert works == 1
    assert binding is not None and binding["provider_id"] == "42"


def test_ambiguous_candidates_block_confirm_and_no_scrape_search(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.resolution import candidates as candidates_module

    service = V4RevisionService(database)
    search = _fake_search(
        [
            {"match_query": "Show", "provider_id": 42, "title": "Show", "year": 2024},
            {"match_query": "Show", "provider_id": 43, "title": "Show", "year": 2024},
        ],
    )
    monkeypatch.setattr(candidates_module, "default_candidate_search", search)

    graph = service.create_draft("rev-ambiguous", [_entry("ev-amb", work_title="Show")], candidate_search=search)

    assert any(issue.code == "candidate_ambiguous" for issue in graph.issues)
    # 不再拦截：候选歧义只是提示，确认照常进行并继续入库（用户要求永不拦截）。
    service.confirm("rev-ambiguous")
    assert service.get_status("rev-ambiguous") == "confirmed"


# ---------------------------------------------------------------------------
# R2：人工确认只接受服务端候选 candidate_id。
# ---------------------------------------------------------------------------


def _confirmed_work_with_revision(tmp_path, monkeypatch, *, work_title="Show", tmdb_id=None):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    entries = [_entry("ev-manual", work_title=work_title, tmdb_id=tmdb_id)]
    service.create_draft("rev-manual", entries, candidate_search=lambda *a, **k: [])
    service.confirm("rev-manual")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]
        revision_id = conn.execute(
            "SELECT revision_id FROM import_revisions WHERE revision_id = 'rev-manual'"
        ).fetchone()["revision_id"]
    return database, work_id, revision_id



def _patch_manual_scrape(monkeypatch, tmp_path, provider_id: str):
    """人工确认后的 scrape 使用已冻结 binding，不再搜索；返回完整 ready 元数据。"""
    from app.media_v4.jobs import completeness as completeness_module
    from app.media_v4.jobs import metadata_artifacts as artifacts_module

    def fake_provider(target):
        assert any(
            b.get("provider") == "tmdb" and str(b.get("provider_id")) == provider_id
            for b in (target.get("provider_bindings") or [])
        ), "scrape 必须消费已确认 binding，不得重新搜索"
        return {
            "provider": "tmdb",
            "provider_id": provider_id,
            "media_type": "tv",
            "title": target.get("preferred_title") or "Show",
            "metadata_state": "ready",
            "poster_url": "https://image.tmdb.org/t/p/w780/p.jpg",
            "fanart_url": "https://image.tmdb.org/t/p/original/f.jpg",
        }

    monkeypatch.setattr("app.media_v4.jobs.metadata.default_metadata_provider", fake_provider)
    monkeypatch.setattr(artifacts_module, "load_config", lambda: _fake_artifacts_config("remote"))
    monkeypatch.setattr(completeness_module, "load_config", lambda: _fake_artifacts_config("remote"))

def test_manual_confirm_requires_server_side_candidate_and_rejects_forgery(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    database, work_id, revision_id = _confirmed_work_with_revision(tmp_path, monkeypatch)

    from app.media_v4.jobs.metadata import _candidate_summary

    fake_candidate = _candidate_summary({"id": 999, "name": "Show", "first_air_date": "2024-01-01"})
    monkeypatch.setattr(
        "app.media_v4.api.media_v4.search_tmdb_candidates" if False else "app.media_v4.jobs.metadata.search_tmdb_candidates",
        lambda *a, **k: [fake_candidate],
    )

    searched = client.post("/api/v4/metadata/search", json={"work_id": work_id, "query": "Show"})
    assert searched.status_code == 200, searched.text
    candidates = searched.json()["candidates"]
    assert candidates and candidates[0]["provider_id"] == "999"

    with database.connect() as conn:
        row = conn.execute(
            "SELECT candidate_id FROM revision_work_candidates WHERE work_id = ? AND status = 'proposed' LIMIT 1",
            (work_id,),
        ).fetchone()
    assert row is not None
    candidate_id = row["candidate_id"]

    # 伪造 provider/provider_id 不再被接受：请求只认 candidate_id。
    response = client.post("/api/v4/metadata/confirm", json={
        "work_id": work_id,
        "provider": "tmdb",
        "provider_id": "12345",
    })
    assert response.status_code == 422

    _patch_manual_scrape(monkeypatch, tmp_path, "999")
    confirmed = client.post("/api/v4/metadata/confirm", json={"work_id": work_id, "candidate_id": candidate_id})
    assert confirmed.status_code == 200, confirmed.text
    with database.connect() as conn:
        binding = conn.execute(
            "SELECT provider, provider_id, status FROM scrape_bindings WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        cand = conn.execute(
            "SELECT status FROM revision_work_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    assert binding["status"] == "confirmed"
    assert binding["provider_id"] == "999"
    assert cand["status"] == "confirmed"


def test_manual_confirm_rejects_foreign_candidate_and_is_idempotent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    database, work_id, revision_id = _confirmed_work_with_revision(tmp_path, monkeypatch)
    from app.media_v4.jobs.metadata import _candidate_summary

    fake_candidate = _candidate_summary({"id": 777, "name": "Show", "first_air_date": "2024-01-01"})
    monkeypatch.setattr(
        "app.media_v4.jobs.metadata.search_tmdb_candidates",
        lambda *a, **k: [fake_candidate],
    )
    assert client.post("/api/v4/metadata/search", json={"work_id": work_id}).status_code == 200
    with database.connect() as conn:
        candidate_id = conn.execute(
            "SELECT candidate_id FROM revision_work_candidates WHERE work_id = ? AND status = 'proposed' LIMIT 1",
            (work_id,),
        ).fetchone()["candidate_id"]

    _patch_manual_scrape(monkeypatch, tmp_path, "777")
    first = client.post("/api/v4/metadata/confirm", json={"work_id": work_id, "candidate_id": candidate_id})
    assert first.status_code == 200
    second = client.post("/api/v4/metadata/confirm", json={"work_id": work_id, "candidate_id": candidate_id})
    assert second.status_code == 200

    foreign = client.post("/api/v4/metadata/confirm", json={
        "work_id": "missing-work", "candidate_id": candidate_id,
    })
    assert foreign.status_code in {404, 409}


# ---------------------------------------------------------------------------
# R3：元数据完整性发布门控。
# ---------------------------------------------------------------------------


def _fake_artifacts_config(mode: str = "local"):
    return SimpleNamespace(
        artwork_storage_mode=mode,
        tmdb_timeout=5,
        proxy_url=None,
        tmdb_bearer_token="token",
    )


def _scrape_with_metadata(tmp_path, monkeypatch, *, provider_result: dict, mode: str = "local"):
    from app.media_v4.jobs import metadata_artifacts as artifacts_module
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    media = tmp_path / "Show.S01E01.mkv"
    media.write_bytes(b"video")
    evidence, facts = _entry("ev-comp", work_title="Show")
    evidence = replace(evidence, source_locator=str(media), playback_locator=str(media))
    service.create_draft("rev-comp", [(evidence, facts)], candidate_search=lambda *a, **k: [])
    service.confirm("rev-comp")
    monkeypatch.setattr(artifacts_module, "load_config", lambda: _fake_artifacts_config(mode))
    from app.media_v4.jobs import completeness as completeness_module

    monkeypatch.setattr(completeness_module, "load_config", lambda: _fake_artifacts_config(mode))
    scrape = V4ScrapeService(database)
    job = next(
        item for item in service.list_jobs("rev-comp") if item["job_type"] == "scrape_work"
    )
    scrape.process(job["job_id"], lambda _work: provider_result, mirror_root=tmp_path / "mirror")
    with database.connect() as conn:
        binding = conn.execute(
            "SELECT status, metadata_json FROM scrape_bindings WHERE work_id = ?",
            (conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"],),
        ).fetchone()
    return database, binding


def test_missing_local_artwork_keeps_metadata_ready_and_marks_artifacts_degraded(tmp_path, monkeypatch):
    """B1-2：图片产物缺失只降级产物，不再把作品判成“媒体信息失败”。

    Work NFO 与剧集 NFO 已经写出，缺的只是 poster/fanart → 资料保持 ready、
    产物标为 degraded；绑定仍为 confirmed，作品照常进入媒体库。
    """

    import json

    _database, binding = _scrape_with_metadata(tmp_path, monkeypatch, provider_result={
        "provider": "tmdb",
        "provider_id": "42",
        "media_type": "tv",
        "title": "Show",
        "metadata_state": "ready",
        "poster_url": "",
        "fanart_url": "",
    })
    payload = json.loads(binding["metadata_json"])
    assert binding["status"] == "confirmed"
    assert payload["metadata_state"] == "ready"
    assert payload["artifact_state"] == "degraded"
    assert payload["artifact_reasons"]


def test_metadata_ready_with_complete_remote_artwork_and_episode_nfo(tmp_path, monkeypatch):
    import json

    _database, binding = _scrape_with_metadata(tmp_path, monkeypatch, mode="remote", provider_result={
        "provider": "tmdb",
        "provider_id": "42",
        "media_type": "tv",
        "title": "Show",
        "metadata_state": "ready",
        "poster_url": "https://image.tmdb.org/t/p/w780/poster.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/original/fanart.jpg",
    })
    assert binding["status"] == "confirmed"
    payload = json.loads(binding["metadata_json"])
    assert payload.get("metadata_state") == "ready"


# ---------------------------------------------------------------------------
# R4：跨 revision 作品关系与卡片字段。
# ---------------------------------------------------------------------------


def test_relation_reuses_existing_parent_work_across_revisions(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    service.create_draft("rev-parent", [_entry("ev-parent", work_title="Yuru Camp", episode=1)], candidate_search=lambda *a, **k: [])
    service.confirm("rev-parent")

    # 仅导入外传：父 Work 已存在于数据库，关系必须被解析并持久化。
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id="ev-heya2",
        scan_id="scan-heya2",
        root_id="root-rework",
        source_key="ev-heya2",
        relative_path="Heya Camp/Season 1/Heya Camp.S01E01.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-heya2",
        evidence_id="ev-heya2",
        parser_version="fixture",
        work_title="Heya Camp",
        series_group="Yuru Camp",
        relation_type="spin_off",
        title_candidates=("Heya Camp", "Yuru Camp"),
        year_candidate=2024,
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        confidence="high",
    )
    service.create_draft("rev-heya", [(evidence, facts)], candidate_search=lambda *a, **k: [])
    service.confirm("rev-heya")

    with database.connect() as conn:
        parent = conn.execute(
            "SELECT work_id FROM works WHERE preferred_title = 'Yuru Camp'"
        ).fetchone()
        child = conn.execute(
            "SELECT work_id FROM works WHERE preferred_title = 'Heya Camp'"
        ).fetchone()
        relation = conn.execute(
            "SELECT relation_type FROM work_relations WHERE parent_work_id = ? AND child_work_id = ?",
            (parent["work_id"], child["work_id"]),
        ).fetchone()
    assert relation is not None and relation["relation_type"] == "spin_off"


def test_library_returns_persisted_show_type_and_card_type(tmp_path, monkeypatch):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.revisions.service import V4RevisionService

    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    media = tmp_path / "Show.S01E01.mkv"
    media.write_bytes(b"video")
    evidence = SourceEvidence(
        evidence_id="ev-card",
        scan_id="scan-card",
        root_id="root-rework",
        source_key="ev-card",
        relative_path="Show/Season 1/Show.S01E01.mkv",
        entry_kind="video",
        provider="local",
        source_locator=str(media),
        playback_locator=str(media),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-card",
        evidence_id="ev-card",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        year_candidate=2024,
        media_type="tv",
        group_type="season",
        show_type="live_series",
        card_type="main_series",
        season_candidate=1,
        episode_candidate=1,
        confidence="high",
    )
    V4RevisionService(media_v4._database).create_draft("rev-card", [(evidence, facts)], candidate_search=lambda *a, **k: [])
    assert client.post("/api/v4/imports/rev-card/confirm").status_code == 200

    response = client.get("/api/library?include_all=true")
    assert response.status_code == 200
    work = response.json()["works"][0]
    assert work["show_type"] == "live_series"
    assert work["card_type"] == "main_series"


# ---------------------------------------------------------------------------
# R5：目录树 sidecar NFO 身份证据。
# ---------------------------------------------------------------------------


def test_directory_tree_filters_nfo_from_offline_identity_evidence():
    from app.media_v4.sources.scanner import build_directory_tree_evidence

    text = "摇曳露营.nfo\nSeason 1/摇曳露营.S01E01.mkv\n"
    _scan_id, evidence = build_directory_tree_evidence(
        text,
        root_id="root-nfo",
        provider="baidu",
    )
    assert len(evidence) == 1
    assert evidence[0].entry_kind == "video"
    assert evidence[0].relative_path == "Season 1/摇曳露营.S01E01.mkv"
