"""目录树与 OpenList 入口必须保留一致的系列关系语义。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest


def _resolve(evidence):
    from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
    from app.media_v4.resolution.resolver import MediaResolver

    parser = V4Parser()
    parsed = [(item, parser.parse(item)) for item in evidence]
    return MediaResolver().resolve(normalize_batch_parsed_facts(parsed))


def _parent_key(graph, child_title: str) -> str:
    child = next(work for work in graph.works if work.preferred_title == child_title)
    return next(
        relation.parent_work_key
        for relation in graph.relations
        if relation.child_work_key == child.work_key
    )


def _pan115_violet_evidence():
    """从真实 115 导出中截取主系列与外传电影，验证父项应为 TV。"""

    from app.media_v4.parsing.parser import V4Parser
    from app.media_v4.sources.scanner import (
        build_directory_tree_evidence,
        read_directory_tree_text,
    )

    sample = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "samples"
        / "根目录20260703203700_目录树.txt"
    )
    text = read_directory_tree_text(sample)
    _scan_id, evidence = build_directory_tree_evidence(
        text,
        root_id="root-pan115-sample",
        scan_id="scan-pan115-sample",
        provider="pan115",
    )

    parser = V4Parser()
    return [
        item
        for item in evidence
        if parser.parse(item).series_group == "紫罗兰永恒花园"
    ]


def test_pan115_tree_sample_links_standalone_movie_to_tv_series():
    evidence = _pan115_violet_evidence()
    graph = _resolve(evidence)

    assert _parent_key(graph, "紫罗兰永恒花园 外传：永远与自动手记人偶") == (
        "series:紫罗兰永恒花园:tv"
    )


class _FakeOpenListClient:
    """仅模拟所选根目录；不触发网络或元数据查询。"""

    def __init__(self, relative_paths: list[str]):
        self.entries: dict[str, list] = defaultdict(list)
        known_entries: set[tuple[str, str]] = set()
        self.entries["/动画"]
        for relative_path in relative_paths:
            parts = PurePosixPath(relative_path).parts
            for index in range(1, len(parts)):
                parent = "/动画" + (
                    "/" + "/".join(parts[: index - 1]) if index > 1 else ""
                )
                child_remote = "/动画/" + "/".join(parts[:index])
                entry_key = (parent, child_remote)
                if entry_key not in known_entries:
                    self.entries[parent].append(
                        self._entry(parts[index - 1], True, child_remote)
                    )
                    known_entries.add(entry_key)
                self.entries[child_remote]
            parent = "/动画" + (
                "/" + "/".join(parts[:-1]) if len(parts) > 1 else ""
            )
            remote_path = "/动画/" + relative_path
            self.entries[parent].append(self._entry(parts[-1], False, remote_path))

    @staticmethod
    def _entry(name: str, is_dir: bool, remote_path: str):
        return SimpleNamespace(
            name=name,
            is_dir=is_dir,
            remote_path=remote_path,
            size=1024,
            modified=0.0,
        )

    def list_dir(self, directory: str, *, page: int, per_page: int, refresh: bool):
        entries = self.entries.get(directory, [])
        return SimpleNamespace(entries=entries, total=len(entries))


@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_openlist_scans_keep_movie_parent_as_tv_series(mode: str):
    """OpenList 完整与增量扫描都走同一关系规则，且保留内容来源 pan115。"""

    from app.media_v4.sources.incremental import (
        build_tree_baseline_state,
        scan_openlist_incremental,
    )
    from app.media_v4.sources.scanner import scan_openlist_directory

    representative_evidence = _pan115_violet_evidence()
    client = _FakeOpenListClient(
        [item.relative_path for item in representative_evidence]
    )
    _scan_id, full_evidence = scan_openlist_directory(
        client,
        remote_root="/动画",
        mapping_root="/动画",
        mount_root="",
        root_id="root-openlist-relations",
        scan_id="scan-openlist-full",
        default_provider="pan115",
    )
    evidence = full_evidence
    if mode == "incremental":
        state = build_tree_baseline_state(
            "root-openlist-relations", "/动画", full_evidence
        )
        _scan_id, evidence, _next_state, _stats = scan_openlist_incremental(
            client,
            baseline=full_evidence,
            state=state,
            mapping_root="/动画",
            mount_root="",
            scan_id="scan-openlist-incremental",
            default_provider="pan115",
        )

    assert {item.provider for item in evidence} == {"pan115"}
    assert _parent_key(_resolve(evidence), "紫罗兰永恒花园 外传：永远与自动手记人偶") == (
        "series:紫罗兰永恒花园:tv"
    )
