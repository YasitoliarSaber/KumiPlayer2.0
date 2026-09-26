"""CHECK-005A/B/C/D：确认落库、跨来源续接与重扫幂等。

全部断言经过真实入口：``V4Parser`` → ``MediaResolver``（由 ``V4RevisionService``
内部调用）→ 真实临时 SQLite → ``V4PlaybackStore``。不使用人工“已成功”行，
不访问源盘、凭据或网络。

多个未定位条目必须通过正式确认入口，不使用 xfail 豁免核心合同。
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import replace

import pytest
from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.persistence.database import V4Database
from app.media_v4.playback.store import V4PlaybackStore
from app.media_v4.resolution.identity_contract import graph_semantic_digest
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
from app.media_v4.sources.scanner import build_directory_tree_evidence

PARSER = V4Parser()


def _database(tmp_path, name: str) -> V4Database:
    database = V4Database(tmp_path / name)
    database.initialize()
    return database


def _tree_entries(text: str, *, root_id: str, scan_id: str, provider: str = "baidu"):
    _scan, evidence = build_directory_tree_evidence(
        text, root_id=root_id, scan_id=scan_id, provider=provider
    )
    return normalize_batch_parsed_facts(
        [(item, PARSER.parse(item, root_container="动画")) for item in evidence]
    )


def _local_entry(
    path: str,
    *,
    root_id: str,
    scan_id: str,
    size: int | None,
    mtime: float | None = 1000.0,
    provider: str = "local",
):
    evidence = to_source_evidence(
        SourceEntry(
            root_id=root_id,
            scan_id=scan_id,
            provider=provider,
            ingest_method="local_scan",
            relative_path=path,
            source_key=path,
            source_locator=path,
            playback_locator=path,
            size=size,
            mtime=mtime,
        )
    )
    return evidence, PARSER.parse(evidence)


def _pair(
    path: str,
    index: int,
    *,
    title: str,
    season: int | None,
    episode: int | None,
    year: int | None = 2024,
    tmdb_hint_id: int | None = None,
    tmdb_hint_type: str = "tv",
    root_id: str = "root-rt",
    scan_id: str | None = None,
):
    scan = scan_id or f"scan-{root_id}"
    evidence = SourceEvidence(
        evidence_id=f"ev-rt-{index:03d}",
        scan_id=scan,
        root_id=root_id,
        source_key=path,
        relative_path=path,
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-rt-{index:03d}",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title=title,
        title_candidates=(title,),
        series_group=title,
        year_candidate=year,
        media_type="tv",
        group_type="season",
        season_candidate=season,
        episode_candidate=episode,
        tmdb_hint_id=tmdb_hint_id,
        tmdb_hint_type=tmdb_hint_type if tmdb_hint_id else "",
        confidence="high",
    )
    return evidence, facts


def _graph_partitions(graph) -> dict[str, set[frozenset[str]]]:
    """图侧去向分区：Work / (Work, Episode) → evidence 集合。"""

    work_map: dict[str, set[str]] = defaultdict(set)
    episode_map: dict[tuple[str, str], set[str]] = defaultdict(set)
    for episode in graph.episodes:
        for evidence_id in episode.asset_evidence_ids:
            work_map[episode.work_key].add(evidence_id)
            episode_map[(episode.work_key, episode.episode_key)].add(evidence_id)
    for asset in graph.work_assets:
        for evidence_id in asset.asset_evidence_ids:
            work_map[asset.work_key].add(evidence_id)
    return {
        "works": {frozenset(items) for items in work_map.values()},
        "episodes": {frozenset(items) for items in episode_map.values()},
    }


def _sql_partitions(conn, revision_id: str):
    rows = conn.execute(
        """
        SELECT evidence_id, work_id, episode_id, asset_id
        FROM revision_bindings WHERE revision_id = ?
        """,
        (revision_id,),
    ).fetchall()
    work_map: dict[str, set[str]] = defaultdict(set)
    episode_map: dict[tuple[str, str], set[str]] = defaultdict(set)
    asset_map: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        work_id = str(row["work_id"])
        evidence_id = str(row["evidence_id"])
        work_map[work_id].add(evidence_id)
        if row["episode_id"]:
            episode_id = str(row["episode_id"])
            episode_map[(work_id, episode_id)].add(evidence_id)
            asset_map[(work_id, episode_id)].add(str(row["asset_id"]))
    return rows, work_map, episode_map, asset_map


def _progress_key(row: dict) -> str:
    """C-007：电影用 ``movie:<work_id>`` 进度键，剧集用真实 episode_id。"""

    return str(row["episode_id"]) if row["episode_id"] else f"movie:{row['work_id']}"


# --- CHECK-005A：图 → confirm → SQLite → reload ------------------------------


def test_confirmed_revision_matches_graph_relations(tmp_path):
    """CHECK-005A: 图里的 Work/Episode 去向与 SQLite 的 binding 分区一一对应。"""

    database = _database(tmp_path, "roundtrip-relations.db")
    service = V4RevisionService(database)
    text = "\n".join(
        [
            "Show/Season 1/Show.S01E01.1080p.mkv",
            "Show/Season 1/Show.S01E01.2160p.mkv",
            "Show/Season 1/Show.S01E03-E04.mkv",
            "Show/Season 2/Show.S02E13.mkv",
            "Show/Show ABS13.mkv",
            "Show/未知内容甲.mkv",
            "Show/Specials/Show.SP01.mkv",
        ]
    )
    entries = _tree_entries(text, root_id="root-a", scan_id="scan-a")
    draft_graph = service.create_draft("rev-a", entries)
    service.confirm("rev-a")

    with database.connect() as conn:
        rows, work_map, episode_map, asset_map = _sql_partitions(conn, "rev-a")
        excluded_ids = {
            evidence.evidence_id
            for evidence, facts in entries
            if not facts.is_importable or facts.is_auxiliary
        }
        bound_ids = {str(row["evidence_id"]) for row in rows}
        assert excluded_ids.isdisjoint(bound_ids)

        parts = _graph_partitions(draft_graph)
        assert {frozenset(items) for items in work_map.values()} == parts["works"]
        assert {frozenset(items) for items in episode_map.values()} == parts["episodes"]

        # 每个观察只对应一个 Asset；同一逻辑集的多版本因此是两个 Asset。
        for key, evidence_ids in episode_map.items():
            assert len(asset_map[key]) == len(evidence_ids), key
            assert all(row["asset_id"] for row in rows)

        assert all(
            row["identity_key"] for row in conn.execute("SELECT identity_key FROM episodes").fetchall()
        )
        assert all(
            row["identity_key"] for row in conn.execute("SELECT identity_key FROM seasons").fetchall()
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM assets WHERE source_file_id IS NULL OR source_file_id = ''"
        ).fetchone()[0] == 0
        observations = conn.execute(
            "SELECT evidence_id, asset_id FROM source_file_observations"
        ).fetchall()
        assert {str(row["evidence_id"]) for row in observations} == bound_ids | excluded_ids
        for row in observations:
            if str(row["evidence_id"]) in excluded_ids:
                assert row["asset_id"] is None
            else:
                assert row["asset_id"]

    # reload：从数据库重读事实重算的图与确认前的草稿保持同一组逻辑键。
    reloaded_entries = service._load_revision_entries("rev-a")
    reloaded = MediaResolver().resolve(reloaded_entries)
    assert _graph_partitions(reloaded) == _graph_partitions(draft_graph)
    assert graph_semantic_digest(reloaded) == graph_semantic_digest(draft_graph)


def test_unresolved_and_absolute_episodes_keep_null_local_coordinates(tmp_path):
    """CHECK-005A: 未定位与绝对编号集的本地季集在库里保持 NULL，身份键非空唯一。"""

    database = _database(tmp_path, "roundtrip-unknown.db")
    service = V4RevisionService(database)
    text = "\n".join(["Show/未知内容甲.mkv", "Show/Show ABS13.mkv"])
    entries = _tree_entries(text, root_id="root-u", scan_id="scan-u")
    service.create_draft("rev-u", entries)
    service.confirm("rev-u")

    with database.connect() as conn:
        episode_rows = conn.execute(
            """
            SELECT e.local_episode_number, e.absolute_episode_number, e.identity_key,
                   e.episode_kind, e.display_title, s.local_season_number, s.season_kind
            FROM episodes e JOIN seasons s ON s.season_id = e.season_id
            """
        ).fetchall()

    assert len(episode_rows) == 2, episode_rows
    absolute_row = next(
        row for row in episode_rows if row["absolute_episode_number"] is not None
    )
    unresolved_row = next(
        row for row in episode_rows if row["absolute_episode_number"] is None
    )
    assert int(absolute_row["absolute_episode_number"]) == 13
    assert unresolved_row["episode_kind"] == "unknown"
    for row in episode_rows:
        assert row["local_season_number"] is None
        assert row["local_episode_number"] is None
        assert row["identity_key"]
    assert absolute_row["identity_key"].startswith('["absolute"')
    assert unresolved_row["identity_key"].startswith('["unresolved"')
    assert len({row["identity_key"] for row in episode_rows}) == 2


def test_two_unresolved_episodes_in_one_work_remain_distinct(tmp_path):
    """CHECK-005A: 两个无编号文件应是两个未定位 Episode。"""

    database = _database(tmp_path, "roundtrip-two-unknown.db")
    service = V4RevisionService(database)
    entries = _tree_entries(
        "\n".join(["Show/未知内容甲.mkv", "Show/未知内容乙.mkv"]),
        root_id="root-uu",
        scan_id="scan-uu",
    )
    service.create_draft("rev-uu", entries)
    service.confirm("rev-uu")

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2


def test_movie_work_uses_work_asset_without_episode(tmp_path):
    """CHECK-005A: 电影是 Works 级 Asset，binding 的 episode_id 为空。"""

    database = _database(tmp_path, "roundtrip-movie.db")
    service = V4RevisionService(database)
    entries = _tree_entries(
        "Movie/Show 剧场版 [Movie].mkv", root_id="root-m", scan_id="scan-m"
    )
    graph = service.create_draft("rev-m", entries)
    service.confirm("rev-m")

    assert graph.work_assets
    assert not graph.episodes
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT episode_id, edition_id, asset_id FROM revision_bindings WHERE revision_id = 'rev-m'"
        ).fetchall()
        work_type = conn.execute("SELECT work_type FROM works").fetchone()["work_type"]
    assert len(rows) == 1
    assert rows[0]["episode_id"] is None
    assert rows[0]["asset_id"]
    assert work_type == "movie"


def test_explicit_special_creates_no_binding(tmp_path):
    """CHECK-005A: 明确特别篇零 binding，只保留排除证据。"""

    database = _database(tmp_path, "roundtrip-special.db")
    service = V4RevisionService(database)
    entries = _tree_entries(
        "\n".join(["Show/Season 1/Show.S01E01.mkv", "Show/Specials/Show.SP01.mkv"]),
        root_id="root-s",
        scan_id="scan-s",
    )
    special_ids = {
        evidence.evidence_id
        for evidence, facts in entries
        if facts.content_class == "attached_special"
    }
    assert special_ids
    service.create_draft("rev-s", entries)
    service.confirm("rev-s")

    with database.connect() as conn:
        bound = {
            str(row["evidence_id"])
            for row in conn.execute(
                "SELECT evidence_id FROM revision_bindings WHERE revision_id = 'rev-s'"
            ).fetchall()
        }
        observed = {
            str(row["evidence_id"]): row["asset_id"]
            for row in conn.execute("SELECT evidence_id, asset_id FROM source_file_observations")
        }
    assert bound.isdisjoint(special_ids)
    for evidence_id in special_ids:
        assert observed.get(evidence_id) is None


# --- CHECK-005B：跨 root 续接与反例 -----------------------------------------


def test_cross_root_same_title_with_verified_identity_reuses_one_work(tmp_path):
    """CHECK-005B: 明确同 Provider 身份 + 完整标题相等 + 相同边界 → 一 Work 两 Asset。"""

    database = _database(tmp_path, "cross-root-positive.db")
    service = V4RevisionService(database)
    text = "无职转生2/Season 1/无职转生2 S01E01.mkv"
    first = _tree_entries(text, root_id="root-p1", scan_id="scan-p1")
    service.create_draft("rev-p1", first)
    service.confirm("rev-p1")

    second = _tree_entries(text, root_id="root-p2", scan_id="scan-p2")
    service.create_draft("rev-p2", second)
    service.confirm("rev-p2")

    with database.connect() as conn:
        works = conn.execute("SELECT work_id, preferred_title FROM works").fetchall()
        assets = conn.execute("SELECT asset_id, source_file_id FROM assets").fetchall()
        bindings = conn.execute(
            "SELECT revision_id, work_id, asset_id FROM revision_bindings ORDER BY revision_id"
        ).fetchall()
    assert len(works) == 1
    assert len(assets) == 2
    assert len({str(row["asset_id"]) for row in bindings}) == 2
    assert {str(row["work_id"]) for row in bindings} == {str(works[0]["work_id"])}
    assert all(row["source_file_id"] for row in assets)


def test_cross_root_same_title_without_identity_evidence_stays_independent(tmp_path):
    """CHECK-005B: 纯同名（无 Provider 证据）不能合并两个来源的本地 Work。"""

    database = _database(tmp_path, "cross-root-title-only.db")
    service = V4RevisionService(database)
    first = _pair("Show/Season 1/Show.S01E01.mkv", 1, title="Show", season=1, episode=1, root_id="root-t1")
    second = _pair("Show/Season 1/Show.S01E01.mkv", 2, title="Show", season=1, episode=1, root_id="root-t2")
    service.create_draft("rev-t1", [first])
    service.confirm("rev-t1")
    service.create_draft("rev-t2", [second])
    service.confirm("rev-t2")

    with database.connect() as conn:
        works = conn.execute("SELECT work_id FROM works").fetchall()
        bindings = conn.execute("SELECT DISTINCT work_id FROM revision_bindings").fetchall()
    assert len(works) == 2
    assert len(bindings) == 2


def test_same_title_different_year_is_not_merged(tmp_path):
    """CHECK-005B: 同名但明确年份不同时保持两个 Work。"""

    database = _database(tmp_path, "cross-root-year.db")
    service = V4RevisionService(database)
    first = _pair(
        "Show/Season 1/Show.S01E01.mkv", 1, title="Show", season=1, episode=1, year=2024, root_id="root-y1"
    )
    second = _pair(
        "Show/Season 1/Show.S01E01.mkv", 2, title="Show", season=1, episode=1, year=1999, root_id="root-y2"
    )
    service.create_draft("rev-y1", [first])
    service.confirm("rev-y1")
    service.create_draft("rev-y2", [second])
    service.confirm("rev-y2")

    with database.connect() as conn:
        years = sorted(int(row["year"]) for row in conn.execute("SELECT year FROM works").fetchall())
    assert years == [1999, 2024]


def test_shared_provider_hint_does_not_merge_tv_and_movie(tmp_path):
    """CHECK-005B: 共享同一 Provider 提示的 TV 与电影边界不能互相吸收。"""

    database = _database(tmp_path, "cross-root-boundary.db")
    service = V4RevisionService(database)
    tv = _pair(
        "Show/Season 1/Show.S01E01.mkv",
        1,
        title="Show",
        season=1,
        episode=1,
        tmdb_hint_id=777,
        root_id="root-b1",
    )
    movie_entries = _tree_entries(
        "Show/Show 剧场版 [Movie].mkv", root_id="root-b2", scan_id="scan-b2"
    )
    service.create_draft("rev-b1", [tv])
    service.confirm("rev-b1")
    service.create_draft("rev-b2", movie_entries)
    service.confirm("rev-b2")

    with database.connect() as conn:
        works = conn.execute("SELECT work_id, work_type FROM works").fetchall()
    assert len(works) == 2
    assert {str(row["work_type"]) for row in works} == {"series", "movie"}


def test_separate_search_hits_on_same_provider_id_do_not_merge(tmp_path):
    """CHECK-005B: 两侧分别搜索命中同一 Provider ID 不足以合并（需标题/别名覆盖）。"""

    database = _database(tmp_path, "cross-root-search.db")
    service = V4RevisionService(database)

    def search(work_key, queries, _year, _media_type):
        from app.media_v4.resolution.candidates import WorkCandidate

        return [
            WorkCandidate(
                work_key=work_key,
                provider="tmdb",
                provider_id="42",
                media_type="tv",
                title=query,
                year=None,
                evidence="online_search",
                confidence="high",
                status="proposed",
            )
            for query in queries
        ]

    first = _pair(
        "摇曳露营/Season 1/摇曳露营.S01E01.mkv", 1, title="摇曳露营", season=1, episode=1, root_id="root-s1"
    )
    second = _pair(
        "Yuru Camp/Season 1/Yuru Camp.S01E01.mkv", 2, title="Yuru Camp", season=1, episode=1, root_id="root-s2"
    )
    service.create_draft("rev-s1", [first], candidate_search=search)
    service.confirm("rev-s1")
    service.create_draft("rev-s2", [second], candidate_search=search)
    service.confirm("rev-s2")

    with database.connect() as conn:
        works = conn.execute(
            "SELECT work_id, preferred_title FROM works ORDER BY preferred_title"
        ).fetchall()
        bindings = conn.execute(
            "SELECT work_id, provider_id FROM provider_bindings WHERE provider_id = '42'"
        ).fetchall()
    assert [str(row["preferred_title"]) for row in works] == ["Yuru Camp", "摇曳露营"]
    assert len({str(row["work_id"]) for row in bindings}) == 2


def test_main_series_and_spin_off_sharing_provider_id_are_not_merged():
    """CHECK-005B: 主系列与外传共享一个已核验身份时，候选层也不合并。"""

    from app.media_v4.resolution.candidates import plan_work_candidates

    main_evidence, main_facts = _pair(
        "Yuru Camp/Season 1/Yuru Camp.S01E01.mkv", 1, title="Yuru Camp", season=1, episode=1
    )
    spin_evidence, spin_facts = _pair(
        "Heya Camp/Season 1/Heya Camp.S01E01.mkv", 2, title="Heya Camp", season=1, episode=1
    )
    spin_facts = replace(
        spin_facts, card_type="standalone", relation_type="spin_off", series_group=""
    )
    entries = [(main_evidence, main_facts), (spin_evidence, spin_facts)]
    graph = MediaResolver().resolve(entries)
    keys = {work.preferred_title: work.work_key for work in graph.works}
    assert set(keys) == {"Yuru Camp", "Heya Camp"}

    _candidates, merge_map, issues = plan_work_candidates(
        graph,
        entries,
        lambda *_args: [],
        existing_bindings={
            keys["Yuru Camp"]: [("tmdb", "tv", "95213")],
            keys["Heya Camp"]: [("tmdb", "tv", "95213")],
        },
    )

    assert merge_map == {}
    assert not [issue for issue in issues if issue.code == "work_identity_conflict"]


# --- CHECK-005C：不变重扫、进度续接与替换 -----------------------------------


def test_unchanged_rescan_keeps_work_episode_asset_and_progress(tmp_path):
    """CHECK-005C: 同 root 两 scan 不变时 Work/Episode/Asset 不变，123 秒仍可读。"""

    database = _database(tmp_path, "rescan-unchanged.db")
    service = V4RevisionService(database)
    store = V4PlaybackStore(database)
    path = "Show/Season 1/Show.S01E01.mkv"

    first = _local_entry(path, root_id="root-r", scan_id="scan-r1", size=1000)
    service.create_draft("rev-r1", [first])
    service.confirm("rev-r1")
    with database.connect() as conn:
        before = dict(
            conn.execute(
                "SELECT work_id, episode_id, asset_id FROM revision_bindings WHERE revision_id = 'rev-r1'"
            ).fetchone()
        )
        evidence_count = conn.execute("SELECT COUNT(*) FROM source_evidence").fetchone()[0]
    store.save_progress(before["work_id"], before["episode_id"], before["asset_id"], 123.0, 1440.0, False)

    second = _local_entry(path, root_id="root-r", scan_id="scan-r2", size=1000)
    assert second[0].evidence_id != first[0].evidence_id
    service.create_draft("rev-r2", [second])
    service.confirm("rev-r2")
    with database.connect() as conn:
        after = dict(
            conn.execute(
                "SELECT work_id, episode_id, asset_id FROM revision_bindings WHERE revision_id = 'rev-r2'"
            ).fetchone()
        )
        assert conn.execute("SELECT COUNT(*) FROM source_evidence").fetchone()[0] == evidence_count + 1

    assert after == before
    assert store.get_progress(after["episode_id"], after["asset_id"])["position"] == 123


def test_size_replacement_creates_new_asset_without_copying_progress(tmp_path):
    """CHECK-005C: 明确 size 变化 → 新 Asset 内容代次，且不复制秒级进度。"""

    database = _database(tmp_path, "rescan-replaced.db")
    service = V4RevisionService(database)
    store = V4PlaybackStore(database)
    path = "Show/Season 1/Show.S01E01.mkv"

    first = _local_entry(path, root_id="root-x", scan_id="scan-x1", size=1000)
    service.create_draft("rev-x1", [first])
    service.confirm("rev-x1")
    with database.connect() as conn:
        before = dict(
            conn.execute(
                "SELECT work_id, episode_id, asset_id FROM revision_bindings WHERE revision_id = 'rev-x1'"
            ).fetchone()
        )
    store.save_progress(before["work_id"], before["episode_id"], before["asset_id"], 123.0, 1440.0, False)

    second = _local_entry(path, root_id="root-x", scan_id="scan-x2", size=2048)
    service.create_draft("rev-x2", [second])
    service.confirm("rev-x2")
    with database.connect() as conn:
        after = dict(
            conn.execute(
                "SELECT work_id, episode_id, asset_id FROM revision_bindings WHERE revision_id = 'rev-x2'"
            ).fetchone()
        )

    assert after["work_id"] == before["work_id"]
    assert after["episode_id"] == before["episode_id"]
    assert after["asset_id"] != before["asset_id"]
    assert store.get_progress(before["episode_id"], before["asset_id"])["position"] == 123
    with pytest.raises(KeyError):
        store.get_progress(after["episode_id"], after["asset_id"])


def test_movie_and_unresolved_progress_survive_unchanged_rescan(tmp_path):
    """CHECK-005C: 电影（movie: 键）与未定位条目在不变重扫下同样续接同一 Asset。"""

    database = _database(tmp_path, "rescan-movie-unknown.db")
    service = V4RevisionService(database)
    store = V4PlaybackStore(database)

    movie = _local_entry("Movie/Show 剧场版 [Movie].mkv", root_id="root-mu", scan_id="scan-mu1", size=10)
    unknown = _local_entry("Show/未知内容甲.mkv", root_id="root-mu", scan_id="scan-mu1", size=20)
    service.create_draft("rev-mu1", [movie, unknown])
    service.confirm("rev-mu1")
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT rb.work_id, rb.episode_id, rb.asset_id, se.relative_path
            FROM revision_bindings rb
            JOIN source_evidence se ON se.evidence_id = rb.evidence_id
            """
        ).fetchall()
    by_path = {str(row["relative_path"]): dict(row) for row in rows}
    assert len(by_path) == 2
    for row in by_path.values():
        store.save_progress(
            row["work_id"], _progress_key(row), row["asset_id"], 123.0, 600.0, False
        )

    movie2 = _local_entry("Movie/Show 剧场版 [Movie].mkv", root_id="root-mu", scan_id="scan-mu2", size=10)
    unknown2 = _local_entry("Show/未知内容甲.mkv", root_id="root-mu", scan_id="scan-mu2", size=20)
    service.create_draft("rev-mu2", [movie2, unknown2])
    service.confirm("rev-mu2")
    with database.connect() as conn:
        rows2 = conn.execute(
            """
            SELECT rb.work_id, rb.episode_id, rb.asset_id, se.relative_path
            FROM revision_bindings rb
            JOIN source_evidence se ON se.evidence_id = rb.evidence_id
            WHERE rb.revision_id = 'rev-mu2'
            """
        ).fetchall()

    by_path2 = {str(row["relative_path"]): dict(row) for row in rows2}
    assert by_path2 == by_path
    for row in by_path2.values():
        assert store.get_progress(_progress_key(row), row["asset_id"])["position"] == 123


