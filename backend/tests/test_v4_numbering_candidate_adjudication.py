"""STEP-003 / STEP-004：低上下文集号候选与同批裁决（F-004 / F-005）。

- 词法层只记录候选（decision_trace），不写成强事实；
- Resolver 用同批、同明确作品边界的正片证据裁决；
- 缺集不补齐、不跨作品合并、电影/外传/OVA 不被吞并；
- 无法定位的序列条目必须给出具体提示，而不是静默零问题。
"""

from __future__ import annotations

TITLE = "上伊那牡丹醉姿如百合"
MIXED_NAMES = (
    [f"上伊那牡丹S01E{index}.mkv" for index in range(1, 8)]
    + ["8.mkv", "9.mkv", f"{TITLE}11.mp4", f"{TITLE}12.mp4"]
)


def _evidence(index: int, name: str, relative_path: str, *, root_id: str = "root-mix", scan_id: str = "scan-mix"):
    from app.media_v4.domain.models import SourceEvidence

    return SourceEvidence(
        evidence_id=f"ev-{root_id}-{scan_id}-{index}",
        scan_id=scan_id,
        root_id=root_id,
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider="local",
        ingest_method="local_scan",
        source_locator=f"local://{relative_path}",
        playback_locator=f"local://{relative_path}",
    )


def _parse_entries(names, *, root_id: str = "root-mix", scan_id: str = "scan-mix", folder: str = TITLE):
    from app.media_v4.parsing.parser import V4Parser

    parser = V4Parser()
    return [
        (_evidence(index, name, f"{folder}/{name}", root_id=root_id, scan_id=scan_id), parser.parse(
            _evidence(index, name, f"{folder}/{name}", root_id=root_id, scan_id=scan_id)
        ))
        for index, name in enumerate(names)
    ]


def _candidates(facts) -> list[dict]:
    return [
        trace.value
        for trace in facts.decision_trace
        if str(getattr(trace, "field", "") or "") == "episode_number_candidate"
    ]


# ── CHECK-004A：词法与事实往返 ─────────────────────────────────────────────

def test_lexical_candidates_are_recorded_without_becoming_strong_facts():
    entries = _parse_entries(["8.mkv", "9.mkv", f"{TITLE}11.mp4", f"{TITLE}12.mp4"])
    facts = [item[1] for item in entries]

    assert [item["number"] for item in _candidates(facts[0])] == [8]
    assert _candidates(facts[0])[0]["rule_id"] == "bare_numeric_stem"
    assert _candidates(facts[1])[0]["number"] == 9
    assert _candidates(facts[2])[0] == {
        "number": 11, "title_prefix": TITLE, "rule_id": "title_suffix_number",
    }
    assert _candidates(facts[3])[0]["number"] == 12

    for item, trace in zip(facts, [trace for fact in facts for trace in fact.decision_trace
                                   if str(getattr(trace, "field", "") or "") == "episode_number_candidate"]):
        assert item.episode_candidate is None, "词法候选不得直接写成强集号事实"
        assert trace.origin == "local_unscoped"


def test_technical_numbers_never_become_episode_candidates():
    entries = _parse_entries(["2012.mkv", "1080p.mkv", "H264.mkv", f"{TITLE}2024.mp4"])
    for _evidence_item, facts in entries:
        assert _candidates(facts) == [], f"{facts.title_candidates} 不应产生集号候选"
        assert facts.episode_candidate is None


def test_explicit_numbering_and_specials_keep_existing_semantics():
    entries = _parse_entries(
        ["Show.S02E03.mkv", "Show.E03-E04.mkv", "Show.ABS13.mkv", "Show.S02.mkv", "Show.S01E05.mkv"]
    )
    by_name = {facts.episode_token_raw: facts for _evidence_item, facts in entries}
    season_episode = next(facts for _e, facts in entries if _e.relative_path.endswith("S02E03.mkv"))
    assert (season_episode.season_candidate, season_episode.episode_candidate) == (2, 3)
    assert season_episode.numbering.episode_origin == "explicit_filename"

    range_entry = next(facts for _e, facts in entries if _e.relative_path.endswith("E03-E04.mkv"))
    assert range_entry.episode_range == (3, 4)

    absolute_entry = next(facts for _e, facts in entries if _e.relative_path.endswith("ABS13.mkv"))
    assert absolute_entry.absolute_episode_candidate == 13

    assert by_name  # 至少解析出三种 token 形态，保证上面的查找有效

    special = _parse_entries(["Show/特别篇/Show.SP01.mkv"])[0][1]
    assert special.content_class == "attached_special"
    assert special.is_importable is False


def test_decision_trace_survives_persistence_roundtrip(tmp_path):
    """候选必须能随 ParsedFacts 落库并读回，否则 Resolver 拿不到证据。"""

    from app.media_v4.domain.models import ParsedFacts
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "roundtrip.db")
    database.initialize()
    entries = _parse_entries(["8.mkv"], folder=TITLE)
    evidence, facts = entries[0]
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'local', 'local_scan', 'now', 'now')",
            (evidence.root_id,),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES (?, ?, 1, 'completed', 'ready', 'now', 'now')",
            (evidence.scan_id, evidence.root_id),
        )
    from app.media_v4.persistence.repositories import V4Repository

    repository = V4Repository(database)
    repository.save_scan_evidence_bulk([evidence])
    repository.save_parsed_facts_bulk([facts])
    loaded = repository.get_parsed_facts(facts.parsed_fact_id)
    assert isinstance(loaded, ParsedFacts)
    restored = _candidates(loaded)
    assert restored and restored[0]["number"] == 8, restored


