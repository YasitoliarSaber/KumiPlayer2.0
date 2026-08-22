# -*- coding: utf-8 -*-
"""P0-3：LibraryIndex 跨 source 去重 回归测试

背景（问题 5/6）：同一作品被多个来源（pan115/baidu/openlist/local）扫描到，
`_deduplicate_library_works` 兜底键 `("directory", source, work_id)` 含 source，
同一 canonical 作品在不同 source 下不合并 → 前端出现重复卡片（如《刀剑神域外传：
暴风之铠》大量重复）。

修复：WorkIndex 增加 canonical_work_id；兜底目录卡键优先用 canonical_work_id
（不含 source）跨来源合并，legacy（无 canonical）保持 source+work_id 历史行为。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.library.models import EpisodeIndex, SeasonIndex, WorkIndex
from app.library.service import _deduplicate_library_works


def _series_work(canonical: str, source: str, title: str, *, seasons=None, episodes=None):
    return WorkIndex(
        work_id=canonical,
        canonical_work_id=canonical,
        title=title,
        source=source,
        media_type="tv",
        card_type="main_series",
        seasons=seasons or [],
        episodes=episodes or [],
    )


def _episode(episode_id: str, source: str, work_id: str, *, season=1, ep=1):
    return EpisodeIndex(
        episode_id=episode_id, work_id=work_id, source=source,
        season_number=season, episode_number=ep, group_type="season",
    )


def test_same_canonical_cross_source_merges_into_one_card():
    """同一 canonical 跨 pan115/openlist 来源合并为一张卡。"""
    pan = _series_work(
        "unit:abc123:main", "pan115", "刀剑神域外传：暴风之铠",
        episodes=[_episode("pan-ep1", "pan115", "unit:abc123:main", season=1, ep=1)],
    )
    ol = _series_work(
        "unit:abc123:main", "openlist", "刀剑神域外传：暴风之铠",
        episodes=[_episode("ol-ep2", "openlist", "unit:abc123:main", season=1, ep=2)],
    )
    normalized = _deduplicate_library_works([pan, ol])
    assert len(normalized) == 1, f"跨来源同一 canonical 必须合并为一张卡: {normalized}"
    merged = normalized[0]
    assert merged.canonical_work_id == "unit:abc123:main"
    assert "pan115" in merged.sources and "openlist" in merged.sources, merged.sources
    assert {e.episode_id for e in merged.episodes} == {"pan-ep1", "ol-ep2"}
    assert {e.source for e in merged.episodes} == {"pan115", "openlist"}


def test_same_canonical_keeps_most_complete_card_identity():
    """跨来源合并保留完整性更高的主卡身份（标题/海报等）。"""
    complete = _series_work(
        "unit:xyz:main", "pan115", "刀剑神域外传",
        seasons=[
            SeasonIndex(
                season_id="s1", work_id="unit:xyz:main", season_number=1,
                group_type="season", label="第1季", episode_count=2,
            )
        ],
        episodes=[
            _episode("a1", "pan115", "unit:xyz:main", season=1, ep=1),
            _episode("a2", "pan115", "unit:xyz:main", season=1, ep=2),
        ],
    )
    sparse = _series_work("unit:xyz:main", "baidu", "刀剑神域外传")
    normalized = _deduplicate_library_works([sparse, complete])
    assert len(normalized) == 1
    merged = normalized[0]
    assert merged.source == "pan115" or merged.source in merged.sources
    assert {e.episode_id for e in merged.episodes} == {"a1", "a2"}
    assert "pan115" in merged.sources and "baidu" in merged.sources


def test_legacy_no_canonical_keeps_source_split():
    """legacy（无 canonical_work_id）保持 source+work_id 分离的历史行为。"""
    a = WorkIndex(work_id="series-a", source="pan115")
    b = WorkIndex(work_id="series-a", source="baidu")
    normalized = _deduplicate_library_works([a, b])
    assert len(normalized) == 2, "legacy 无 canonical 不得跨来源合并"


def test_different_canonical_not_merged():
    """不同 canonical 的作品不得合并。"""
    a = _series_work("unit:aaa:main", "pan115", "作品A")
    b = _series_work("unit:bbb:main", "pan115", "作品B")
    normalized = _deduplicate_library_works([a, b])
    assert len(normalized) == 2
