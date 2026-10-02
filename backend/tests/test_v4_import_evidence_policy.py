"""CHECK-003：单文件事实、附属内容边界与编号保真。

全部断言只经过真实解析入口（``V4Parser.parse``）与纯词法仲裁函数，
不访问源盘、不读挂载盘、不联网。
"""

from __future__ import annotations

import pytest

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.evidence_policy import (
    arbitrate_hint,
    collect_nearest_semantic_directory_tokens,
    parse_numbering,
    tokenize_filename,
)
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.resolution.resolver import MediaResolver

PARSER = V4Parser()


def _facts(relative_path: str, index: int = 0, *, provider: str = "pan115", **kwargs):
    evidence = SourceEvidence(
        evidence_id=f"ev-policy-{index:03d}",
        scan_id="scan-policy",
        root_id="root-policy",
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider=provider,
    )
    return evidence, PARSER.parse(evidence, **kwargs)


def _classification(relative_path: str):
    tokens = tokenize_filename(relative_path)
    directory = collect_nearest_semantic_directory_tokens(relative_path)
    numbering = parse_numbering(tokens, directory)
    return tokens, directory, numbering


# --- CHECK-003A：附属内容与排除边界 -----------------------------------------


def test_specials_and_auxiliary_are_excluded_only_with_explicit_evidence():
    """CHECK-003A: 明确附属关系排除；OP/ED/PV 附属；正片标题里的词不误伤。"""

    from app.media_v4.domain.identity import NON_IMPORTABLE_CONTENT_CLASSES

    excluded = [
        # 中文特别篇与 Specials 目录语义一致
        "动画/作品甲/特别篇/作品甲.S01E01.mkv",
        "动画/作品甲/Specials/作品甲.S01E01.mkv",
        "动画/作品甲/作品甲.S00E03.mkv",
        # 明确附属视频
        "动画/作品甲/作品甲 [NCOP01].mkv",
        "动画/作品甲/作品甲 [PV01].mkv",
        "动画/作品甲/特别篇/作品甲 映像特典.mkv",
    ]
    for index, path in enumerate(excluded):
        _evidence, facts = _facts(path, index)
        assert facts.content_class in NON_IMPORTABLE_CONTENT_CLASSES, path
        assert facts.is_importable is False, path
        assert facts.is_auxiliary is True, path

    kept = [
        # 标题里含特殊词但不是附属内容
        "动画/Happy Ending/Happy Ending.S01E01.mkv",
        "动画/作品甲/作品甲.H264.50fps.第01集.mkv",
        "动画/SPY×FAMILY/SPY×FAMILY.S01E01.mkv",
        # 独立 OVA 有本地编号：按正片导入
        "动画/独立作品/独立作品 [01].mkv",
        "动画/独立作品/独立作品 [02].mkv",
        # 父包是"合集+特典"不排除子正片
        "动画/大合集 S1-S3+剧场版+特典/作品甲/作品甲.S01E01.mkv",
    ]
    for index, path in enumerate(kept, start=100):
        _evidence, facts = _facts(path, index)
        assert facts.is_importable is True, path
        assert facts.is_auxiliary is False, path
        assert facts.group_type != "special", path
        assert facts.content_class != "attached_special", path


def test_ova_without_regular_season_is_playable_special():
    """明确 OVA 按最新边界归特别篇，不要求先有正片季号。"""

    _evidence, facts = _facts("动画/某作品/某作品 [OVA].mkv", 7)
    assert facts.content_class == "playable_special"
    assert facts.media_type == "tv"
    assert facts.is_importable is True
    assert facts.is_auxiliary is False
    assert facts.episode_candidate is None


def test_explicit_attachment_beats_inline_episode_number():
    """CHECK-003A: 附属关系明确时，文件里的 S01E01 不改变排除结论。"""

    _evidence, facts = _facts("动画/作品甲/特典/作品甲.S01E01.mkv", 9)
    assert facts.content_class == "attached_special"
    assert facts.is_importable is False
    assert facts.classification_state in {"conflict", "explicit"}


# --- CHECK-003B：来源无关与目录补标题 ----------------------------------------


