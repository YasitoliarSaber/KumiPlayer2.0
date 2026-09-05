"""真实目录树样本的 V4 识别合同。

这些断言只读取仓库内脱敏目录清单，不访问清单中的真实路径。它们用于防止
后续重构再次丢失旧版已经验证过的作品边界、季度归属和多文件资产语义。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from app.media_v4.parsing.parser import V4Parser
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
    facts = [parser.parse(item, root_container=tree_scope_name(sample)) for item in evidence]
    graph = MediaResolver().resolve(list(zip(evidence, facts, strict=True)))

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
    facts = [parser.parse(item, root_container=tree_scope_name(sample)) for item in evidence]
    return MediaResolver().resolve(list(zip(evidence, facts, strict=True)))


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
