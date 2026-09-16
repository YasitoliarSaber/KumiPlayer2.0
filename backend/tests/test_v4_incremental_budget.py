"""O3：增量核对的请求预算必须真的封顶（清单哈希变化不得膨胀成近全量重列）。

回归背景：已核对过的目录，一旦其直接清单哈希变化，原实现会把**所有直接子目录**
标记为变化并入队；`queued` 只增不减、`processed` 不设上限，于是一次增删就能让
一轮"受控增量"变成成百上千次目录请求，`DEFAULT_VERIFICATION_BUDGET` 形同失效。

本文件同时锁定页终止判据：短页在 total 缺失时不能被当成末页（否则静默丢条目）。
"""

from __future__ import annotations

from types import SimpleNamespace


class _TreeClient:
    """最小 OpenList 客户端：按目录返回分页条目，并记录每次请求。"""

    def __init__(self, entries_by_dir: dict[str, list], *, page_size: int = 100, total_mode: str = "exact"):
        self.entries_by_dir = entries_by_dir
        self.page_size = page_size
        self.total_mode = total_mode
        self.calls: list[tuple[str, int]] = []

    def list_dir(self, path, *, page=1, per_page=100, refresh=False):
        self.calls.append((path, page))
        entries = list(self.entries_by_dir.get(path, []))
        size = max(1, int(per_page or self.page_size))
        start = (page - 1) * size
        chunk = entries[start:start + size]
        if self.total_mode == "exact":
            total = len(entries)
        elif self.total_mode == "missing":
            # 服务端没有给出总数：此时只有空页才是确定的末页。
            total = 0
        else:
            raise AssertionError("未知 total_mode")
        return SimpleNamespace(entries=chunk, total=total)


def _file(name: str, remote: str):
    return SimpleNamespace(name=name, is_dir=False, size=10, modified=1.0, remote_path=remote)


def _dir(name: str, remote: str):
    return SimpleNamespace(name=name, is_dir=True, size=0, modified=None, remote_path=remote)


def _baseline(children: list[str]):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return [
        to_source_evidence(SourceEntry(
            root_id="root-incr",
            scan_id="scan-tree",
            provider="pan115",
            ingest_method="directory_tree",
            relative_path=f"{child}/{child}.S01E01.mkv",
            source_key=f"/Anime/{child}/{child}.S01E01.mkv",
            source_locator=f"{child}/{child}.S01E01.mkv",
            playback_locator=f"{child}/{child}.S01E01.mkv",
        ))
        for child in children
    ]


def _tree(children: list[str]) -> dict[str, list]:
    pages = {"/Anime": [_dir(child, f"/Anime/{child}") for child in children]}
    for child in children:
        pages[f"/Anime/{child}"] = [_file(f"{child}.S01E01.mkv", f"/Anime/{child}/{child}.S01E01.mkv")]
    return pages


def _round(client, baseline, state, *, budget: int, now: float):
    from app.media_v4.sources.incremental import scan_openlist_incremental

    return scan_openlist_incremental(
        client,
        baseline=baseline,
        state=state,
        mapping_root="/",
        mount_root="",
        default_provider="pan115",
        verification_budget=budget,
        now=now,
    )


