"""真实目录树样本的 V4 识别合同。

这些断言只读取仓库内脱敏目录清单，不访问清单中的真实路径。它们用于防止
后续重构再次丢失旧版已经验证过的作品边界、季度归属和多文件资产语义。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.resolution.candidates import merge_graph, plan_work_candidates
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.sources.scanner import (
    build_directory_tree_evidence,
    read_directory_tree_text,
)
from app.media_v4.sources.tree_root import tree_scope_name

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE_FILENAMES = (
    "根目录20260703203700_目录树.txt",
    "新番_文件目录_20260712005344.txt",
    "01动画_文件目录.txt",
)


@pytest.mark.parametrize("filename", _SAMPLE_FILENAMES)
def test_real_directory_tree_sample_has_complete_non_ambiguous_v4_graph(
    filename: str,
):
    sample = _PROJECT_ROOT / "docs" / "samples" / filename
    text = read_directory_tree_text(sample)
    _scan, evidence = build_directory_tree_evidence(
        text,
        root_id=f"sample-{filename}",
        scan_id=f"sample-{filename}",
        provider="pan115" if filename == "根目录20260703203700_目录树.txt" else "baidu",
    )
    parser = V4Parser()
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container=tree_scope_name(sample))) for item in evidence]
    )
    facts = [facts for _evidence, facts in parsed]
    graph = MediaResolver().resolve(parsed)

    importable_ids = {
        item.evidence_id
        for item in facts
        if item.is_importable and not item.is_auxiliary
    }
    assigned_ids = {
        evidence_id
        for episode in graph.episodes
        for evidence_id in episode.asset_evidence_ids
    } | {
        evidence_id
        for work_asset in graph.work_assets
        for evidence_id in work_asset.asset_evidence_ids
    }

    assert assigned_ids == importable_ids
    # F-005（2026-09-28）：序列中无法定位的正片条目必须显式提示，从前的
    # “零 issue”断言正是掩盖它们的盲点。这两个码是信息类提示（warning），
    # 不阻断导入；结构错误与其余提示仍然必须为空。
    informational = {"episode_number_unresolved", "media_type_unresolved"}
    unexpected = [issue for issue in graph.issues if issue.code not in informational]
    assert unexpected == [], unexpected
    assert all(issue.severity == "warning" for issue in graph.issues), graph.issues
    assert graph.works
    # 只验证解析图会漏掉本次回归：错误绑定是在第二步离线候选阶段产生的。
    _candidates, merge_map, candidate_issues = plan_work_candidates(graph, parsed, lambda *_: [])
    assert candidate_issues == []
    merged = merge_graph(graph, merge_map)
    assert {
        evidence_id
        for episode in merged.episodes
        for evidence_id in episode.asset_evidence_ids
    } | {
        evidence_id
        for asset in merged.work_assets
        for evidence_id in asset.asset_evidence_ids
    } == importable_ids

    works_by_display_identity: dict[tuple[str, int | None, str, str], list[str]] = defaultdict(list)
    for work in graph.works:
        assert work.preferred_title.strip()
        works_by_display_identity[
            (work.preferred_title.casefold(), work.year, work.media_type, work.card_type)
        ].append(work.work_key)
    assert not {
        identity: work_keys
        for identity, work_keys in works_by_display_identity.items()
        if len(work_keys) > 1
    }


def _resolve_sample(filename: str):

    sample = _PROJECT_ROOT / "docs" / "samples" / filename
    text = read_directory_tree_text(sample)
    _scan, evidence = build_directory_tree_evidence(
        text,
        root_id=f"sample-{filename}",
        scan_id=f"sample-{filename}",
        provider="baidu",
    )
    parser = V4Parser()
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container=tree_scope_name(sample))) for item in evidence]
    )
    return MediaResolver().resolve(parsed)


def _parsed_sample(filename: str):
    """返回样本的 (parsed, graph)，供特别篇反向断言复用。"""

    sample = _PROJECT_ROOT / "docs" / "samples" / filename
    text = read_directory_tree_text(sample)
    _scan, evidence = build_directory_tree_evidence(
        text,
        root_id=f"sample-{filename}",
        scan_id=f"sample-{filename}",
        provider="baidu",
    )
    parser = V4Parser()
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container=tree_scope_name(sample))) for item in evidence]
    )
    return parsed, MediaResolver().resolve(parsed)


def _assigned_evidence_ids(graph) -> set[str]:
    """图中已经占用的全部 evidence：Episode Asset 与 Work 级 Asset。"""

    return {
        evidence_id for episode in graph.episodes for evidence_id in episode.asset_evidence_ids
    } | {
        evidence_id for asset in graph.work_assets for evidence_id in asset.asset_evidence_ids
    }


def _assert_specials_excluded(parsed, graph, *, marker: str = "") -> int:
    """未被最新四类标记准入的特别篇仍排除，不混入正片。"""

    matched = [
        (evidence, facts)
        for evidence, facts in parsed
        if facts.group_type == "special" and facts.content_class != "playable_special"
        and marker in evidence.relative_path
    ]
    assert matched, f"样本中应存在可识别的特别篇（marker={marker!r}）"
    assert all(facts.is_importable is False for _evidence, facts in matched)
    assert all(facts.is_auxiliary is True for _evidence, facts in matched)
    assert _assigned_evidence_ids(graph).isdisjoint(
        {evidence.evidence_id for evidence, _facts in matched}
    )
    return len(matched)


def _resolve_sample_with_verified_identities(filename: str):
    """按生产扫描的离线候选阶段合并已核验的跨语言 Provider 身份。"""

    sample = _PROJECT_ROOT / "docs" / "samples" / filename
    text = read_directory_tree_text(sample)
    _scan, evidence = build_directory_tree_evidence(
        text,
        root_id=f"sample-{filename}",
        scan_id=f"sample-{filename}",
        provider="baidu",
    )
    parser = V4Parser()
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container=tree_scope_name(sample))) for item in evidence]
    )
    graph = MediaResolver().resolve(parsed)
    _candidates, merge_map, candidate_issues = plan_work_candidates(
        graph,
        parsed,
        lambda *_args: [],
    )
    assert candidate_issues == []
    return merge_graph(graph, merge_map)


def _work_season_counts(graph, title: str) -> dict[int, int]:
    work = next(work for work in graph.works if work.preferred_title == title)
    counts: dict[int, int] = defaultdict(int)
    for episode in graph.episodes:
        if episode.work_key == work.work_key:
            counts[episode.local_season_number] += 1
    return dict(sorted(counts.items()))


def test_sample_corpus_keeps_main_series_spinoff_and_movie_identities_apart():
    """真实库样本中主系列、后续季、外传与电影必须保持独立身份。"""

    parsed, graph = _parsed_sample("01动画_文件目录.txt")
    titles = {work.preferred_title for work in graph.works}

    # 主系列与后续季归属同一 Work（石纪元 S1-S4 真实 24/11/22/24 集）。
    assert _work_season_counts(graph, "石纪元") == {1: 24, 2: 11, 3: 22, 4: 24}
    # 钢之炼金术师FA 单季 64 集，集标题里的数字不得展开幽灵剧集。
    assert _work_season_counts(graph, "钢之炼金术师FA") == {1: 64}
    assert _work_season_counts(graph, "天国大魔境") == {1: 13}
    # Re:零样本原本还包含 S00E01-E66 特别篇；用户 2026-09-24 规则后只剩正片三季。
    assert _work_season_counts(graph, "Re：从零开始的异世界生活") == {1: 25, 2: 25, 3: 16}
    # 吉卜力等电影是独立 Work，不进入任何剧集 Season。
    assert {"天空之城", "龙猫", "魔女宅急便"} <= titles
    for movie_title in ("天空之城", "龙猫", "魔女宅急便"):
        movie = next(work for work in graph.works if work.preferred_title == movie_title)
        assert movie.media_type == "movie"
        assert not [e for e in graph.episodes if e.work_key == movie.work_key]
    assert any(work.card_type == "standalone" and work.media_type == "movie" for work in graph.works)
    # 有明确 OVA 标记的两条和一条小数集号准入；其余 82 条仍排除。
    assert _assert_specials_excluded(parsed, graph) == 82
    selected = {e.evidence_id for e, f in parsed if f.content_class == "playable_special"}
    assert len(selected) == 3
    assert selected <= _assigned_evidence_ids(graph)


# 已删除 test_sample_corpus_special_titles_stay_distinguishable_from_each_other：
# 该用例只断言 CLANNAD / Angel Beats! 特别篇标题的区分度。用户 2026-09-24 规则
# 取消了 special 入库，特别篇不再产生 Episode，该契约已被取消。


def test_root_sample_excludes_production_extras_and_hyouka_ova_special():
    parsed, graph = _parsed_sample("根目录20260703203700_目录树.txt")
    hyouka = next(work for work in graph.works if work.preferred_title == "Hyouka")
    episodes = [episode for episode in graph.episodes if episode.work_key == hyouka.work_key]

    assert sum(episode.local_season_number == 1 for episode in episodes) == 22
    # 11.5 是明确小数集，制作素材仍排除。
    assert sum(episode.season_kind == "special" for episode in episodes) == 1
    selected = {e.evidence_id for e, f in parsed if "Hyouka [11.5]" in e.relative_path}
    assert len(selected) == 1
    assert selected <= _assigned_evidence_ids(graph)
    assert not any(
        marker in episode.display_title.casefold()
        for episode in episodes
        for marker in ("recording", "location hunting", "event")
    )


def test_root_sample_groups_konosuba_regular_seasons_but_keeps_movie_separate():
    graph = _resolve_sample("根目录20260703203700_目录树.txt")
    tv_works = [
        work
        for work in graph.works
        if work.media_type == "tv"
        and (
            "kono subarashii" in work.preferred_title.casefold()
            or work.preferred_title.casefold() == "konosuba"
        )
    ]
    movies = [
        work
        for work in graph.works
        if work.media_type == "movie" and "kono subarashii" in work.preferred_title.casefold()
    ]

    assert len(tv_works) == 1
    assert len(movies) == 1
    assert _work_season_counts(graph, tv_works[0].preferred_title)[1] == 10
    assert _work_season_counts(graph, tv_works[0].preferred_title)[2] == 10


# 已删除 test_root_sample_keeps_rezero_compound_specials_distinct_and_versions_merged：
# 该用例只断言 66 个 Re:Zero 特别篇的编号区分度与 1080p/2160p 版本合并。
# 用户 2026-09-24 规则取消了 special 入库，特别篇不再产生 Episode 与 Asset。


def test_root_sample_provider_identity_merge_has_no_phantom_200_episode_work():
    """生产候选合并后，中英文发布包不能重复成幽灵集或 200 集假季度。"""

    graph = _resolve_sample_with_verified_identities("根目录20260703203700_目录树.txt")
    episode_counts = {
        work.preferred_title: sum(
            episode.work_key == work.work_key and episode.season_kind == "regular"
            for episode in graph.episodes
        )
        for work in graph.works
    }

    # OVA / 正片外传不再被发行形式反向排除后，最大的作品是 69 集。
    assert max(episode_counts.values()) == 69
    assert {title: count for title, count in episode_counts.items() if count > 100} == {}
    # Re:Zero 只保留正片三季 66 集（25+25+16），不再叠加 66 个 S00 特典。
    assert episode_counts["Re：从零开始的异世界生活"] == 66


def test_root_sample_kaguya_keeps_fourth_season_assets_without_special_episodes():
    parsed, plain_graph = _parsed_sample("根目录20260703203700_目录树.txt")
    graph = _resolve_sample_with_verified_identities("根目录20260703203700_目录树.txt")
    works = [work for work in graph.works if work.preferred_title == "辉夜大小姐想让我告白"]

    assert len(works) == 1
    work = works[0]
    episodes = [episode for episode in graph.episodes if episode.work_key == work.work_key]
    # 正片四季的集数与重复版本合并语义保持原样。
    assert {
        season: sum(episode.local_season_number == season for episode in episodes)
        for season in (1, 2, 3, 4)
    } == {1: 12, 2: 12, 3: 13, 4: 2}
    assert sum(episode.season_kind == "special" for episode in episodes) == 1
    season_four = [episode for episode in episodes if episode.local_season_number == 4]
    assert len(season_four) == 2
    # C-004：仅文件明确的 S04E01/E02 属于本地第四季；Provider 映射不能重写本地季号。
    assert {episode.local_episode_number for episode in season_four} == {1, 2}
    assert all(len(episode.asset_evidence_ids) == 1 for episode in season_four)
    expected_four = {evidence.evidence_id for evidence, _facts in parsed
                     if "辉夜大小姐" in evidence.relative_path and ".S04E" in evidence.relative_path}
    assert {identity for episode in season_four for identity in episode.asset_evidence_ids} == expected_four
    # C-002：单发 `[OVA]` 是发行形式而不是附属关系声明，不再反向排除；
    # 它作为本地可播放内容入库，Provider 侧是否算特别篇由映射层决定。
    ova_entries = [
        (evidence, facts)
        for evidence, facts in parsed
        if "[OVA][Ma10p_2160p][x265_aac_ass]" in evidence.relative_path
    ]
    assert ova_entries, "样本中应存在该 OVA 条目"
    assert all(
        facts.is_importable is True and facts.is_auxiliary is False
        for _evidence, facts in ova_entries
    )
    # 小数特别篇准入，不改变第四季正片集号。
    selected = {e.evidence_id for e, f in parsed if "Hyouka [11.5]" in e.relative_path}
    assert selected <= _assigned_evidence_ids(plain_graph)


def test_root_sample_lycoris_short_movies_do_not_create_extra_work_cards():
    """官方六篇短动画不能另起作品卡片；按 2026-09-24 规则它们整体不入库。"""

    parsed, plain_graph = _parsed_sample("根目录20260703203700_目录树.txt")
    graph = _resolve_sample_with_verified_identities("根目录20260703203700_目录树.txt")
    works = [
        work
        for work in graph.works
        if "莉可丽丝" in work.preferred_title or "lycoris" in work.preferred_title.casefold()
    ]

    # 六篇短动画全部排除后不再产生任何工作，仍然只有主系列一张卡。
    assert len(works) == 1
    work = works[0]
    assert work.preferred_title == "莉可丽丝"
    episodes = [episode for episode in graph.episodes if episode.work_key == work.work_key]
    assert sum(episode.local_season_number == 1 for episode in episodes) == 13
    # 反向断言：Season 0 下的 S00E05-S00E10 六篇短动画不入库、不生成镜像。
    assert (
        _assert_specials_excluded(parsed, plain_graph, marker="Season 0/Lycoris.Recoil.S00E")
        == 6
    )


def test_movie_work_titles_remove_release_quality_and_keep_parent_series_context():
    animation_graph = _resolve_sample("01动画_文件目录.txt")
    animation_titles = {work.preferred_title for work in animation_graph.works}

    assert "吹响吧！上低音号 剧场版：想要传达的旋律" in animation_titles
    assert {
        "福音战士新剧场版：序",
        "福音战士新剧场版：破",
        "福音战士新剧场版：Q",
        "福音战士新剧场版：终",
    } <= animation_titles
    assert not any(
        marker in title.casefold()
        for title in animation_titles
        for marker in ("1080p", "2160p", "4320p", "blu-ray")
    )

    root_graph = _resolve_sample("根目录20260703203700_目录树.txt")
    root_titles = {work.preferred_title for work in root_graph.works}
    assert "中二病也要谈恋爱 剧场版：Take On Me" in root_titles
    assert "路人女主的养成方法 剧场版：Fine" in root_titles
    assert "刀剑神域 外传：Gun Gale Online" in root_titles
    assert "少女☆歌剧 Revue Starlight 剧场版" in root_titles
    assert not any(
        marker in title.casefold()
        for title in root_titles
        for marker in ("1080p", "2160p", "4320p", "blu-ray")
    )