def test_duplicate_confirm_is_idempotent(tmp_path):
    """CHECK-005C: 重复确认同一 revision 不注册新实体、不重复入队任务。"""

    database = _database(tmp_path, "rescan-idempotent.db")
    service = V4RevisionService(database)
    first = _local_entry(
        "Show/Season 1/Show.S01E01.mkv", root_id="root-i", scan_id="scan-i1", size=100
    )
    service.create_draft("rev-i", [first])
    service.confirm("rev-i")
    tables = ("works", "episodes", "assets", "revision_bindings", "jobs")

    def counts():
        with database.connect() as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }

    snapshot = counts()
    service.confirm("rev-i")

    assert counts() == snapshot
    assert service.get_status("rev-i") == "confirmed"


# --- CHECK-005D：置换、重读与签名 -------------------------------------------


def test_input_permutation_keeps_canonical_title_and_relations(tmp_path):
    """CHECK-005D: 输入顺序与 evidence_id 顺序变化不改变规范标题与逻辑关系。"""

    database = _database(tmp_path, "permutation.db")
    service = V4RevisionService(database)
    pool = {
        "caps": _pair("Show/Season 1/SHOW.S01E01.mkv", 1, title="SHOW", season=1, episode=1),
        "normal": _pair("Show/Season 1/Show.S01E02.mkv", 2, title="Show", season=1, episode=2),
        "other": _pair("另一部/另一部.S01E01.mkv", 3, title="另一部", season=1, episode=1),
    }
    baseline: tuple | None = None
    for index, order in enumerate(itertools.permutations(("caps", "normal", "other"))):
        root_id = f"root-perm-{index}"
        scan_id = f"scan-perm-{index}"
        entries = []
        for key in order:
            evidence, facts = pool[key]
            evidence_id = f"ev-perm-{index}-{key}"
            entries.append(
                (
                    replace(evidence, evidence_id=evidence_id, root_id=root_id, scan_id=scan_id),
                    replace(
                        facts,
                        evidence_id=evidence_id,
                        parsed_fact_id=f"facts-{evidence_id}",
                    ),
                )
            )
        revision_id = f"rev-perm-{index}"
        graph = service.create_draft(revision_id, entries)
        show = next(work for work in graph.works if work.preferred_title == "Show")
        signature = tuple(
            sorted(
                (episode.season_identity_key, episode.identity_key)
                for episode in graph.episodes
                if episode.work_key == show.work_key
            )
        )
        if baseline is None:
            baseline = signature
        else:
            assert signature == baseline, order
        service.confirm(revision_id)

    with database.connect() as conn:
        titles = sorted(
            {
                str(row["preferred_title"])
                for row in conn.execute("SELECT preferred_title FROM works").fetchall()
            }
        )
    assert titles == ["Show", "另一部"]


