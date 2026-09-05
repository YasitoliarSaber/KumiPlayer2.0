"""Yuru Camp 跨批次作品身份稳定性（3.2 P0）。

路径形态脱敏自真实库只读提取（仅相对目录与文件名，无凭据、无播放
历史、无绝对根路径）。按真实顺序重放「扫描 → 草稿 → 确认」两轮，
断言主系列不被季度标题改名、外传与电影不被吸回主系列、第二轮不产
生重复 Work。

只读诊断结论（2026-09-05）：真实库中 `Yuru Camp Season 2` 错误 Work
创建于 2026-08-24，早于作品边界修复与统一身份修复；本测试用于证明
当前代码在连续多次导入确认下不再复现。
"""

from __future__ import annotations

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.revisions.service import V4RevisionService

# 脱敏后的真实来源路径形态（release 组名统一替换为 [Group]）。
YURU_CAMP_SOURCE_PATHS = [
    # 主系列第一季 + SP
    "[Group] Yuru Camp/[Group] Yuru Camp [Ma10p_1080p]/[Group] Yuru Camp [01][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp [Ma10p_1080p]/[Group] Yuru Camp [04][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp [Ma10p_1080p]/[Group] Yuru Camp [07][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp [Ma10p_1080p]/[Group] Yuru Camp [Survival Camp][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp [Ma10p_1080p]/SPs/[Group] Yuru Camp [IV01][Ma10p_1080p][x265_aac].mkv",
    # 主系列第二季 + SP
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [02][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [03][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [05][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [12][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [13][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [Mystery Camp][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/SPs/[Group] Yuru Camp Season 2 [IV02_1][Ma10p_1080p][x265_aac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/SPs/[Group] Yuru Camp Season 2 [IV03_2][Ma10p_1080p][x265_aac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/SPs/[Group] Yuru Camp Season 2 [Tabisuru Shima Rin][Ma10p_1080p][x265_aac].mkv",
    # 第三季（现行发布目录形态）
    "[Group] Yuru Camp/[Group] Yuru Camp Season 3 [Ma10p_1080p]/[Group] Yuru Camp Season 3 [01][Ma10p_1080p][x265_flac].mkv",
    # 独立泡面番外传 + SP（含演唱会 SP）
    "[Group] Yuru Camp/[Group] Heya Camp [Ma10p_1080p]/[Group] Heya Camp [01][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Heya Camp [Ma10p_1080p]/[Group] Heya Camp [09][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Heya Camp [Ma10p_1080p]/[Group] Heya Camp [12][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Heya Camp [Ma10p_1080p]/SPs/[Group] Heya Camp [Yuru Camp 2019 Music Concert][Ma10p_1080p][x265_aac].mkv",
    "[Group] Yuru Camp/[Group] Heya Camp [Ma10p_1080p]/SPs/[Group] Heya Camp [CM Collection 01][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Heya Camp [Ma10p_1080p]/[Group] Yuru Camp [Heya Camp EP00][Ma10p_1080p][x265_flac].mkv",
    # 动画电影 + SP
    "[Group] Yuru Camp/[Group] Yuru Camp Movie [Ma10p_1080p]/[Group] Yuru Camp Movie [Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Movie [Ma10p_1080p]/SPs/[Group] Yuru Camp Movie [After Talk][Ma10p_1080p][x265_aac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Movie [Ma10p_1080p]/SPs/[Group] Yuru Camp Movie [Making Documentary][Ma10p_1080p][x265_aac].mkv",
]


def _make_evidence(index: int, relative_path: str, scan_id: str) -> SourceEvidence:
    return SourceEvidence(
        evidence_id=f"ev-{scan_id}-{index:03d}",
        scan_id=scan_id,
        root_id="root-stability",
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider="local",
        ingest_method="local_scan",
        source_locator=f"K:\\媒体库\\{relative_path}",
        playback_locator=f"K:\\媒体库\\{relative_path}",
    )


def _import_round(database: V4Database, revision_id: str, scan_id: str):
    """一轮完整导入：扫描证据 → 解析 → 草稿 → 确认。"""

    from datetime import UTC, datetime

    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-stability', 'local', 'local_scan', ?, ?)",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        generation = int(conn.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 FROM source_scans WHERE root_id = 'root-stability'"
        ).fetchone()[0])
        conn.execute(
            "INSERT OR IGNORE INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES (?, 'root-stability', ?, 'completed', ?)",
            (scan_id, generation, datetime.now(UTC).isoformat()),
        )
    repository = V4Repository(database)
    evidence = [_make_evidence(index, path, scan_id) for index, path in enumerate(YURU_CAMP_SOURCE_PATHS)]
    repository.save_scan_evidence_bulk(evidence)
    parser = V4Parser()
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container="Yuru Camp")) for item in evidence]
    )
    service = V4RevisionService(database)
    graph = service.create_draft(
        revision_id,
        parsed,
        root_id="root-stability",
        scan_id=scan_id,
        source_provider="local",
        source_mode="local",
    )
    service.confirm(revision_id)
    return graph


def test_draft_graph_keeps_movie_specials_in_one_movie_work(tmp_path):
    """电影正文与其 SP 必须在确认前就形成同一个电影 Work。"""

    database = V4Database(tmp_path / "draft-graph.db")
    database.initialize()

    parser = V4Parser()
    evidence = [
        _make_evidence(index, path, "scan-draft-graph")
        for index, path in enumerate(YURU_CAMP_SOURCE_PATHS)
    ]
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container="Yuru Camp")) for item in evidence]
    )
    raw_graph = MediaResolver().resolve(parsed)
    assert {work.preferred_title for work in raw_graph.works} == {
        "Yuru Camp",
        "Heya Camp",
        "Yuru Camp Movie",
    }
    assert len(raw_graph.works) == 3
    reversed_graph = MediaResolver().resolve(list(reversed(parsed)))
    assert {work.preferred_title for work in reversed_graph.works} == {
        "Yuru Camp",
        "Heya Camp",
        "Yuru Camp Movie",
    }
    assert len(reversed_graph.works) == 3
    permuted_graph = MediaResolver().resolve(
        [
            parsed[index]
            for index in (
                4, 11, 12, 19, 20, 21, 22, 23, 0, 5, 14, 15,
                16, 17, 18, 1, 2, 3, 6, 7, 8, 9, 10, 13,
            )
        ]
    )
    assert {work.preferred_title for work in permuted_graph.works} == {
        "Yuru Camp",
        "Heya Camp",
        "Yuru Camp Movie",
    }
    assert len(permuted_graph.works) == 3

    facts_by_path = {evidence.relative_path: facts for evidence, facts in parsed}
    season_two_facts = next(
        facts for path, facts in facts_by_path.items()
        if "Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [02]" in path
    )
    heya_facts = next(
        facts for path, facts in facts_by_path.items()
        if "Heya Camp [Ma10p_1080p]/[Group] Heya Camp [01]" in path
    )
    movie_special_facts = next(
        facts for path, facts in facts_by_path.items()
        if "Yuru Camp Movie [Ma10p_1080p]/SPs/" in path
    )
    assert (season_two_facts.series_group, season_two_facts.relation_type, season_two_facts.season_candidate) == (
        "Yuru Camp",
        "main",
        2,
    )
    assert (heya_facts.card_type, heya_facts.relation_type, heya_facts.media_type) == (
        "standalone",
        "spin_off",
        "tv",
    )
    assert (movie_special_facts.card_type, movie_special_facts.relation_type, movie_special_facts.media_type) == (
        "standalone",
        "movie",
        "movie",
    )

    graph = _import_round(database, "rev-draft-graph", "scan-draft-graph")

    assert {work.preferred_title for work in graph.works} == {
        "Yuru Camp",
        "Heya Camp",
        "Yuru Camp Movie",
    }
    assert len(graph.works) == 3

    movie_evidence_ids = {
        f"ev-scan-draft-graph-{index:03d}"
        for index, path in enumerate(YURU_CAMP_SOURCE_PATHS)
        if "Yuru Camp Movie" in path
    }
    movie_work_keys = {
        asset.work_key
        for asset in graph.work_assets
        if movie_evidence_ids.intersection(asset.asset_evidence_ids)
    }
    assert len(movie_work_keys) == 1
    movie_work = next(work for work in graph.works if work.preferred_title == "Yuru Camp Movie")
    assert movie_work.work_key in movie_work_keys

    evidence_work_keys: dict[str, str] = {}
    for episode in graph.episodes:
        for evidence_id in episode.asset_evidence_ids:
            previous = evidence_work_keys.setdefault(evidence_id, episode.work_key)
            assert previous == episode.work_key
    for asset in graph.work_assets:
        for evidence_id in asset.asset_evidence_ids:
            previous = evidence_work_keys.setdefault(evidence_id, asset.work_key)
            assert previous == asset.work_key
    assert len(evidence_work_keys) == len(parsed)


