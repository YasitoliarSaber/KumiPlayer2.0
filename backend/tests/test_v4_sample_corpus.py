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