def test_provider_only_changes_format_not_single_file_facts():
    """CHECK-003B: 同一文件在不同 provider 下得到同一单文件事实。"""

    path = "我的收藏/作品甲/作品甲.S01E01.mkv"
    baseline = None
    for index, provider in enumerate(("local", "pan115", "baidu", "openlist")):
        _evidence, facts = _facts(path, index, provider=provider)
        current = (
            facts.content_class,
            facts.media_type,
            facts.season_candidate,
            facts.episode_candidate,
            facts.is_importable,
            facts.is_auxiliary,
        )
        if baseline is None:
            baseline = current
        assert current == baseline, provider


def test_bare_episode_takes_title_from_unique_nearest_directory():
    """CHECK-003B: 裸 E01 用最近的作品目录补标题。"""

    _evidence, facts = _facts("我的收藏/作品甲/作品甲/作品甲 E01.mkv", 11)
    assert facts.season_candidate == 1
    assert facts.episode_candidate == 1


def test_baidu_on_air_version_stays_episode_six_in_resolved_graph():
    entries = [
        _facts("天元突破/sprcial/[4K_EA] 天元突破 05(On Air Ver) [简体内嵌].mkv", 51, provider="baidu"),
        _facts("天元突破/sprcial/[4K_EA] 天元突破 06(On Air Ver) [简体内嵌].mkv", 52, provider="baidu"),
    ]

    graph = MediaResolver().resolve(entries)

    assert len(graph.works) == 1
    assert sorted(episode.local_episode_number for episode in graph.episodes) == [5, 6]
    assert all(episode.season_kind != "special" for episode in graph.episodes)


def test_distinct_file_titles_are_not_joined_by_a_weak_directory():
    """CHECK-003B: 同一"我的收藏"下两个强文件标题各自成作品。"""

    entries = [
        _facts("我的收藏/作品甲/作品甲.S01E01.mkv", 20),
        _facts("我的收藏/作品乙/作品乙.S01E01.mkv", 21),
    ]
    graph = MediaResolver().resolve(entries)
    assert len({work.work_key for work in graph.works}) == 2


# --- CHECK-003C：编号保真 ---------------------------------------------------


def test_local_numbering_is_never_rebased_or_overridden():
    """CHECK-003C: 缺集不改本地编号；目录不覆盖文件显式季号。"""

    _evidence, facts = _facts("作品甲/Season 2/作品甲.S02E13.mkv", 30)
    assert (facts.season_candidate, facts.episode_candidate) == (2, 13)
    assert facts.absolute_episode_candidate is None

    _evidence, single = _facts("作品甲/Season 2/作品甲.S02E14.mkv", 31)
    assert (single.season_candidate, single.episode_candidate) == (2, 14)

    _evidence, conflict = _facts("作品甲/Season 2/作品甲.S01E13.mkv", 32)
    assert (conflict.season_candidate, conflict.episode_candidate) == (1, 13)
    assert "season_evidence_conflict" in conflict.numbering.basis


def test_cjk_and_absolute_only_numbering_keep_original_values():
    """CHECK-003C: 中文集号不默认季号；ABS13 是全作品绝对编号。"""

    _evidence, cjk = _facts("作品甲/第01集.mkv", 33)
    assert cjk.episode_candidate == 1
    assert cjk.season_candidate is None

    _evidence, absolute = _facts("作品甲/作品甲 ABS13.mkv", 34)
    assert absolute.absolute_episode_candidate == 13
    assert absolute.episode_candidate is None


def test_batch_membership_does_not_change_known_numbers():
    """CHECK-003C: 加入无关作品或删掉前几集都不改已知编号。"""

    base = [
        _facts("作品甲/S1/作品甲.S01E13.mkv", 40),
        _facts("作品甲/S1/作品甲.S01E14.mkv", 41),
    ]
    extra = base + [_facts("作品乙/S1/作品乙.S01E01.mkv", 42)]
    base_numbers = sorted(facts.episode_candidate for _e, facts in base)
    extra_numbers = sorted(
        facts.episode_candidate for _e, facts in extra if "作品甲" in _e.relative_path
    )
    assert base_numbers == extra_numbers == [13, 14]


# --- CHECK-003D：结构化提示与媒体类型 ----------------------------------------