# ── CHECK-004B / CHECK-005A：同批裁决 ──────────────────────────────────────

def test_mixed_naming_batch_resolves_to_one_work_with_kept_gap():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(_parse_entries(MIXED_NAMES))

    assert len(graph.works) == 1, [work.work_key for work in graph.works]
    assert len(graph.episodes) == 11
    assert sorted(episode.local_episode_number for episode in graph.episodes) == [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12,
    ], "缺第 10 集不得被补齐，也不得把 11 改成 10"
    assert all(episode.local_season_number == 1 for episode in graph.episodes)
    physical = {
        evidence_id
        for episode in graph.episodes
        for evidence_id in episode.asset_evidence_ids
    }
    assert len(physical) == 11, "11 个物理视频必须各保留一个 Asset"


def test_input_order_does_not_change_the_result():
    from app.media_v4.resolution.resolver import MediaResolver

    forward = MediaResolver().resolve(_parse_entries(MIXED_NAMES))
    reversed_graph = MediaResolver().resolve(list(reversed(_parse_entries(MIXED_NAMES))))

    def shape(graph):
        return (
            sorted(work.work_key for work in graph.works),
            sorted(
                (episode.local_season_number, episode.local_episode_number)
                for episode in graph.episodes
            ),
            sorted(issue.code for issue in graph.issues),
        )

    assert shape(forward) == shape(reversed_graph)


def test_candidates_stay_unknown_when_series_anchors_are_removed():
    from app.media_v4.resolution.resolver import MediaResolver

    # 只留一个正片锚点：弱上下文，且没有显式目录季号 → 不得提升。
    graph = MediaResolver().resolve(_parse_entries(["上伊那牡丹S01E1.mkv", "8.mkv", "9.mkv"]))

    numbers = {
        episode.local_episode_number
        for episode in graph.episodes
        if episode.local_episode_number is not None
    }
    assert 8 not in numbers and 9 not in numbers, numbers
    codes = {issue.code for issue in graph.issues}
    assert "media_type_unresolved" in codes, codes
    # 弱上下文也不得把类型未知的条目强行并进正片 Work（不提升就不合并）。
    assert len(graph.works) == 2, [work.work_key for work in graph.works]


def test_single_bare_number_file_is_not_promoted_without_series_context():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(_parse_entries(["86.mkv"]))

    assert len(graph.works) == 1
    assert all(episode.local_episode_number is None for episode in graph.episodes)
    codes = {issue.code for issue in graph.issues}
    assert "media_type_unresolved" not in codes, "没有序列上下文不应新增类型警告"
    assert "episode_number_unresolved" not in codes, "未知类型不是“预期正片缺集”"


def test_two_roots_with_the_same_title_keep_separate_physical_assets():
    """同名但不同根不产生第二套集号身份，也不丢物理文件。

    实测（本用例）：两个根的同名作品命中同一个 TV Work，相同集号合成一个
    Episode 并各自保留一个 Asset（每集 2 个版本）。这是 Resolver 既有的
    “本地编号为主键”身份语义，本任务不修改它；这里只锁定“不因同名丢掉
    任何一个物理文件、也不产生重复集号”。
    """

    from app.media_v4.resolution.resolver import MediaResolver

    left = _parse_entries(MIXED_NAMES, root_id="root-left", scan_id="scan-left")
    right = _parse_entries(MIXED_NAMES, root_id="root-right", scan_id="scan-right")
    graph = MediaResolver().resolve(left + right)

    assert len(graph.works) == 1, [work.work_key for work in graph.works]
    assert len(graph.episodes) == 11
    assert sorted(episode.local_episode_number for episode in graph.episodes) == [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12,
    ]
    physical = {
        evidence_id
        for episode in graph.episodes
        for evidence_id in episode.asset_evidence_ids
    }
    assert len(physical) == 22, "两个根各自的物理文件都必须保留"


def test_movie_and_standalone_are_not_swallowed_by_numbering_candidates():
    from app.media_v4.resolution.resolver import MediaResolver

    names = [
        f"上伊那牡丹S01E{index}.mkv" for index in range(1, 4)
    ] + ["上伊那牡丹 剧场版 [MOVIE].mkv", "上伊那牡丹 外传 SPIN-OFF.mkv"]
    graph = MediaResolver().resolve(_parse_entries(names, folder="上伊那牡丹"))

    work_keys = sorted(work.work_key for work in graph.works)
    assert len(work_keys) >= 2, work_keys
    episodes = [episode for episode in graph.episodes if episode.local_episode_number is not None]
    assert len(episodes) == 3, "电影/外传不得被算成正片剧集"


def test_expected_tv_regular_without_number_reports_specific_issue():
    """标题与类型都确定、只有集号缺失时必须给出具体原因（F-005）。"""

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.resolution.resolver import MediaResolver

    evidence = _evidence(0, "Show.S01.mkv", "Show/Show.S01.mkv")
    facts = ParsedFacts(
        parsed_fact_id="facts-fixture",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        media_type="tv",
        group_type="season",
        work_title="Show",
        title_candidates=("Show",),
        season_candidate=1,
        episode_candidate=None,
    )
    graph = MediaResolver().resolve([(evidence, facts)])

    codes = {issue.code for issue in graph.issues}
    assert "episode_number_unresolved" in codes, codes
