"""B5-2：OpenList 扫描目录 frontier 的断点续扫合同。

关键保证：
- 登记幂等，且**不会**把已完成目录重新排队（否则"续扫"会退化成重扫）；
- 被中断的 ``scanning`` 目录仍算待处理，恢复时从记录的 ``next_page`` 继续；
- 取用顺序浅层优先，保证根目录先被列出。
"""

from __future__ import annotations


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "frontier.db")
    database.initialize()
    return database


def test_ensure_is_idempotent_and_keeps_completed_rows(tmp_path):
    from app.media_v4.sources.scan_frontier import (
        directory_counts,
        ensure_directories,
        mark_directory,
    )

    database = _database(tmp_path)

    assert ensure_directories(database, scan_id="scan-1", remote_paths=["", "/a", "/b"], depth=0) == 3
    # 重复登记不再新增。
    assert ensure_directories(database, scan_id="scan-1", remote_paths=["", "/a"], depth=0) == 0

    mark_directory(database, scan_id="scan-1", remote_path="/a", status="completed")
    # 续扫时重新登记，不能让已完成的目录回到 queued。
    assert ensure_directories(database, scan_id="scan-1", remote_paths=["/a", "/b"], depth=0) == 0
    assert directory_counts(database, scan_id="scan-1") == {"total": 3, "completed": 1, "pending": 2}


def test_next_pending_prefers_shallow_then_path_order(tmp_path):
    from app.media_v4.sources.scan_frontier import ensure_directories, mark_directory, next_pending_directory

    database = _database(tmp_path)
    ensure_directories(database, scan_id="scan-1", remote_paths=["/deep/b"], depth=2)
    ensure_directories(database, scan_id="scan-1", remote_paths=[""], depth=0)
    ensure_directories(database, scan_id="scan-1", remote_paths=["/shallow/b", "/shallow/a"], depth=1)

    taken = []
    while True:
        item = next_pending_directory(database, scan_id="scan-1")
        if item is None:
            break
        taken.append(item["remote_path"])
        mark_directory(database, scan_id="scan-1", remote_path=item["remote_path"], status="completed")

    assert taken == ["", "/shallow/a", "/shallow/b", "/deep/b"]


def test_interrupted_scanning_directory_stays_pending_with_page_cursor(tmp_path):
    from app.media_v4.sources.scan_frontier import (
        directory_counts,
        ensure_directories,
        mark_directory,
        next_pending_directory,
    )

    database = _database(tmp_path)
    ensure_directories(database, scan_id="scan-1", remote_paths=[""], depth=0)

    first = next_pending_directory(database, scan_id="scan-1")
    assert first == {"remote_path": "", "depth": 0, "next_page": 1}

    # 模拟翻到第 3 页后进程中断：写回游标，状态仍是 scanning。
    mark_directory(database, scan_id="scan-1", remote_path="", status="scanning", next_page=3)

    resumed = next_pending_directory(database, scan_id="scan-1")
    assert resumed == {"remote_path": "", "depth": 0, "next_page": 3}
    assert directory_counts(database, scan_id="scan-1")["pending"] == 1


def test_directory_counts_and_clear_are_scoped_per_scan(tmp_path):
    from app.media_v4.sources.scan_frontier import (
        clear,
        directory_counts,
        ensure_directories,
        mark_directory,
    )

    database = _database(tmp_path)
    ensure_directories(database, scan_id="scan-1", remote_paths=["/a"], depth=0)
    ensure_directories(database, scan_id="scan-2", remote_paths=["/x"], depth=0)
    mark_directory(database, scan_id="scan-1", remote_path="/a", status="completed")

    assert directory_counts(database, scan_id="scan-1")["completed"] == 1
    assert directory_counts(database, scan_id="scan-2")["completed"] == 0

    clear(database, scan_id="scan-1")
    assert directory_counts(database, scan_id="scan-1") == {"total": 0, "completed": 0, "pending": 0}
    assert directory_counts(database, scan_id="scan-2")["total"] == 1