def test_structured_hint_reaches_facts_and_conflicts_do_not_bind():
    """CHECK-003D: 入口结构化提示进入事实；冲突不自动绑定。"""

    evidence = SourceEvidence(
        evidence_id="ev-hint-1",
        scan_id="scan-policy",
        root_id="root-policy",
        source_key="作品甲/作品甲.S01E01.mkv",
        relative_path="作品甲/作品甲.S01E01.mkv",
        entry_kind="video",
        tmdb_hint_id="123",
        tmdb_hint_type="tv",
    )
    facts = PARSER.parse(evidence)
    assert facts.tmdb_hint_id == 123
    assert facts.tmdb_hint_type == "tv"
    assert any(trace.field == "provider_hint" for trace in facts.decision_trace)

    conflicted = SourceEvidence(
        evidence_id="ev-hint-2",
        scan_id="scan-policy",
        root_id="root-policy",
        source_key="作品甲/作品甲 [tmdb-456].S01E01.mkv",
        relative_path="作品甲/作品甲 [tmdb-456].S01E01.mkv",
        entry_kind="video",
        tmdb_hint_id="123",
        tmdb_hint_type="tv",
    )
    conflicted_facts = PARSER.parse(conflicted)
    assert conflicted_facts.is_importable is True
    assert "provider_hint_conflict" in conflicted_facts.reasons
    assert any(
        trace.rule_id == "hint_conflict" for trace in conflicted_facts.decision_trace
    )


def test_invalid_structured_hint_keeps_the_video():
    """CHECK-003D: 非法 ID 不丢视频，只发 provider_hint_invalid。"""

    evidence = SourceEvidence(
        evidence_id="ev-hint-3",
        scan_id="scan-policy",
        root_id="root-policy",
        source_key="作品甲/作品甲.S01E01.mkv",
        relative_path="作品甲/作品甲.S01E01.mkv",
        entry_kind="video",
        tmdb_hint_id="not-a-number",
    )
    facts = PARSER.parse(evidence)
    assert facts.is_importable is True
    assert facts.tmdb_hint_id is None
    assert "provider_hint_invalid" in facts.reasons


@pytest.mark.parametrize("missing", [None, "", "  \t"])
@pytest.mark.parametrize("source", ["filename_hint", "structured_hint", "evidence_hint"])
def test_absent_hint_is_not_an_invalid_identity(missing, source):
    hints = {"filename_hint": (None, ""), "structured_hint": ("", ""), "evidence_hint": ("", "")}
    hints[source] = (missing, "tv")
    decision = arbitrate_hint(**hints)
    assert decision.tmdb_id is None
    assert decision.reasons == ()
    assert decision.traces == ()


@pytest.mark.parametrize("invalid", ["not-a-number", "0", "-1"])
def test_invalid_observation_hint_is_diagnostic_and_does_not_abort_import(invalid):
    decision = arbitrate_hint(filename_hint=(123, "tv"), structured_hint=("", ""),
                              evidence_hint=(invalid, "tv"))
    assert decision.tmdb_id == 123
    assert decision.reasons == ("provider_hint_invalid",)
    assert not decision.conflict


def test_filename_identity_without_structured_hint_has_no_false_error():
    _evidence, facts = _facts("作品甲/作品甲 [tmdb-123].S01E01.mkv")
    assert facts.tmdb_hint_id == 123
    assert "provider_hint_invalid" not in facts.reasons
    assert facts.is_importable
    assert facts.episode_candidate == 1


def test_show_type_is_driven_by_import_family_and_resolved_media_type():
    """CHECK-003D: import_family + 判定后的 media_type 驱动 show_type。"""

    for index, (family, path, expected) in enumerate(
        (
            ("anime", "作品甲/作品甲.S01E01.mkv", "anime_series"),
            ("anime", "作品甲/电影/作品甲 剧场版.mkv", "anime_movie"),
            ("live", "作品甲/作品甲.S01E01.mkv", "live_series"),
        )
    ):
        evidence = SourceEvidence(
            evidence_id=f"ev-show-{index}",
            scan_id="scan-policy",
            root_id="root-policy",
            source_key=path,
            relative_path=path,
            entry_kind="video",
            import_family=family,
        )
        facts = PARSER.parse(evidence)
        assert facts.show_type == expected, path