def test_listing_hash_change_cannot_blow_past_the_round_budget():
    from app.media_v4.sources.incremental import build_tree_baseline_state

    children = [f"Show{i:03d}" for i in range(6)]
    baseline = _baseline(children)
    state = build_tree_baseline_state("root-incr", "/Anime", baseline)

    # 第一轮：预算 1 → 根目录 + 1 个滚动抽查 = 2 次目录请求。
    first = _TreeClient(_tree(children))
    _scan_id, _evidence, first_state, first_stats = _round(first, baseline, state, budget=1, now=1000)
    assert first_stats["requested_directories"] == 2
    assert len(first.calls) == 2
    assert first_state["directories"][""]["listing_hash"]

    # 第二轮：根目录多了一个子目录（所有 mtime 不变，只能靠清单哈希发现）。
    changed_children = [*children, "Show999"]
    second = _TreeClient(_tree(changed_children))
    _scan_id, _evidence, _state, second_stats = _round(
        second, baseline, first_state, budget=1, now=2000
    )

    # 关键断言：哈希变化只能"给下一轮排优先级"，不能把 6 个未变化的子目录
    # 全拉进同一轮（那会让一轮受控增量膨胀成近全量重列）。请求数只能与
    # "滚动抽查数 + 真正新增数"相关，与未变化子目录的数量无关。
    requested = [path for path, _page in second.calls]
    rolling_pick = [path for path in requested if path.startswith("/Anime/Show") and path != "/Anime/Show999"]
    assert requested[0] == "/Anime"
    assert requested[-1] == "/Anime/Show999", f"新增目录必须被核对，实际 {requested}"
    assert len(rolling_pick) == 1, f"预算 1 只允许 1 个滚动抽查目录，实际 {requested}"
    assert len(requested) == 3, f"6 个未变化子目录不应被拉进本轮，实际 {requested}"
    assert second_stats["requested_directories"] == len(second.calls)
    # 新增目录算"变化目录"，被预算挡下的未变化目录不算。
    assert second_stats["changed_directories"] == 1


def test_hash_change_demotes_children_priority_for_the_next_round():
    """哈希变化的目录，其子目录要被"降级为最久未核对"，让下一轮滚动抽样优先挑中。"""

    from app.media_v4.sources.incremental import build_tree_baseline_state

    children = [f"Show{i:03d}" for i in range(6)]
    baseline = _baseline(children)
    state = build_tree_baseline_state("root-incr", "/Anime", baseline)

    first = _TreeClient(_tree(children))
    _scan_id, _evidence, first_state, _stats = _round(first, baseline, state, budget=1, now=1000)
    # 首轮核对过的子目录有核对时间；未核对的是 0。
    verified_child = next(
        path for path, entry in first_state["directories"].items()
        if path and float(entry.get("last_verified_at") or 0) > 0
    )
    assert float(first_state["directories"][verified_child]["last_verified_at"]) == 1000

    # 根目录清单变化（新增一个子目录），已核对过的子目录 mtime 不变。
    second = _TreeClient(_tree([*children, "Show999"]))
    _scan_id, _evidence, second_state, _stats = _round(second, baseline, first_state, budget=1, now=2000)

    assert float(second_state["directories"][verified_child]["last_verified_at"]) == 0, (
        "清单变化必须把子目录降级为下一轮优先核对，否则子树变化会被永久漏掉"
    )


def test_short_page_without_total_is_not_treated_as_last_page():
    """total 缺失时短页不能当末页：否则该目录后续条目被静默丢弃。"""

    from app.media_v4.sources.scanner import scan_openlist_directory

    entries = [_file(f"F{index:03d}.mkv", f"/Anime/F{index:03d}.mkv") for index in range(5)]
    # 服务端每页只给 2 条且不给 total：正确实现必须一直翻到空页。
    client = _TreeClient({"/Anime": entries}, total_mode="missing")

    def list_dir(path, *, page=1, per_page=100, refresh=False):
        client.calls.append((path, page))
        chunk = entries[(page - 1) * 2:page * 2]
        return SimpleNamespace(entries=chunk, total=0)

    client.list_dir = list_dir

    _scan_id, evidence = scan_openlist_directory(
        client,
        remote_root="/Anime",
        mapping_root="/Anime",
        mount_root="",
        root_id="root-incr",
        scan_id="scan-pages",
        default_provider="pan115",
    )

    assert [page for _path, page in client.calls] == [1, 2, 3, 4], "短页必须继续翻页直到空页"
    assert len(evidence) == 5, "所有分页条目都必须被收集"
