"""模块2 checkpoint1：目录分页跨页一致性校验。

覆盖（服务端提供可信 total 的场景）：
- 跨页重复 path → PageConsistencyError，不 commit（stage 丢弃/目录 failed）；
- total 漂移（page1=1050、page2=1051）→ PageConsistencyError，不 commit；
- 提前短页（collected < total，最后页 < per_page 不代表完成）→ 不 commit；
- collected == total → commit 成功。
任何一致性失败：旧事实保留、无 tombstone、stage 清空、目录标记 failed。
"""
from __future__ import annotations

import pytest

from app.catalog import service, store
from app.catalog.models import SourceNodeInput
from app.db.database import close_connection, get_connection, init_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog_pagination.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _setup_root():
    store.create_source(
        source_id="s1", source_type="openlist",
        provider_id="quark", ingest_method="openlist_api",
    )
    return store.create_source_root(
        source_id="s1", remote_locator="/动画", local_locator="K:\\动画",
    )


def _node(remote_path: str) -> SourceNodeInput:
    return SourceNodeInput(
        name=remote_path.rsplit("/", 1)[-1],
        remote_path=remote_path,
        parent_path=remote_path.rsplit("/", 1)[0] if "/" in remote_path else "",
        kind="file",
        size=1,
        mtime=1.0,
    )


def _stage_count() -> int:
    conn = get_connection()
    return int(
        conn.execute("SELECT COUNT(*) AS c FROM source_stage_entries").fetchone()["c"]
    )


class PaginationClient:
    """可控分页行为客户端。

    mode:
      - stable: total 恒定、无重复、页集合完整；
      - cross_page_duplicate: 第 2 页首项与第 1 页末项重复（total 不变）；
      - total_drift: 第 2 页起 total + 1；
      - short_page: 声明 total=total_items 但实际只返回前面部分条目（最后页不齐）。
    """

    def __init__(self, total_items: int, *, mode: str = "stable", per_page: int = 100):
        self.total_items = total_items
        self.mode = mode
        self.per_page = per_page
        self.calls: list[int] = []

    def _page_paths(self, page: int) -> list[str]:
        start = (page - 1) * self.per_page
        end = min(start + self.per_page, self.total_items)
        return [f"/动画/文件{i:04d}.mkv" for i in range(start, end)]

    def enumerate_directory(self, remote_path, page=1, per_page=100):
        self.calls.append(page)
        if self.mode == "cross_page_duplicate" and page == 2:
            # 第 2 页首项 = 第 1 页末项（跨页重复），total 保持不变
            paths = [f"/动画/文件{self.per_page - 1:04d}.mkv", *self._page_paths(2)]
            return type("Page", (), {
                "entries": [_node(p) for p in paths],
                "total": self.total_items,
            })()
        if self.mode == "total_drift":
            total = self.total_items + (1 if page >= 2 else 0)
            return type("Page", (), {
                "entries": [_node(p) for p in self._page_paths(page)],
                "total": total,
            })()
        if self.mode == "short_page":
            paths = self._page_paths(page)
            last_page = self.total_items // self.per_page + 1
            if page == last_page:
                # 最后页只给一部分（提前短页：最后页 < per_page 却未到 total）
                keep = max(0, self.total_items - 10 - (page - 1) * self.per_page)
                paths = paths[:keep]
            return type("Page", (), {
                "entries": [_node(p) for p in paths],
                "total": self.total_items,
            })()
        return type("Page", (), {
            "entries": [_node(p) for p in self._page_paths(page)],
            "total": self.total_items,
        })()


class TestPaginationConsistency:
    def _seed_old_facts(self, root, generation) -> dict:
        """先成功提交一轮旧事实，供失败保留旧事实断言使用。"""
        client = PaginationClient(1050, mode="stable")
        return service.scan_directory_paginated(
            client, root.root_id, "/动画", generation, per_page=100,
        )

    def test_stable_full_pages_commits(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        client = PaginationClient(1050, mode="stable")
        stats = service.scan_directory_paginated(
            client, root.root_id, "/动画", generation, per_page=100,
        )
        assert stats["added"] == 1050
        assert len(store.list_nodes(root.root_id)) == 1050
        assert client.calls[-1] == 11  # 11 页
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "complete"
        assert directory["entry_count"] == 1050
        assert _stage_count() == 0

    def test_cross_page_duplicate_no_commit(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        self._seed_old_facts(root, generation)
        client = PaginationClient(1050, mode="cross_page_duplicate")
        with pytest.raises(service.PageConsistencyError):
            service.scan_directory_paginated(
                client, root.root_id, "/动画", generation, per_page=100,
            )
        # 不 commit：旧事实保留、无 tombstone、stage 清空、目录 failed
        assert len(store.list_nodes(root.root_id)) == 1050
        assert len(store.list_nodes(root.root_id, include_tombstone=True)) == 1050
        assert _stage_count() == 0
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "failed"
        assert directory["last_error_kind"] == "pageconsistencyerror"

    def test_total_drift_no_commit(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        self._seed_old_facts(root, generation)
        client = PaginationClient(1050, mode="total_drift")
        with pytest.raises(service.PageConsistencyError):
            service.scan_directory_paginated(
                client, root.root_id, "/动画", generation, per_page=100,
            )
        assert len(store.list_nodes(root.root_id)) == 1050
        assert len(store.list_nodes(root.root_id, include_tombstone=True)) == 1050
        assert _stage_count() == 0
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "failed"
        assert directory["last_error_kind"] == "pageconsistencyerror"

    def test_short_page_no_commit(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        self._seed_old_facts(root, generation)
        client = PaginationClient(1050, mode="short_page")
        with pytest.raises(service.PageConsistencyError):
            service.scan_directory_paginated(
                client, root.root_id, "/动画", generation, per_page=100,
            )
        # 提前短页不 commit：无新增、无 tombstone、旧事实保留
        assert len(store.list_nodes(root.root_id)) == 1050
        assert len(store.list_nodes(root.root_id, include_tombstone=True)) == 1050
        assert _stage_count() == 0
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "failed"
        assert directory["last_error_kind"] == "pageconsistencyerror"

    def test_empty_directory_total_zero_commits(self):
        """空目录：total=0、空首页 → collected == total → commit 成功（无新增无墓碑）。"""
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        client = PaginationClient(0, mode="stable")
        stats = service.scan_directory_paginated(
            client, root.root_id, "/动画", generation, per_page=100,
        )
        assert stats["added"] == 0
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "complete"
        assert directory["entry_count"] == 0

    def test_unknown_total_keeps_page_termination(self):
        """服务端不提供 total（None）时保持按页终止行为：短页即完成。"""
        root = _setup_root()
        generation = store.bump_generation(root.root_id)

        class NoTotalClient:
            def __init__(self, items):
                self.items = items

            def enumerate_directory(self, remote_path, page=1, per_page=100):
                start = (page - 1) * per_page
                chunk = self.items[start:start + per_page]
                return type("Page", (), {
                    "entries": [_node(p) for p in chunk],
                    "total": None,
                })()

        client = NoTotalClient([f"/动画/文件{i:04d}.mkv" for i in range(250)])
        stats = service.scan_directory_paginated(
            client, root.root_id, "/动画", generation, per_page=100,
        )
        assert stats["added"] == 250
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "complete"
