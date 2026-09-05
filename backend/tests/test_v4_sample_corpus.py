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
        provider="baidu",
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
    assert graph.issues == ()
    assert graph.works

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

    graph = _resolve_sample("01动画_文件目录.txt")
    titles = {work.preferred_title for work in graph.works}

    # 主系列与后续季归属同一 Work（石纪元 S1-S4 真实 24/11/22/24 集 + S00 特典）。
    assert _work_season_counts(graph, "石纪元") == {0: 1, 1: 24, 2: 11, 3: 22, 4: 24}
    # 钢之炼金术师FA 单季 64 集，集标题里的数字不得展开幽灵剧集。
    assert _work_season_counts(graph, "钢之炼金术师FA") == {1: 64}
    assert _work_season_counts(graph, "天国大魔境") == {1: 13}
    # Re:零样本真实包含 S00E01-E66 特别篇，全部保持本地 SP 编号可区分。
    assert _work_season_counts(graph, "Re：从零开始的异世界生活") == {0: 66, 1: 25, 2: 25, 3: 16}
    # 吉卜力等电影是独立 Work，不进入任何剧集 Season。
    assert {"天空之城", "龙猫", "魔女宅急便"} <= titles
    for movie_title in ("天空之城", "龙猫", "魔女宅急便"):
        movie = next(work for work in graph.works if work.preferred_title == movie_title)
        assert movie.media_type == "movie"
        assert not [e for e in graph.episodes if e.work_key == movie.work_key]
    assert any(work.card_type == "standalone" and work.media_type == "movie" for work in graph.works)


def test_sample_corpus_special_titles_stay_distinguishable_from_each_other():
    """样本中带语义副标题的特别篇不得被清洗成同一个名字。"""

    graph = _resolve_sample("01动画_文件目录.txt")
    clannad = next(work for work in graph.works if work.preferred_title == "CLANNAD")
    clannad_specials = [
        episode.display_title
        for episode in graph.episodes
        if episode.work_key == clannad.work_key and episode.episode_kind == "special"
    ]
    assert len(clannad_specials) == 5
    assert len(set(clannad_specials)) == 5, f"特别篇标题失去区分度: {clannad_specials}"
    assert all(title != "特别篇" for title in clannad_specials)

    angel = next(work for work in graph.works if work.preferred_title == "Angel Beats!")
    angel_specials = [
        episode.display_title
        for episode in graph.episodes
        if episode.work_key == angel.work_key and episode.episode_kind == "special"
    ]
    assert "OVA1：通向天堂的阶梯（Stairway to Heaven）" in angel_specials
    assert "OVA2：地狱厨房（Hell's Kitchen）" in angel_specials


def test_root_sample_excludes_production_extras_and_keeps_real_hyouka_ova():
    graph = _resolve_sample("根目录20260703203700_目录树.txt")
    hyouka = next(work for work in graph.works if work.preferred_title == "Hyouka")
    episodes = [episode for episode in graph.episodes if episode.work_key == hyouka.work_key]

    assert sum(episode.local_season_number == 1 for episode in episodes) == 22
    assert sum(episode.local_season_number == 0 for episode in episodes) == 1
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


def test_root_sample_keeps_rezero_compound_specials_distinct_and_versions_merged():
    graph = _resolve_sample("根目录20260703203700_目录树.txt")
    work = next(
        work
        for work in graph.works
        if work.preferred_title == "Re：从零开始的异世界生活"
    )
    specials = [
        episode
        for episode in graph.episodes
        if episode.work_key == work.work_key and episode.episode_kind == "special"
    ]

    # 目录中 66 个不同 SP token 不能因复合编号或季度重启而互相覆盖；
    # 其中 5 个 token 同时存在 1080p/2160p，应成为同 Episode 的多个 Asset。
    assert len(specials) >= 66
    assert sum(len(episode.asset_evidence_ids) == 2 for episode in specials) >= 5


def test_root_sample_provider_identity_merge_has_no_phantom_200_episode_work():
    """生产候选合并后，中英文发布包不能重复成幽灵集或 200 集假季度。"""

    graph = _resolve_sample_with_verified_identities("根目录20260703203700_目录树.txt")
    episode_counts = {
        work.preferred_title: sum(
            episode.work_key == work.work_key for episode in graph.episodes
        )
        for work in graph.works
    }

    assert max(episode_counts.values()) == 136
    assert {
        title: count for title, count in episode_counts.items() if count > 100
    } == {"Re：从零开始的异世界生活": 136}


def test_root_sample_kaguya_specials_and_fourth_season_keep_distinct_playable_assets():
    graph = _resolve_sample_with_verified_identities("根目录20260703203700_目录树.txt")
    works = [work for work in graph.works if work.preferred_title == "辉夜大小姐想让我告白"]

    assert len(works) == 1
    work = works[0]
    episodes = [episode for episode in graph.episodes if episode.work_key == work.work_key]
    assert {
        season: sum(episode.local_season_number == season for episode in episodes)
        for season in {episode.local_season_number for episode in episodes}
    } == {0: 5, 1: 12, 2: 12, 3: 13, 4: 2}
    first_kiss = [
        episode
        for episode in episodes
        if "First Kiss" in episode.display_title
    ]
    assert len(first_kiss) == 4
    assert all(len(episode.asset_evidence_ids) == 1 for episode in first_kiss)
    season_four = [episode for episode in episodes if episode.local_season_number == 4]
    assert len(season_four) == 2
    assert all(len(episode.asset_evidence_ids) == 2 for episode in season_four)


def test_root_sample_lycoris_short_movies_merge_into_main_series_specials():
    """官方六篇短动画必须进入《莉可丽丝》同一作品的特别篇，不能另起卡片。"""

    graph = _resolve_sample_with_verified_identities("根目录20260703203700_目录树.txt")
    works = [
        work
        for work in graph.works
        if "莉可丽丝" in work.preferred_title or "lycoris" in work.preferred_title.casefold()
    ]

    assert len(works) == 1
    work = works[0]
    assert work.preferred_title == "莉可丽丝"
    episodes = [episode for episode in graph.episodes if episode.work_key == work.work_key]
    assert sum(episode.local_season_number == 1 for episode in episodes) == 13
    short_movies = [
        episode
        for episode in episodes
        if episode.local_season_number == 0 and 5 <= (episode.special_number or 0) <= 10
    ]
    assert len(short_movies) == 6
    assert all(len(episode.asset_evidence_ids) == 1 for episode in short_movies)


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
