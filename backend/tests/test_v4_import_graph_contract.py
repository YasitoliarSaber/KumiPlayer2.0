"""CHECK-004A/B/C：准入集合、确定性归组与统一 Episode 关系。

断言只经过真实解析入口（``V4Parser.parse``）与真实解析器（``MediaResolver``）：
准入集合、规范标题置换不变、排除项不改变正片语义、未知/绝对/多版本/多集同
Asset 的 Episode 关系都在图上核对，不写实现细节、不访问源盘。
"""

from __future__ import annotations

import itertools
from dataclasses import replace

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.resolution.candidates import plan_work_candidates
from app.media_v4.resolution.identity_contract import graph_semantic_digest
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.sources.scanner import build_directory_tree_evidence

PARSER = V4Parser()


def _pair(
    path: str,
    index: int,
    *,
    title: str,
    season: int | None,
    episode: int | None,
    absolute: int | None = None,
    episode_range: tuple[int, int] | None = None,
    year: int | None = 2024,
    edition_tags: tuple[str, ...] = (),
    root_id: str = "root-graph",
    scan_id: str = "scan-graph",
) -> tuple[SourceEvidence, ParsedFacts]:
    """一个可播放正片条目的未解析事实（解析层已由 STEP-003 单独验收）。"""

    evidence = SourceEvidence(
        evidence_id=f"ev-graph-{index:03d}",
        scan_id=scan_id,
        root_id=root_id,
        source_key=path,
        relative_path=path,
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-graph-{index:03d}",
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
        absolute_episode_candidate=absolute,
        episode_range=episode_range,
        edition_tags=edition_tags,
        confidence="high",
    )
    return evidence, facts


def _tree(text: str, *, root_id: str, scan_id: str, provider: str = "baidu"):
    """真实扫描入口：目录树文本 → evidence + 解析事实。"""

    _scan, evidence = build_directory_tree_evidence(
        text, root_id=root_id, scan_id=scan_id, provider=provider
    )
    return normalize_batch_parsed_facts(
        [(item, PARSER.parse(item, root_container="动画")) for item in evidence]
    )


def _episode_signature(graph, work_key: str) -> tuple:
    return tuple(
        sorted(
            (episode.season_identity_key, episode.identity_key, episode.edition_key)
            for episode in graph.episodes
            if episode.work_key == work_key
        )
    )


def _work_key_for(graph, title: str) -> str:
    return next(
        work.work_key for work in graph.works if work.preferred_title == title
    )


# --- CHECK-004A：准入集合、规范标题与置换不变 --------------------------------


def test_permutations_keep_canonical_title_and_logical_keys():
    """CHECK-004A: SHOW/Show 的任意顺序置换得到同一规范标题、类别与逻辑键。"""

    def build(order: tuple[str, ...]):
        pool = {
            "caps": _pair("Show/Season 1/SHOW.S01E01.mkv", 1, title="SHOW", season=1, episode=1),
            "normal": _pair("Show/Season 1/Show.S01E02.mkv", 2, title="Show", season=1, episode=2),
            "unrelated": _pair("另一部/另一部.S01E01.mkv", 3, title="另一部", season=1, episode=1),
        }
        return [pool[key] for key in order]

    baseline_keys: tuple | None = None
    baseline_digest: str | None = None
    for order in itertools.permutations(("caps", "normal", "unrelated")):
        graph = MediaResolver().resolve(build(order))
        show_key = _work_key_for(graph, "Show")
        show = next(work for work in graph.works if work.work_key == show_key)
        assert show.preferred_title == "Show", order
        assert show.media_type == "tv"
        signature = _episode_signature(graph, show_key)
        assert len(signature) == 2, order
        if baseline_keys is None:
            baseline_keys = signature
            baseline_digest = graph_semantic_digest(graph)
        else:
            assert signature == baseline_keys, order
            assert graph_semantic_digest(graph) == baseline_digest, order


def test_adding_unrelated_work_does_not_change_existing_group():
    """CHECK-004A: 加入另一部作品不改变原组的标题、类别与逻辑键。"""

    base_entries = [
        _pair("Show/Season 1/Show.S01E01.mkv", 1, title="Show", season=1, episode=1),
        _pair("Show/Season 2/Show.S02E01.mkv", 2, title="Show", season=2, episode=1),
    ]
    base_graph = MediaResolver().resolve(base_entries)
    base_key = _work_key_for(base_graph, "Show")
    base_signature = _episode_signature(base_graph, base_key)
    base_work = next(work for work in base_graph.works if work.work_key == base_key)

    extended_graph = MediaResolver().resolve(
        [*base_entries, _pair("无关作品/无关作品.S01E01.mkv", 9, title="无关作品", season=1, episode=1)]
    )
    extended_key = _work_key_for(extended_graph, "Show")
    extended_work = next(work for work in extended_graph.works if work.work_key == extended_key)

    assert extended_key == base_key
    assert extended_work.preferred_title == base_work.preferred_title
    assert extended_work.media_type == base_work.media_type
    assert extended_work.card_type == base_work.card_type
    assert _episode_signature(extended_graph, extended_key) == base_signature


