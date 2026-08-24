"""P-001 第四次定向返工回归（7.12 R16/R17）。

R16：生产搜索真实补全 provider alias（详情缓存/预算）；别名仅在等值匹配时
     才 high；人工 search 持久化 original/aliases。
R17：NFO→Work 确定性归属；目录级 NFO 多 Work 必须 sidecar_nfo_ambiguous
     阻断确认，不注入、不 merge。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.media_v4.domain.models import ParsedFacts, SourceEvidence


def _entry(evidence_id: str, *, work_title: str, year: int = 2024, season: int = 1, episode: int = 1, relative_path: str = ""):
    relative = relative_path or f"{work_title}/Season {season}/{work_title}.S{season:02d}E{episode:02d}.mkv"
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-rw4",
        root_id="root-rw4",
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
        confidence="high",
    )
    return evidence, facts


def _nfo(evidence_id: str, relative_path: str, *, tmdb_id: int, title: str = "", original: str = ""):
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-rw4",
        root_id="root-rw4",
        source_key=evidence_id,
        relative_path=relative_path,
        entry_kind="metadata",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        resource_type="metadata",
        work_title=title,
        original_title=original,
        title_candidates=(title or relative_path.rsplit("/", 1)[-1],),
        tmdb_hint_id=tmdb_id,
        tmdb_hint_type="tv",
        is_importable=False,
        is_auxiliary=True,
    )
    return evidence, facts


def _patch_database(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "p001-rw4.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    return database


# ---------------------------------------------------------------------------
# R16：真实搜索补全 alias。
# ---------------------------------------------------------------------------


def test_real_search_has_no_aliases_until_detail_enrichment_makes_local_alias_high(tmp_path, monkeypatch):
    from app.media_v4.jobs import metadata as metadata_module
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    # 真实搜索摘要：没有 aliases 字段。
    monkeypatch.setattr(
        metadata_module,
        "search_tmdb_candidates",
        lambda *a, **k: [{
            "provider_id": "43",
            "media_type": "tv",
            "title": "Yuru Camp",
            "original_title": "Yuru Camp",
            "year": 2024,
        }],
    )

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_tv_detail(self, provider_id):
            return {
                "alternative_titles": {"results": []},
                "translations": {"translations": [
                    {"data": {"title": "摇曳露营", "name": "摇曳露营"}},
                ]},
            }

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(
        tmdb_bearer_token="token", artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))

    monkeypatch.setattr("app.core.config.load_config", lambda: SimpleNamespace(
        tmdb_bearer_token="token", artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    service = V4RevisionService(database)
    service.create_draft("rev-alias", [_entry("alias-en", work_title="摇曳露营")])

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider, provider_id, status, original_title, aliases_json "
            "FROM revision_work_candidates WHERE revision_id = 'rev-alias'"
        ).fetchall()
    assert any(
        row["provider"] == "tmdb" and row["provider_id"] == "43" and row["status"] == "confirmed"
        for row in rows
    )
    assert any("摇曳露营" in row["aliases_json"] for row in rows)


def test_alias_enrichment_failure_or_budget_keeps_candidate_non_high(tmp_path, monkeypatch):
    from app.media_v4.jobs import metadata as metadata_module
    from app.media_v4.resolution import candidates as candidates_module
    from app.media_v4.resolution.resolver import MediaResolver

    _patch_database(tmp_path, monkeypatch)
    monkeypatch.setattr("app.core.config.load_config", lambda: SimpleNamespace(
        tmdb_bearer_token="token", artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    monkeypatch.setattr(
        metadata_module,
        "search_tmdb_candidates",
        lambda *a, **k: [{
            "provider_id": "44",
            "media_type": "tv",
            "title": "Yuru Camp",
            "original_title": "Yuru Camp",
            "year": 2024,
        }],
    )

    class FailingClient:
        def __init__(self, bearer_token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_tv_detail(self, provider_id):
            raise RuntimeError("detail unavailable")

    monkeypatch.setattr(metadata_module, "TMDBClient", FailingClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(
        tmdb_bearer_token="token", artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))

    evidence, facts = _entry("alias-zh", work_title="摇曳露营")
    graph = MediaResolver().resolve([(evidence, facts)])
    candidates_by_key, _merge, issues = candidates_module.plan_work_candidates(
        graph,
        [(evidence, facts)],
        candidates_module.default_candidate_search,
    )
    items = candidates_by_key.get(graph.works[0].work_key, [])
    assert items
    # 详情失败 → 无别名 → 本地中文不匹配 provider 英文 → 非 high，保持 proposed。
    assert all(item.confidence != "high" for item in items)
    assert all(item.status == "proposed" for item in items)


def test_manual_search_persists_original_and_aliases(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    database = _patch_database(tmp_path, monkeypatch)
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    service.create_draft("rev-man", [_entry("man", work_title="Show")], candidate_search=lambda *a, **k: [])
    service.confirm("rev-man")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]

    from app.media_v4.jobs import metadata as metadata_module

    monkeypatch.setattr("app.core.config.load_config", lambda: SimpleNamespace(
        tmdb_bearer_token="token", artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    monkeypatch.setattr(
        metadata_module,
        "search_tmdb_candidates",
        lambda *a, **k: [{
            "provider_id": "99",
            "media_type": "tv",
            "title": "The Show",
            "original_title": "Original Show",
            "year": 2024,
        }],
    )

    class FakeTMDBClient:
        def __init__(self, bearer_token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_tv_detail(self, provider_id):
            return {"alternative_titles": {"results": [{"title": "别名甲"}]}, "translations": {"translations": []}}

    monkeypatch.setattr(metadata_module, "TMDBClient", FakeTMDBClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(
        tmdb_bearer_token="token", artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))

    searched = client.post("/api/v4/metadata/search", json={"work_id": work_id, "query": "Show"})
    assert searched.status_code == 200, searched.text
    with database.connect() as conn:
        row = conn.execute(
            "SELECT original_title, aliases_json FROM revision_work_candidates "
            "WHERE work_id = ? AND evidence = 'manual_search' LIMIT 1",
            (work_id,),
        ).fetchone()
    assert row is not None
    assert row["original_title"] == "Original Show"
    assert "别名甲" in row["aliases_json"]


# ---------------------------------------------------------------------------
# R17：NFO→Work 确定性归属。
# ---------------------------------------------------------------------------


def test_independent_work_nfos_bind_respectively(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    entries = [
        _entry("a", work_title="Show A"),
        _entry("b", work_title="Show B"),
        _nfo("nfo-a", "Show A/Show A.nfo", tmdb_id=100, title="Show A"),
        _nfo("nfo-b", "Show B/Show B.nfo", tmdb_id=200, title="Show B"),
    ]
    graph = service.create_draft("rev-nfo-ab", entries, candidate_search=lambda *a, **k: [])
    assert len(graph.works) == 2
    assert not any(issue.code == "sidecar_nfo_ambiguous" for issue in graph.issues)
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT draft_work_key, provider_id, status FROM revision_work_candidates "
            "WHERE revision_id = 'rev-nfo-ab' AND evidence = 'sidecar_nfo_provider'"
        ).fetchall()
    assert {row["provider_id"] for row in rows} == {"100", "200"}


def test_single_work_dir_tvshow_nfo_binds_normally(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    entries = [
        _entry("solo", work_title="Show"),
        _nfo("nfo-tv", "Show/Season 1/tvshow.nfo", tmdb_id=55),
    ]
    graph = service.create_draft("rev-nfo-solo", entries, candidate_search=lambda *a, **k: [])
    assert len(graph.works) == 1
    assert not any(issue.code == "sidecar_nfo_ambiguous" for issue in graph.issues)
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider_id, status FROM revision_work_candidates "
            "WHERE revision_id = 'rev-nfo-solo' AND evidence = 'sidecar_nfo_provider'"
        ).fetchall()
    assert any(row["provider_id"] == "55" for row in rows)


def test_multi_work_dir_tvshow_nfo_is_ambiguous_and_not_injected(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    # 同一目录两个作品 + 一个目录级 tvshow.nfo。
    entries = [
        _entry("m1", work_title="Show A", relative_path="合集/Show A.S01E01.mkv"),
        _entry("m2", work_title="Show B", relative_path="合集/Show B.S01E01.mkv"),
        _nfo("nfo-amb", "合集/tvshow.nfo", tmdb_id=66),
    ]
    graph = service.create_draft("rev-nfo-amb", entries, candidate_search=lambda *a, **k: [])
    assert any(issue.code == "sidecar_nfo_ambiguous" for issue in graph.issues)
    # 任一 Work 都不应拿到该 NFO 的 provider ID，也不产生 merge。
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider_id, status FROM revision_work_candidates "
            "WHERE revision_id = 'rev-nfo-amb' AND evidence = 'sidecar_nfo_provider'"
        ).fetchall()
    assert rows == []
    # confirm 被 issue 阻断。
    from app.media_v4.revisions.service import RevisionBlockedError

    with pytest.raises(RevisionBlockedError):
        service.confirm("rev-nfo-amb")


def test_failed_alias_detail_is_cached_for_the_whole_draft(monkeypatch):
    """详情失败也是缓存结果，不能因多个 Work 对同一 ID 重复请求。"""

    from app.media_v4.jobs import metadata as metadata_module

    calls = 0

    class FailingClient:
        def __init__(self, bearer_token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_tv_detail(self, provider_id):
            nonlocal calls
            calls += 1
            raise RuntimeError("detail unavailable")

    monkeypatch.setattr(metadata_module, "TMDBClient", FailingClient)
    monkeypatch.setattr(metadata_module, "load_config", lambda: SimpleNamespace(
        tmdb_bearer_token="token", artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    candidate = [{"provider_id": "77", "media_type": "tv", "title": "English", "original_title": "English"}]
    cache: dict = {}
    budget = [0]
    metadata_module.enrich_candidate_aliases(candidate, ["中文名"], detail_cache=cache, detail_budget=budget)
    metadata_module.enrich_candidate_aliases(candidate, ["中文名"], detail_cache=cache, detail_budget=budget)

    assert calls == 1
    assert budget == [1]


def test_nfo_stem_must_share_work_directory_scope(tmp_path, monkeypatch):
    """同名 NFO 位于无关目录时不能仅凭标题绑定到作品。"""

    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    entries = [
        _entry("scoped-video", work_title="Show A", relative_path="Library/Show A/Season 1/Show A.S01E01.mkv"),
        _nfo("unrelated-nfo", "Backup/Show A.nfo", tmdb_id=701, title="Show A"),
    ]
    service.create_draft("rev-nfo-scope", entries, candidate_search=lambda *a, **k: [])
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider_id FROM revision_work_candidates "
            "WHERE revision_id = 'rev-nfo-scope' AND evidence = 'sidecar_nfo_provider'"
        ).fetchall()
    assert rows == []


def test_owned_tvshow_nfo_uses_its_content_titles_as_query(tmp_path, monkeypatch):
    """目录级 NFO 归属明确时，查询应使用其内容标题而不是 tvshow 文件名。"""

    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    captured: list[str] = []

    def search(_work_key, queries, _year, _media_type):
        captured.extend(queries)
        return []

    entries = [
        _entry("query-video", work_title="Opaque Name", relative_path="Show/Season 1/Opaque Name.S01E01.mkv"),
        _nfo("query-nfo", "Show/tvshow.nfo", tmdb_id=0, title="Provider Title"),
    ]
    service.create_draft("rev-nfo-query", entries, candidate_search=search)

    assert "Provider Title" in captured
    assert "tvshow" not in captured


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4

    _patch_database(tmp_path, monkeypatch)
    application = FastAPI()
    application.include_router(media_v4.router)
    return TestClient(application)
