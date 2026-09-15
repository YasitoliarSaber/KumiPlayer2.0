"""B5-2b：OpenList 全量扫描的目录级断点续扫。

保证：
- 已完成的目录在续扫时不会重新列出（否则"续扫"等于重扫）；
- 一页列完写回 ``next_page``，在某一页失败后从该页继续；
- 目录状态与游标落在 ``source_scan_directories``，不依赖进程内存。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeOpenListClient:
    """最小 OpenList 客户端：记录请求，支持在指定 (目录, 页) 上抛错。"""

    def __init__(self, tree, *, fail_on=None, page_size: int = 100):
        self.tree = tree
        self.fail_on = fail_on
        self.page_size = page_size
        self.calls: list[tuple[str, int]] = []

    def list_dir(self, path, *, page=1, per_page=100, refresh=False):
        self.calls.append((path, page))
        if self.fail_on is not None and (path, page) == self.fail_on:
            raise RuntimeError("OpenList 请求失败（500）")
        entries = list(self.tree.get(path, ()))
        size = per_page or self.page_size
        start = (page - 1) * size
        chunk = entries[start:start + size]
        return SimpleNamespace(entries=chunk, total=len(entries))


def _entry(remote_path: str, name: str, *, is_dir: bool):
    return SimpleNamespace(
        remote_path=remote_path,
        name=name,
        is_dir=is_dir,
        size=1024,
        modified=1720000000.0,
    )


_TREE = {
    "/动画": [
        _entry("/动画/ShowA", "ShowA", is_dir=True),
        _entry("/动画/ShowB", "ShowB", is_dir=True),
        _entry("/动画/README.txt", "README.txt", is_dir=False),
    ],
    "/动画/ShowA": [_entry("/动画/ShowA/A.S01E01.mkv", "A.S01E01.mkv", is_dir=False)],
    "/动画/ShowB": [_entry("/动画/ShowB/B.S01E01.mkv", "B.S01E01.mkv", is_dir=False)],
}


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "frontier-scan.db")
    database.initialize()
    return database


def _frontier_callbacks(database, scan_id: str):
    from app.media_v4.sources import scan_frontier

    return {
        "frontier_next": lambda: scan_frontier.next_pending_directory(database, scan_id=scan_id),
        "frontier_mark": lambda **kwargs: scan_frontier.mark_directory(
            database, scan_id=scan_id, **kwargs
        ),
        "frontier_add": lambda **kwargs: scan_frontier.ensure_directories(
            database, scan_id=scan_id, **kwargs
        ),
    }


def _scan(client, callbacks, batches: list | None = None):
    from app.media_v4.sources.scanner import scan_openlist_directory

    def _collect(items):
        if batches is not None:
            batches.extend(items)

    return scan_openlist_directory(
        client,
        remote_root="/动画",
        mapping_root="/动画",
        mount_root="K:\\",
        root_id="root-frontier",
        scan_id="scan-frontier",
        default_provider="quark",
        on_evidence_batch=_collect if batches is not None else None,
        **callbacks,
    )


def test_resume_skips_completed_directories_and_keeps_evidence(tmp_path):
    from app.media_v4.sources import scan_frontier

    database = _database(tmp_path)
    callbacks = _frontier_callbacks(database, "scan-frontier")
    delivered: list = []

    broken = _FakeOpenListClient(_TREE, fail_on=("/动画/ShowB", 1))
    with pytest.raises(RuntimeError):
        _scan(broken, callbacks, delivered)

    # 断点边界：已标记 completed 的目录，其证据必须已经交付（否则续扫会丢证据）。
    assert sorted(item.source_key for item in delivered) == ["/动画/ShowA/A.S01E01.mkv"]

    # 断点：根目录与 ShowA 已完成；ShowB 仍是待处理（保留在该页重新开始）。
    counts = scan_frontier.directory_counts(database, scan_id="scan-frontier")
    assert counts["completed"] == 2
    assert counts["pending"] == 1
    pending = scan_frontier.next_pending_directory(database, scan_id="scan-frontier")
    assert pending == {"remote_path": "/动画/ShowB", "depth": 1, "next_page": 1}

    healthy = _FakeOpenListClient(_TREE)
    _scan_id, evidence = _scan(healthy, callbacks, delivered)

    requested = [path for path, _page in healthy.calls]
    assert requested == ["/动画/ShowB"]
    # 续扫只重算未完成目录；两轮交付合起来才是完整证据集。
    assert sorted(item.source_key for item in evidence) == ["/动画/ShowB/B.S01E01.mkv"]
    assert sorted(item.source_key for item in delivered) == [
        "/动画/ShowA/A.S01E01.mkv",
        "/动画/ShowB/B.S01E01.mkv",
    ]


def test_resume_continues_from_recorded_page(tmp_path):
    from app.media_v4.sources import scan_frontier

    database = _database(tmp_path)
    callbacks = _frontier_callbacks(database, "scan-frontier")
    delivered: list = []

    many_files = [
        _entry(f"/动画/Big/F{index:03d}.mkv", f"F{index:03d}.mkv", is_dir=False)
        for index in range(150)
    ]
    tree = {"/动画": [_entry("/动画/Big", "Big", is_dir=True)], "/动画/Big": many_files}

    broken = _FakeOpenListClient(tree, fail_on=("/动画/Big", 2))
    with pytest.raises(RuntimeError):
        _scan(broken, callbacks, delivered)
    assert [page for _path, page in broken.calls] == [1, 1, 2]
    # 第 1 页在推进游标前已经交付。
    assert len(delivered) == 100

    pending = scan_frontier.next_pending_directory(database, scan_id="scan-frontier")
    assert pending == {"remote_path": "/动画/Big", "depth": 1, "next_page": 2}

    healthy = _FakeOpenListClient(tree)
    _scan_id, evidence = _scan(healthy, callbacks, delivered)

    # 第一页没有被重列，直接从第 2 页继续。
    assert healthy.calls == [("/动画/Big", 2)]
    assert len(evidence) == 50
    assert len(delivered) == 150


def test_resume_does_not_relist_when_root_was_completed(tmp_path):
    from app.media_v4.sources import scan_frontier

    database = _database(tmp_path)
    callbacks = _frontier_callbacks(database, "scan-frontier")

    first = _FakeOpenListClient(_TREE)
    _scan(first, callbacks)
    assert [path for path, _page in first.calls] == ["/动画", "/动画/ShowA", "/动画/ShowB"]
    # 完整走完后由调用方清理（handler 在 _assert_scan_identity 之后 clear）。
    scan_frontier.clear(database, scan_id="scan-frontier")

    second = _FakeOpenListClient(_TREE)
    _scan(second, callbacks)
    assert [path for path, _page in second.calls] == ["/动画", "/动画/ShowA", "/动画/ShowB"]