def test_explicit_year_conflict_is_not_swallowed_by_min_year():
    """CHECK-004A: 同名但明确年份冲突的两部作品不能被 min(year) 吞并成一个 Work。"""

    entries = [
        _pair("Show/Season 1/Show.S01E01.mkv", 1, title="Show", season=1, episode=1, year=2024),
        _pair("Show2/Season 1/Show.S01E01.mkv", 2, title="Show", season=1, episode=1, year=1999),
    ]
    graph = MediaResolver().resolve(entries)

    show_works = [work for work in graph.works if work.preferred_title == "Show"]
    assert len(show_works) == 2
    assert {work.year for work in show_works} == {1999, 2024}
    assert len({work.work_key for work in show_works}) == 2
    assert len(graph.episodes) == 2


# --- CHECK-004B：排除项不影响正片语义 ---------------------------------------


def test_excluded_special_does_not_change_admitted_semantics():
    """CHECK-004B: 排除条目（含高置信 Provider 提示）不改变正片语义与候选输入。"""

    admitted_tree = "\n".join(
        [
            "Show/Season 1/Show.S01E01.mkv",
            "Show/Season 1/Show.S01E02.mkv",
        ]
    )
    with_excluded_tree = "\n".join(
        [
            "Show/Season 1/Show.S01E01.mkv",
            "Show/Season 1/Show.S01E02.mkv",
            "Show/Specials/Show.SP01.mkv",
            "Show/Show [NCOP01].mkv",
        ]
    )
    admitted = _tree(admitted_tree, root_id="root-admit", scan_id="scan-admit")
    with_excluded = _tree(with_excluded_tree, root_id="root-admit", scan_id="scan-admit-2")

    excluded_ids = {
        evidence.evidence_id
        for evidence, facts in with_excluded
        if not facts.is_importable or facts.is_auxiliary
    }
    assert excluded_ids, "样本中必须存在被排除的附属内容"

    # 排除条目即使带上高置信 Provider 身份与更多语义证据也不改变结论。
    with_excluded = [
        (
            evidence,
            replace(facts, tmdb_hint_id=42, tmdb_hint_type="tv", series_group="Show")
            if evidence.evidence_id in excluded_ids
            else facts,
        )
        for evidence, facts in with_excluded
    ]

    admitted_graph = MediaResolver().resolve(admitted)
    full_graph = MediaResolver().resolve(with_excluded)
    bound_ids = {
        evidence_id
        for episode in full_graph.episodes
        for evidence_id in episode.asset_evidence_ids
    } | {
        evidence_id
        for asset in full_graph.work_assets
        for evidence_id in asset.asset_evidence_ids
    }

    assert bound_ids.isdisjoint(excluded_ids)
    assert graph_semantic_digest(full_graph) == graph_semantic_digest(admitted_graph)

    queries: list[tuple[str, tuple[str, ...], str]] = []

    def search(work_key, work_queries, year, media_type):
        queries.append((work_key, tuple(work_queries), str(media_type)))
        return []

    plan_work_candidates(admitted_graph, admitted, search)
    baseline_queries = list(queries)
    queries.clear()
    plan_work_candidates(full_graph, with_excluded, search)
    assert queries == baseline_queries


def test_directory_nfo_ambiguity_does_not_inject_into_any_work():
    """CHECK-004B: 目录级 NFO 归属不唯一时不能把提示注入任一 Work 的候选。"""

    tree = "\n".join(
        [
            "合集/作品甲/Season 1/作品甲.S01E01.mkv",
            "合集/作品乙/Season 1/作品乙.S01E01.mkv",
            "合集/tvshow.nfo",
        ]
    )
    parsed = _tree(tree, root_id="root-nfo", scan_id="scan-nfo")
    graph = MediaResolver().resolve(parsed)
    entries = [entry for entry in parsed if entry[0].entry_kind == "video"]

    _candidates, merge_map, issues = plan_work_candidates(graph, entries, lambda *_: [])
    assert merge_map == {}
    assert len(graph.works) == 2
    assert not [issue for issue in issues if issue.code == "candidate_ambiguous"]


# --- CHECK-004C：Episode 关系 ------------------------------------------------


