"""历史 Provider 错绑不能再次把独立作品合并。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.media_v4.domain.models import ParsedFacts, ResolvedWork, SourceEvidence
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.resolution.candidates import WorkCandidate, plan_work_candidates
from app.media_v4.resolution.identity_policy import historical_identity_conflict
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.revisions.service import V4RevisionService, _existing_work_matches


def test_movie_subtitle_survives_historical_identity_comparison():
    # title:<完整片名>:<年份>:movie 中的片名本身也可以含冒号。
    # 首次导入形成 work:<完整片名>:movie 后，再导入必须仍为同一边界。
    for subtitle, year in (("序", 2007), ("破", 2009), ("q", 2012)):
        title = f"福音战士新剧场版:{subtitle}"
        work = ResolvedWork(
            work_key=f"title:{title}:{year}:movie",
            preferred_title=title,
            year=year,
            media_type="movie",
            card_type="standalone",
        )
        assert historical_identity_conflict(work, {f"work:{title}:movie"}) is None
        assert historical_identity_conflict(work, {"work:另一部电影:movie"}) is not None


def _entry(
    evidence_id: str,
    relative_path: str,
    *,
    work_title: str,
    card_type: str = "main_series",
    relation_type: str = "main",
    season: int = 1,
) -> tuple[SourceEvidence, ParsedFacts]:
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-identity-repair",
        root_id="root-identity-repair",
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=work_title,
        title_candidates=(work_title, "Yuru Camp"),
        series_group="Yuru Camp",
        card_type=card_type,
        relation_type=relation_type,
        media_type="tv",
        group_type="season",
        season_candidate=season,
        episode_candidate=1,
        confidence="high",
    )
    return evidence, facts


def test_historical_heya_binding_does_not_merge_with_main_series():
    """旧 Work 同时覆盖主系列和外传时，95213 不能成为两者的合并依据。"""

    entries = [
        _entry(
            "main-01",
            "Yuru Camp/Yuru Camp [01].mkv",
            work_title="Yuru Camp",
        ),
        _entry(
            "heya-01",
            "Yuru Camp/Heya Camp/Heya Camp [01].mkv",
            work_title="Heya Camp",
            card_type="standalone",
            relation_type="spin_off",
        ),
    ]
    graph = MediaResolver().resolve(entries)
    work_keys = {work.preferred_title: work.work_key for work in graph.works}

    candidates, merge_map, issues = plan_work_candidates(
        graph,
        entries,
        lambda *_args: [],
        existing_bindings={
            work_keys["Yuru Camp"]: [("tmdb", "tv", "95213")],
            work_keys["Heya Camp"]: [("tmdb", "tv", "95213")],
        },
    )

    assert merge_map == {}
    assert any(issue.code == "work_identity_conflict" for issue in issues)
    assert {
        item.provider_id
        for values in candidates.values()
        for item in values
        if item.status == "confirmed"
    } == set()


def test_reimport_movie_after_cancelled_jobs_reuses_correct_identity(tmp_path):
    database = V4Database(tmp_path / "movie-reimport.db")
    database.initialize()
    service = V4RevisionService(database)
    evidence, facts = _entry(
        "movie-first", "电影/福音战士新剧场版：序 (2007)/序.mkv",
        work_title="福音战士新剧场版：序", card_type="standalone", relation_type="movie",
    )
    facts = replace(
        facts, title_candidates=(facts.work_title,), series_group="",
        media_type="movie", group_type="movie", season_candidate=None,
        episode_candidate=None, year_candidate=2007, tmdb_hint_id=15137, tmdb_hint_type="movie",
    )
    service.create_draft("first", [(evidence, facts)])
    service.confirm("first")
    with database.connect() as conn:
        first_ids = [r[0] for r in conn.execute("SELECT work_id FROM works WHERE status = 'active'")]
        conn.execute("UPDATE jobs SET status = 'cancelled' WHERE revision_id = 'first'")
    next_evidence = replace(evidence, evidence_id="movie-next", scan_id="scan-next")
    next_facts = replace(facts, parsed_fact_id="facts-next", evidence_id="movie-next")
    service.create_draft("next", [(next_evidence, next_facts)])
    service.confirm("next")
    assert service.get_status("next") == "confirmed"
    with database.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT work_id FROM works WHERE status = 'active'")] == first_ids


def test_confirmed_scraped_title_reuses_cross_language_owner_not_empty_duplicate(tmp_path):
    import json

    database = V4Database(tmp_path / "translated-owner.db")
    database.initialize()
    service = V4RevisionService(database)
    entry = _entry("english", "Yuru Camp/S01E01.mkv", work_title="Yuru Camp")
    service.create_draft("english", [entry])
    service.confirm("english")
    with database.connect() as conn:
        owner = conn.execute("SELECT work_id FROM works WHERE preferred_title='Yuru Camp'").fetchone()[0]
        conn.execute("INSERT INTO provider_bindings VALUES (?, 'tmdb', 'tv', '76075')", (owner,))
        conn.execute(
            "INSERT INTO scrape_bindings(binding_id,revision_id,work_id,provider,provider_id,metadata_json,status,created_at,updated_at) "
            "VALUES ('translated','english',?,'tmdb','76075',?,'confirmed','now','now')",
            (owner, json.dumps({'title': '摇曳露营△', 'original_title': 'ゆるキャン△', 'metadata_state': 'ready'})),
        )
        conn.execute(
            "INSERT INTO works(work_id,identity_key,work_type,preferred_title,created_at,updated_at) "
            "VALUES ('empty','series:摇曳露营:tv','series','摇曳露营','now','now')",
        )
        work = ResolvedWork(work_key="series:摇曳露营:tv", preferred_title="摇曳露营", media_type="tv", year=None)
        assert [r['work_id'] for r in _existing_work_matches(conn, work)] == [owner]
        # 失败/待复核资料不能成为跨语言身份依据。
        conn.execute("UPDATE scrape_bindings SET status='waiting_review'")
        assert [r['work_id'] for r in _existing_work_matches(conn, work)] == ['empty']
        conn.execute("UPDATE scrape_bindings SET status='confirmed'")
    evidence, facts = _entry('chinese', '摇曳露营/Season 1/S01E01.mp4', work_title='摇曳露营')
    facts = replace(facts, series_group='摇曳露营', title_candidates=('摇曳露营',))
    service.create_draft('chinese', [(evidence, facts)])
    service.confirm('chinese')
    with database.connect() as conn:
        assert {r[0] for r in conn.execute(
            "SELECT work_id FROM revision_bindings WHERE revision_id='chinese'",
        )} == {owner}


def test_independent_work_keeps_asset_boundary_when_episode_numbers_match():
    """独立作品同号剧集不能被收口为一个逻辑 Episode。"""

    entries = [
        _entry("main-01", "Yuru Camp/Season 1/[01].mkv", work_title="Yuru Camp"),
        _entry(
            "heya-01",
            "Yuru Camp/Heya Camp/[01].mkv",
            work_title="Heya Camp",
            card_type="standalone",
            relation_type="spin_off",
        ),
    ]
    graph = MediaResolver().resolve(entries)

    assert {work.preferred_title for work in graph.works} == {"Yuru Camp", "Heya Camp"}
    episodes = [episode for episode in graph.episodes if episode.local_episode_number == 1]
    assert len(episodes) == 2
    assert {episode.work_key for episode in episodes} == {
        next(work.work_key for work in graph.works if work.preferred_title == "Yuru Camp"),
        next(work.work_key for work in graph.works if work.preferred_title == "Heya Camp"),
    }


def test_polluted_historical_work_is_not_reused_for_either_boundary(tmp_path):
    """同一旧 Work 含主系列/外传结构键时，落库阶段必须进入恢复路径。"""

    database = V4Database(tmp_path / "polluted-identity.db")
    database.initialize()
    now = datetime.now(UTC).isoformat()
    entries = [
        _entry(
            "heya-01",
            "Yuru Camp/Heya Camp/[01].mkv",
            work_title="Heya Camp",
            card_type="standalone",
            relation_type="spin_off",
        )
    ]
    heya_work = ResolvedWork(
        work_key="work:heya camp:tv",
        preferred_title="Heya Camp",
        year=None,
        media_type="tv",
        source_evidence_ids=("heya-01",),
        card_type="standalone",
        relation_type="spin_off",
    )
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-identity-repair', 'local', 'local_scan', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO works(work_id, identity_key, work_type, preferred_title, year, created_at, updated_at) "
            "VALUES ('polluted-work', 'title:yuru camp::tv', 'series', 'Yuru Camp Season 2', NULL, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO work_aliases(work_id, normalized_title, alias_type) "
            "VALUES ('polluted-work', 'heya camp', 'observed')"
        )
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES ('polluted-work', 'tmdb', 'tv', '95213')"
        )
        conn.executemany(
            "INSERT INTO work_source_bindings(work_id, root_id, structural_key) VALUES (?, ?, ?)",
            [
                ('polluted-work', 'root-identity-repair', 'series:yuru camp:tv'),
                ('polluted-work', 'root-identity-repair', 'work:heya camp:tv'),
            ],
        )
        assert _existing_work_matches(conn, heya_work, entries) == []


def test_provider_candidate_owned_by_polluted_work_requires_repair(tmp_path):
    """Provider owner 仍存在时，草稿不能绕过边界校验直接复用它。"""

    database = V4Database(tmp_path / "polluted-provider.db")
    database.initialize()
    now = datetime.now(UTC).isoformat()
    entries = [
        _entry(
            "heya-01",
            "Yuru Camp/Heya Camp/[01].mkv",
            work_title="Heya Camp",
            card_type="standalone",
            relation_type="spin_off",
        )
    ]
    graph = MediaResolver().resolve(entries)
    work = graph.works[0]
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-identity-repair', 'local', 'local_scan', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO works(work_id, identity_key, work_type, preferred_title, year, created_at, updated_at) "
            "VALUES ('polluted-work', 'title:yuru camp::tv', 'series', 'Yuru Camp Season 2', NULL, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO work_aliases(work_id, normalized_title, alias_type) "
            "VALUES ('polluted-work', 'heya camp', 'observed')"
        )
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES ('polluted-work', 'tmdb', 'tv', '95213')"
        )
        conn.executemany(
            "INSERT INTO work_source_bindings(work_id, root_id, structural_key) VALUES (?, ?, ?)",
            [
                ('polluted-work', 'root-identity-repair', 'series:yuru camp:tv'),
                ('polluted-work', 'root-identity-repair', 'work:heya camp:tv'),
            ],
        )
    candidates = {
        work.work_key: [WorkCandidate(
            work_key=work.work_key,
            provider="tmdb",
            provider_id="95213",
            media_type="tv",
            title="Heya Camp",
            original_title="へやキャン△",
            aliases=(),
            year=None,
            evidence="provider_search",
            confidence="high",
            status="confirmed",
        )]
    }
    issues = V4RevisionService(database)._candidate_identity_conflicts(
        graph,
        candidates,
        entries,
    )
    assert any(issue.code == "work_identity_conflict" for issue in issues)


def _seed_polluted_media(database: V4Database, *, main_title="Yuru Camp", main_series="Yuru Camp") -> str:
    """写入一份最小历史污染库，两个同号资产共用一个旧 Episode。"""

    now = datetime.now(UTC).isoformat()
    entries = [
        _entry("main-01", "Yuru Camp/Season 1/[01].mkv", work_title="Yuru Camp"),
        _entry(
            "heya-01",
            "Yuru Camp/Heya Camp/[01].mkv",
            work_title="Heya Camp",
            card_type="standalone",
            relation_type="spin_off",
        ),
    ]
    entries = [
        (replace(evidence, scan_id="scan-repair", root_id="root-repair"), facts)
        for evidence, facts in entries
    ]
    evidence, facts = entries[0]
    entries[0] = (evidence, replace(facts, work_title=main_title, series_group=main_series))
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-repair', 'local', 'local_scan', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at, finished_at) "
            "VALUES ('scan-repair', 'root-repair', 1, 'completed', ?, ?)",
            (now, now),
        )
    repository = V4Repository(database)
    repository.save_scan_evidence_bulk([evidence for evidence, _facts in entries])
    repository.save_parsed_facts_bulk([facts for _evidence, facts in entries])
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
            "VALUES ('polluted-work', 'title:yuru camp::tv', 'series', 'Yuru Camp Season 2', ?, ?)",
            (now, now),
        )
        conn.executemany(
            "INSERT INTO work_source_bindings(work_id, root_id, structural_key) VALUES (?, ?, ?)",
            [
                ('polluted-work', 'root-repair', 'series:yuru camp:tv'),
                ('polluted-work', 'root-repair', 'work:heya camp:tv'),
            ],
        )
        conn.execute(
            "INSERT INTO provider_bindings(work_id, provider, media_type, provider_id) "
            "VALUES ('polluted-work', 'tmdb', 'tv', '95213')"
        )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at, confirmed_at) "
            "VALUES ('revision-repair-old', 'root-repair', 'scan-repair', 'fixture', 'confirmed', 'old', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at, confirmed_at) "
            "VALUES ('revision-repair-history', 'root-repair', 'scan-repair', 'fixture', 'superseded', 'history', ?, ?)",
            (now, now),
        )
        conn.executemany(
            "INSERT INTO revision_evidence(revision_id, evidence_id, parsed_fact_id) VALUES ('revision-repair-old', ?, ?)",
            [(evidence.evidence_id, f"facts-{evidence.evidence_id}") for evidence, _facts in entries],
        )
        conn.execute(
            "INSERT INTO seasons(season_id, work_id, local_season_number, season_kind) "
            "VALUES ('old-season', 'polluted-work', 1, 'regular')"
        )
        conn.execute(
            "INSERT INTO episodes(episode_id, work_id, season_id, local_episode_number, episode_kind, display_title) "
            "VALUES ('old-episode', 'polluted-work', 'old-season', 1, 'regular', '')"
        )
        for index, (evidence, _facts) in enumerate(entries):
            asset_id = f"asset-{index}"
            conn.execute(
                "INSERT INTO assets(asset_id, evidence_id, root_id, source_locator, playback_locator, fingerprint) "
                "VALUES (?, ?, 'root-repair', ?, ?, ?)",
                (asset_id, evidence.evidence_id, evidence.source_locator, evidence.playback_locator, asset_id),
            )
            conn.execute(
                "INSERT INTO episode_assets(episode_id, asset_id) VALUES ('old-episode', ?)",
                (asset_id,),
            )
            conn.execute(
                "INSERT INTO revision_bindings(binding_id, revision_id, evidence_id, work_id, season_id, episode_id, asset_id) "
                "VALUES (?, 'revision-repair-old', ?, 'polluted-work', 'old-season', 'old-episode', ?)",
                (f"old-binding-{index}", evidence.evidence_id, asset_id),
            )
        conn.execute(
            "INSERT INTO scrape_bindings(binding_id, revision_id, work_id, provider, provider_id, metadata_json, status, created_at, updated_at) "
            "VALUES ('old-scrape', 'revision-repair-old', 'polluted-work', 'tmdb', '95213', ?, 'confirmed', ?, ?)",
            ('{"title":"房间露营△","original_title":"へやキャン△"}', now, now),
        )
        conn.execute(
            "INSERT INTO scrape_bindings(binding_id, revision_id, work_id, provider, provider_id, metadata_json, status, created_at, updated_at) "
            "VALUES ('old-scrape-main', 'revision-repair-history', 'polluted-work', 'tmdb', '76075', ?, 'confirmed', ?, ?)",
            ('{"title":"Yuru Camp","original_title":"ゆるキャン△"}', now, now),
        )
        conn.execute(
            "INSERT INTO playback_progress(episode_id, asset_id, work_id, position, duration, completed, updated_at) "
            "VALUES ('old-episode', 'asset-1', 'polluted-work', 42, 100, 0, ?)",
            (now,),
        )
    return "polluted-work"


@pytest.mark.parametrize("metadata_title, expected_match", [("Collection", False), ("Collection: Part One", True)])
def test_repair_matches_complete_series_title_not_colon_prefix(tmp_path, metadata_title, expected_match):
    """修复预览可以匹配系列别名，但不能把冒号前缀当成完整名称。"""
    import json

    from app.media_v4.maintenance.identity_repair import build_identity_repair_preview

    database = V4Database(tmp_path / "repair-colon.db")
    database.initialize()
    work_id = _seed_polluted_media(database, main_title="Display Name", main_series="Collection: Part One")
    with database.connect() as conn:
        conn.execute(
            "UPDATE scrape_bindings SET metadata_json = ? WHERE provider_id = '76075'",
            (json.dumps({"title": metadata_title}),),
        )
    preview = build_identity_repair_preview(database, work_id=work_id)
    matches = [item for item in preview["provider_assignments"] if item["provider_id"] == "76075"]
    assert bool(matches) is expected_match
    if expected_match:
        assert matches[0]["target_work_key"] == "series:collection: part one:tv"
    else:
        assert any(item.get("provider_id") == "76075" for item in preview["blocked_reasons"])


def test_identity_repair_preview_apply_and_resume_are_asset_scoped(tmp_path):
    """恢复按 Asset 分裂同号集，进度迁移且重复应用幂等。"""

    from app.media_v4.maintenance.identity_repair import (
        apply_identity_repair,
        build_identity_repair_preview,
        resume_identity_repair,
    )

    database = V4Database(tmp_path / "identity-repair.db")
    database.initialize()
    work_id = _seed_polluted_media(database)

    preview = build_identity_repair_preview(database, work_id=work_id)
    assert preview["blocked"] is False
    assert {item["title"] for item in preview["target_works"]} == {"Yuru Camp", "Heya Camp"}
    assert len(preview["items"]) == 2
    assert {
        (item["provider_id"], item["target_work_key"])
        for item in preview["provider_assignments"]
        if item["provider"] == "tmdb"
    } == {
        ("76075", "series:yuru camp:tv"),
        ("95213", "work:heya camp:tv"),
    }

    result = apply_identity_repair(
        database,
        preview_id=preview["preview_id"],
        digest=preview["digest"],
    )
    assert result["status"] == "completed"
    assert result["migrated_asset_count"] == 2

    with database.connect() as conn:
        active_works = conn.execute(
            "SELECT preferred_title FROM works WHERE status = 'active' ORDER BY preferred_title"
        ).fetchall()
        assert [str(row[0]) for row in active_works] == ["Heya Camp", "Yuru Camp"]
        assert {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                "SELECT w.preferred_title, pb.provider, pb.provider_id "
                "FROM provider_bindings pb JOIN works w ON w.work_id = pb.work_id "
                "WHERE w.status = 'active' AND pb.provider = 'tmdb'"
            ).fetchall()
        } == {
            ("Heya Camp", "tmdb", "95213"),
            ("Yuru Camp", "tmdb", "76075"),
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM playback_progress WHERE work_id != 'polluted-work'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM revision_bindings WHERE revision_id LIKE 'identity-repair-%'"
        ).fetchone()[0] == 2
        repaired_bindings = conn.execute(
            """
            SELECT w.preferred_title, b.structural_key
            FROM work_source_bindings b
            JOIN works w ON w.work_id = b.work_id
            WHERE w.status = 'active' AND b.binding_source = 'identity_repair'
            ORDER BY w.preferred_title
            """
        ).fetchall()
        assert [(str(row[0]), str(row[1])) for row in repaired_bindings] == [
            ("Heya Camp", "work:heya camp:tv"),
            ("Yuru Camp", "series:yuru camp:tv"),
        ]

    repaired_entries = [
        _entry("main-01", "Yuru Camp/Season 1/[01].mkv", work_title="Yuru Camp"),
        _entry(
            "heya-01",
            "Yuru Camp/Heya Camp/[01].mkv",
            work_title="Heya Camp",
            card_type="standalone",
            relation_type="spin_off",
        ),
    ]
    repaired_graph = MediaResolver().resolve(repaired_entries)
    assert V4RevisionService(database)._structural_identity_conflicts(
        repaired_graph,
        repaired_entries,
    ) == []

    repeated = apply_identity_repair(
        database,
        preview_id=preview["preview_id"],
        digest=preview["digest"],
    )
    assert repeated == result
    assert resume_identity_repair(database, operation_id=preview["preview_id"]) == result
