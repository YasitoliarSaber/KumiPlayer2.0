"""合集目录不得被当成一部作品（用户反馈的严重问题）。

真因：解析器把**合集目录名**写进 `series_group`（relation_type=main），Resolver 的
系列归并于是把所有子作品提升为同一个 `series:<合集>` —— 200+ 个不同动画的文件会
变成同一部作品的剧集，身份键、刮削与剧集编号全错。

区分「合集」与「多季系列」必须靠整批：去掉季度后缀比基名——
``CLANNAD`` vs ``轻音少女`` 基名不同 → 合集；``Yuru Camp S1`` vs ``Yuru Camp S2``
基名相同 → 同一作品的多季（本条是反例，改动不能把它拆开）。
"""

from __future__ import annotations

from app.media_v4.parsing.parser import V4Parser
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


def _draft(tmp_path, paths: list[str], *, revision_id: str = "rev-collection"):
    database = V4Database(tmp_path / f"{revision_id}.db")
    database.initialize()
    parser = V4Parser()
    pairs = []
    for relative in paths:
        evidence = to_source_evidence(
            SourceEntry(
                root_id="root-c",
                scan_id="scan-c",
                provider="quark",
                ingest_method="openlist_scan",
                relative_path=relative,
                source_key=relative,
            )
        )
        pairs.append((evidence, parser.parse(evidence, root_container="")))
    return V4RevisionService(database).create_draft(
        revision_id, pairs, root_id="root-c", scan_id="scan-c"
    )


def test_collection_directory_does_not_become_one_work(tmp_path):
    graph = _draft(tmp_path, [
        "4k 京阿尼合集/C 4k Clannad/第一季/[Ygm] Clannad [08][Ma10p_2160p].mkv",
        "4k 京阿尼合集/K 4k 轻音少女/轻音少女 S01E01.mkv",
        "4k 京阿尼合集/H 4k 冰菓/冰菓 S01E01.mkv",
    ])

    titles = sorted(work.preferred_title for work in graph.works)
    assert titles == ["Clannad", "冰菓", "轻音少女"], f"合集被并成一部作品：{titles}"
    assert not any("京阿尼合集" in title for title in titles), "合集目录名不得成为作品"


def test_season_subdirectories_still_merge_into_one_work(tmp_path):
    """反证：以季目录（第 N 季）分层时同样只有一部作品。"""

    graph = _draft(tmp_path, [
        "摇曳露营/第1季/摇曳露营 S01E01.mkv",
        "摇曳露营/第2季/摇曳露营 S02E01.mkv",
    ], revision_id="rev-season-dirs")

    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "摇曳露营"


def test_series_with_naming_variants_is_never_treated_as_collection(tmp_path):
    """回归：作品名不叫"合集"时绝不能被拆卡（2026-09-18 实测回归）。

    忠实重放真实数据时发现：同一部 Yuru Camp 因为子目录基名不同
    （季节目录、发布组前缀变体、特典目录）被判定成"合集"，拆成 3 部作品，
    其中一部标题带 `Season 2`，在线匹配只有 27 分、无法自动采用。
    """

    graph = _draft(tmp_path, [
        "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Ma10p_1080p]/[Airota] Yuru Camp - 01.mkv",
        "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Ma10p_1080p]/[Airota] Yuru Camp S02E01.mkv",
        "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Ma10p_1080p]/SPs/[Airota] Yuru Camp SP01.mkv",
        "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [Ma10p_1080p]/[Airota] Heya Camp - 01.mkv",
    ], revision_id="rev-yuru-variants")

    titles = sorted(work.preferred_title for work in graph.works)
    assert not any("Season 2" in title for title in titles), f"季节目录变体被拆卡：{titles}"
    assert "Yuru Camp" in titles, titles
    assert "Heya Camp" in titles, "外传仍应是独立作品"


def test_collection_named_directory_with_multiple_works_still_splits(tmp_path):
    """反证：名称自称合集且确有多个不同作品时，仍必须各自成作品。"""

    graph = _draft(tmp_path, [
        "4k 物语系列/化物语/化物语 S01E01.mkv",
        "4k 物语系列/伪物语/伪物语 S01E01.mkv",
    ], revision_id="rev-monogatari-collection")

    titles = sorted(work.preferred_title for work in graph.works)
    assert titles == ["偽物語", "化物語"] or titles == ["伪物语", "化物语"] or len(titles) == 2, titles
    assert not any("物语系列" in title for title in titles), "合集名不得成为作品名"


def test_collection_name_detection_is_token_based():
    from app.media_v4.resolution.resolver import _looks_like_collection_name

    assert _looks_like_collection_name("京阿尼合集")
    assert _looks_like_collection_name("4k 物语系列")
    assert _looks_like_collection_name("CLANNAD Collection")
    # 作品名（含季/发布组/剧场版等变体）绝不能被当成合集
    for name in ("Yuru Camp", "摇曳露营", "CLANNAD", "凉宫春日的忧郁", "Heya Camp"):
        assert not _looks_like_collection_name(name), name


def test_resolver_reads_persisted_facts_not_just_fresh_parse(tmp_path):
    """回归（方法论）：必须覆盖"**已持久化事实** → resolver"这条路径。

    2026-09-18 的拆卡回归之所以两次没被复现，是因为当时只用**重新解析**的窄样本
    试跑；真实扫描是把**整批持久化事实**交给 resolver（合集/系列判定都是整批判定）。
    这里把事实写库再读回来，走与真实扫描相同的输入形态。
    """

    from app.media_v4.persistence.repositories import V4Repository

    database = V4Database(tmp_path / "persisted.db")
    database.initialize()
    repository = V4Repository(database)
    paths = [
        "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Ma10p_1080p]/[Airota] Yuru Camp - 01.mkv",
        "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Ma10p_1080p]/[Airota] Yuru Camp S02E01.mkv",
    ]
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-p', 'local', 'local_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-p', 'root-p', 1, 'completed', 'ready', 'now', 'now')"
        )
    parser = V4Parser()
    evidence_items = []
    for relative in paths:
        evidence = to_source_evidence(
            SourceEntry(
                root_id="root-p",
                scan_id="scan-p",
                provider="local",
                ingest_method="local_scan",
                relative_path=relative,
                source_key=relative,
            )
        )
        evidence_items.append((evidence, parser.parse(evidence, root_container="02_动漫")))
    with database.connect() as conn:
        repository.save_scan_evidence_bulk([item for item, _facts in evidence_items])
        repository.save_parsed_facts_bulk([facts for _item, facts in evidence_items])
    # 从库里读回（模拟真实扫描交给 resolver 的输入）
    reloaded_evidence = repository.list_scan_evidence("scan-p")
    pairs = [
        (item, repository.get_parsed_facts(_fact_id_for(database, item.evidence_id)))
        for item in reloaded_evidence
    ]
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(pairs)
    titles = sorted(work.preferred_title for work in graph.works)
    assert not any("Season 2" in title for title in titles), f"持久化事实路径被拆卡：{titles}"
    assert "Yuru Camp" in titles, titles


def _fact_id_for(database, evidence_id: str) -> str:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT parsed_fact_id FROM parsed_facts WHERE evidence_id = ? LIMIT 1", (evidence_id,)
        ).fetchone()
    assert row is not None, f"证据 {evidence_id} 没有持久化事实"
    return str(row["parsed_fact_id"])