def test_semantic_signature_is_stable_while_digest_detects_other_observations():
    """CHECK-005D: 语义签名忽略观察 ID；保护快照 digest 会随观察集合变化。"""

    entries = _tree_entries(
        "Show/Season 1/Show.S01E01.mkv", root_id="root-d", scan_id="scan-d1"
    )
    renamed = [
        (
            replace(evidence, evidence_id=f"ev-other-{index}"),
            replace(facts, evidence_id=f"ev-other-{index}"),
        )
        for index, (evidence, facts) in enumerate(entries)
    ]

    first_graph = MediaResolver().resolve(entries)
    second_graph = MediaResolver().resolve(renamed)

    assert graph_semantic_digest(first_graph) == graph_semantic_digest(second_graph)
    assert V4RevisionService._graph_digest(first_graph) != V4RevisionService._graph_digest(second_graph)


def test_draft_reread_matches_confirmed_result(tmp_path):
    """CHECK-005D: 草稿重读后再确认，规范标题、类别与逻辑关系保持一致。"""

    database = _database(tmp_path, "draft-reread.db")
    service = V4RevisionService(database)
    entries = _tree_entries(
        "\n".join(["Show/Season 1/Show.S01E01.mkv", "Show/Season 2/Show.S02E01.mkv"]),
        root_id="root-rr",
        scan_id="scan-rr",
    )
    draft_graph = service.create_draft("rev-rr", entries)
    reread_graph = service.load_draft_graph("rev-rr")

    assert [work.preferred_title for work in reread_graph.works] == [
        work.preferred_title for work in draft_graph.works
    ]
    assert [work.media_type for work in reread_graph.works] == [
        work.media_type for work in draft_graph.works
    ]
    assert _graph_partitions(reread_graph) == _graph_partitions(draft_graph)

    service.confirm("rev-rr")
    with database.connect() as conn:
        rows, work_map, episode_map, _asset_map = _sql_partitions(conn, "rev-rr")
    assert rows
    assert {frozenset(items) for items in work_map.values()} == _graph_partitions(draft_graph)["works"]
    assert {frozenset(items) for items in episode_map.values()} == _graph_partitions(draft_graph)["episodes"]