def test_two_unnumbered_files_are_two_episodes_with_stable_anchors():
    """CHECK-004C: 两个无编号文件是两个 Episode，各自以 SourceFile 槽位为锚点。"""

    tree = "\n".join(["Show/未知内容甲.mkv", "Show/未知内容乙.mkv"])
    parsed = _tree(tree, root_id="root-unknown", scan_id="scan-unknown")
    graph = MediaResolver().resolve(parsed)

    assert len(graph.works) == 1
    assert len(graph.episodes) == 2
    identities = {episode.identity_key for episode in graph.episodes}
    assert len(identities) == 2
    assert all(episode.local_episode_number is None for episode in graph.episodes)
    assert all(episode.local_season_number is None for episode in graph.episodes)
    assert {episode.asset_evidence_ids for episode in graph.episodes} == {
        (parsed[0][0].evidence_id,),
        (parsed[1][0].evidence_id,),
    }


def test_absolute_only_episodes_keep_local_numbers_null():
    """CHECK-004C: 只有绝对编号的 13/14 是两个逻辑 Episode，本地季集保持 null。"""

    tree = "\n".join(["Show/Show ABS13.mkv", "Show/Show ABS14.mkv"])
    parsed = _tree(tree, root_id="root-absolute", scan_id="scan-absolute")
    graph = MediaResolver().resolve(parsed)

    assert len(graph.episodes) == 2
    assert sorted(episode.absolute_episode_number for episode in graph.episodes) == [13, 14]
    assert all(episode.local_season_number is None for episode in graph.episodes)
    assert all(episode.local_episode_number is None for episode in graph.episodes)
    assert len({episode.identity_key for episode in graph.episodes}) == 2


def test_two_qualities_of_one_episode_share_one_episode_with_two_assets():
    """CHECK-004C: 同一 S1E1 的两个版本是一个逻辑 Episode、两个 Asset。"""

    entries = [
        _pair("Show/Season 1/Show.S01E01.1080p.mkv", 1, title="Show", season=1, episode=1),
        _pair("Show/Season 1/Show.S01E01.2160p.mkv", 2, title="Show", season=1, episode=1),
    ]
    graph = MediaResolver().resolve(entries)

    assert len(graph.episodes) == 1
    assert len(graph.episodes[0].asset_evidence_ids) == 2
    assert graph.episodes[0].local_season_number == 1
    assert graph.episodes[0].local_episode_number == 1


def test_two_semantic_editions_are_one_logical_episode_with_two_editions():
    """CHECK-004C: 两个语义版别是同一逻辑 Episode 的两条 edition 行。"""

    entries = [
        _pair("Show/Season 1/Show.S01E01.mkv", 1, title="Show", season=1, episode=1),
        _pair(
            "Show/Season 1/Show.S01E01.导演剪辑版.mkv",
            2,
            title="Show",
            season=1,
            episode=1,
            edition_tags=("director-cut",),
        ),
        _pair(
            "Show/Season 1/Show.S01E01.Theatrical.Cut.mkv",
            3,
            title="Show",
            season=1,
            episode=1,
            edition_tags=("theatrical",),
        ),
    ]
    graph = MediaResolver().resolve(entries)

    show_key = _work_key_for(graph, "Show")
    editions = {
        (episode.identity_key, episode.edition_key)
        for episode in graph.episodes
        if episode.work_key == show_key
    }
    assert len({identity for identity, _edition in editions}) == 1, editions
    assert len({edition for _identity, edition in editions}) >= 2, editions


def test_multi_episode_file_binds_two_logical_episodes_to_one_asset():
    """CHECK-004C: E01-E02 合集文件绑定两个 Episode，且共享同一 Asset。"""

    entries = [
        _pair(
            "Show/Season 1/Show.S01E01-E02.mkv",
            1,
            title="Show",
            season=1,
            episode=1,
            episode_range=(1, 2),
        ),
    ]
    graph = MediaResolver().resolve(entries)

    assert len(graph.episodes) == 2
    assert {episode.local_episode_number for episode in graph.episodes} == {1, 2}
    assert all(
        episode.asset_evidence_ids == ("ev-graph-001",) for episode in graph.episodes
    )
    assert len({episode.identity_key for episode in graph.episodes}) == 2


def test_conflicting_absolute_numbers_stay_provisional_and_separate():
    """CHECK-004C: 同一 Local 的互斥绝对号按独立 provisional 处理，不合并任取。"""

    entries = [
        _pair("Show/Season 2/Show.S02E01.mkv", 1, title="Show", season=2, episode=1, absolute=13),
        _pair("Show/Season 2/Show.S02E01.重制.mkv", 2, title="Show", season=2, episode=1, absolute=14),
    ]
    graph = MediaResolver().resolve(entries)

    assert len(graph.episodes) == 2
    assert {episode.asset_evidence_ids for episode in graph.episodes} == {
        ("ev-graph-001",),
        ("ev-graph-002",),
    }
    assert all(episode.season_kind == "unassigned" for episode in graph.episodes)
    assert any(issue.code == "absolute_episode_conflict" for issue in graph.issues)
