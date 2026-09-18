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