def _work_snapshot(database: V4Database) -> dict:
    with database.connect() as conn:
        works = [
            {
                "work_id": str(row["work_id"]),
                "preferred_title": str(row["preferred_title"]),
                "identity_key": str(row["identity_key"]),
                "work_type": str(row["work_type"]),
            }
            for row in conn.execute("SELECT work_id, preferred_title, identity_key, work_type FROM works").fetchall()
        ]
        season_rows = [
            {
                "work_id": str(row["work_id"]),
                "season_number": int(row["local_season_number"] or 0),
                "episode_count": int(row["episode_count"] or 0),
            }
            for row in conn.execute(
                """
                SELECT s.work_id, s.local_season_number, COUNT(e.episode_id) AS episode_count
                FROM seasons s LEFT JOIN episodes e ON e.season_id = s.season_id
                GROUP BY s.season_id
                """
            ).fetchall()
        ]
        bindings = [
            {"revision_id": str(row["revision_id"]), "work_id": str(row["work_id"])}
            for row in conn.execute("SELECT DISTINCT revision_id, work_id FROM revision_bindings").fetchall()
        ]
    return {"works": works, "seasons": season_rows, "bindings": bindings}


def test_two_rounds_of_import_keep_three_semantic_works_stable(tmp_path):
    database = V4Database(tmp_path / "stability.db")
    database.initialize()

    _import_round(database, "rev-round-1", "scan-round-1")
    after_first = _work_snapshot(database)

    _import_round(database, "rev-round-2", "scan-round-2")
    after_second = _work_snapshot(database)

    # 两轮各自只有三个语义作品：主系列 / 外传 / 电影。
    for snapshot in (after_first, after_second):
        titles = {work["preferred_title"] for work in snapshot["works"]}
        assert titles == {"Yuru Camp", "Heya Camp", "Yuru Camp Movie"}, titles
        # 第二轮不得新增重复 Work。
        assert len(snapshot["works"]) == 3

    # 主系列标题不被季度目录名覆盖；身份键跨轮保持稳定（具体键格式由
    # 解析器决定：结构系列键 series:… 或标题键 title:… 均可）。
    main = next(work for work in after_second["works"] if work["preferred_title"] == "Yuru Camp")
    assert "Season 2" not in main["preferred_title"]
    main_after_first = next(work for work in after_first["works"] if work["preferred_title"] == "Yuru Camp")
    assert main["identity_key"] == main_after_first["identity_key"]

    # 主系列季度归属：特别篇归 S0，常规季 1/2/3 都挂在主系列下且有集绑定。
    main_seasons = sorted(
        item["season_number"] for item in after_second["seasons"] if item["work_id"] == main["work_id"]
    )
    assert main_seasons == [0, 1, 2, 3]
    for number in (1, 2, 3):
        season = next(
            item for item in after_second["seasons"]
            if item["work_id"] == main["work_id"] and item["season_number"] == number
        )
        assert season["episode_count"] > 0
    specials = next(
        item for item in after_second["seasons"]
        if item["work_id"] == main["work_id"] and item["season_number"] == 0
    )
    assert specials["episode_count"] >= 5  # IV01 + Survival Camp + S2 的 SPs

    # 外传与电影是独立 Work，主系列季列表里没有它们的集。
    heya = next(work for work in after_second["works"] if work["preferred_title"] == "Heya Camp")
    movie = next(work for work in after_second["works"] if work["preferred_title"] == "Yuru Camp Movie")
    assert heya["work_id"] != main["work_id"]
    assert movie["work_id"] != main["work_id"]

    # 每个 revision 的绑定都指向本轮解析的三个语义作品，不出现第四个 work。
    for revision_id in ("rev-round-1", "rev-round-2"):
        bound = {
            item["work_id"] for item in after_second["bindings"] if item["revision_id"] == revision_id
        }
        assert bound == {main["work_id"], heya["work_id"], movie["work_id"]}


def test_second_round_does_not_rename_main_series_with_season_title(tmp_path):
    """确认历史错误（preferred_title 变成 Yuru Camp Season 2）不再复现。"""

    database = V4Database(tmp_path / "rename.db")
    database.initialize()
    _import_round(database, "rev-rename-1", "scan-rename-1")
    _import_round(database, "rev-rename-2", "scan-rename-2")

    with database.connect() as conn:
        titles = {
            str(row["preferred_title"])
            for row in conn.execute("SELECT preferred_title FROM works").fetchall()
        }
    assert "Yuru Camp Season 2" not in titles
