"""任务 3 验收：OpenList Source Catalog 扫描。

覆盖：1050 项分页、20 层目录、限流 2 req/s、429 分类为 stale、
取消恢复、目录失败不误删。
"""

import time

import pytest

from app.catalog import service, store
from app.db.database import close_connection, init_db
from app.integrations.openlist.models import (
    OpenListEntry,
    OpenListError,
    OpenListRateLimitedError,
)
from app.integrations.openlist.scanner import OpenListDirectoryScanner


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "olcatalog.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _setup_root():
    store.create_source(source_id="ol", source_type="openlist", provider_id="quark", ingest_method="openlist_api")
    return store.create_source_root(source_id="ol", remote_locator="/动画", local_locator="K:\\动画")


class FakeClient:
    def __init__(self, tree, *, slow=False, fail_path="", fail_kind="network"):
        self.tree = tree
        self.slow = slow
        self.fail_path = fail_path
        self.fail_kind = fail_kind
        self.calls: list[tuple[str, int]] = []
        self.request_times: list[float] = []

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        self.calls.append((path, page))
        self.request_times.append(time.monotonic())
        if self.slow:
            time.sleep(0.05)
        if path == self.fail_path:
            if self.fail_kind == "rate_limit":
                raise OpenListRateLimitedError()
            raise OpenListError("OpenList 服务暂时不可用", status_code=503, kind=self.fail_kind)
        items = self.tree.get(path, [])
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=f"{path.rstrip('/')}/{name}",
            )
            for name, is_dir, size, modified in chunk
        ]
        return type("Page", (), {"entries": entries, "total": len(items)})()


def _make_tree(count: int) -> dict:
    return {
        "/动画": [(f"作品{i:04d}", True, None, None) for i in range(count)],
    }


class TestOpenListCatalogScan:
    def test_1050_items_paginated(self):
        root = _setup_root()
        client = FakeClient(_make_tree(1050))
        scanner = OpenListDirectoryScanner(client, rate_per_second=0)
        generation = store.bump_generation(root.root_id)
        stats = service.scan_directory_paginated(
            scanner, root.root_id, "/动画", generation, per_page=100,
        )
        assert stats["added"] == 1050
        assert len(store.list_nodes(root.root_id)) == 1050
        assert client.calls[-1][1] == 11  # 11 页

    def test_20_levels_directory(self):
        root = _setup_root()
        tree = {}
        current = "/动画"
        for depth in range(20):
            child = f"{current}/层{depth}"
            tree[current] = [(f"层{depth}", True, None, None)]
            current = child
        tree[current] = [("底.mkv", False, 1, 1.0)]
        client = FakeClient(tree)
        scanner = OpenListDirectoryScanner(client, rate_per_second=0)
        generation = store.bump_generation(root.root_id)
        # 逐层扫描（模拟 frontier：先入根，再按目录检查点推进）
        for depth in range(21):
            path = "/动画" if depth == 0 else f"/动画/{'/'.join(f'层{i}' for i in range(depth))}"
            service.scan_directory_paginated(
                scanner, root.root_id, path, generation,
                parent_path=path.rsplit("/", 1)[0] if depth else "",
                depth=depth, per_page=100,
            )
        nodes = store.list_nodes(root.root_id)
        kinds = {}
        for n in nodes:
            kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
        last_path = "/动画/" + "/".join(f"层{i}" for i in range(20))
        # 根本身不是条目：20 个层目录节点 + 最深层 1 个文件 = 21
        assert kinds.get("dir") == 20, (kinds, [n["remote_path"] for n in nodes])
        assert any(n["remote_path"] == last_path + "/底.mkv" for n in nodes), kinds
        assert len(nodes) == 21, [n["remote_path"] for n in nodes]

    def test_rate_limit_two_per_second(self):
        root = _setup_root()
        client = FakeClient(_make_tree(250))
        scanner = OpenListDirectoryScanner(client, rate_per_second=2.0)
        generation = store.bump_generation(root.root_id)
        service.scan_directory_paginated(
            scanner, root.root_id, "/动画", generation, per_page=100,
        )
        # 5 个请求：间隔 >= 0.5s（前 2 个可能接近但整体时间足够）
        intervals = [
            client.request_times[i + 1] - client.request_times[i]
            for i in range(len(client.request_times) - 1)
        ]
        assert min(intervals) >= 0.4  # 允许少量抖动

    def test_rate_limited_marks_stale_keeps_facts(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        good = FakeClient(_make_tree(3))
        service.scan_directory_paginated(
            OpenListDirectoryScanner(good, rate_per_second=0),
            root.root_id, "/动画", generation, per_page=100,
        )
        old_nodes = store.list_nodes(root.root_id)

        failing = FakeClient(_make_tree(3), fail_path="/动画", fail_kind="rate_limit")
        with pytest.raises(service.PageConsistencyError):
            service.scan_directory_paginated(
                OpenListDirectoryScanner(failing, rate_per_second=0),
                root.root_id, "/动画", generation, per_page=100,
            )
        assert store.list_nodes(root.root_id) == old_nodes
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "failed"
        assert directory["last_error_kind"] == "rate_limit"

    def test_cancel_requeues_and_resume(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        client = FakeClient(_make_tree(250), slow=True)
        scanner = OpenListDirectoryScanner(client, rate_per_second=0)
        cancel_state = {"n": 0}

        def should_cancel():
            cancel_state["n"] += 1
            return cancel_state["n"] > 3

        with pytest.raises(service.ScanCancelled):
            service.scan_directory_paginated(
                scanner, root.root_id, "/动画", generation, per_page=100,
                should_cancel=should_cancel,
            )
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "queued"
        # 恢复：再次扫描成功（新 generation，不传取消检查）
        new_gen = store.bump_generation(root.root_id)
        stats = service.scan_directory_paginated(
            scanner, root.root_id, "/动画", new_gen, per_page=100,
        )
        assert stats["added"] == 250

    def test_mtime_drift_updates_physical_fact(self):
        """mtime 变化是物理事实更新（catalog 层不判断替换）。"""
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        client = FakeClient({"/动画": [("a.mkv", False, 100, 1.0)]})
        service.scan_directory_paginated(
            OpenListDirectoryScanner(client, rate_per_second=0),
            root.root_id, "/动画", generation, per_page=100,
        )
        second = FakeClient({"/动画": [("a.mkv", False, 100, 2.0)]})
        stats = service.scan_directory_paginated(
            OpenListDirectoryScanner(second, rate_per_second=0),
            root.root_id, "/动画", generation, per_page=100,
        )
        assert stats["updated"] == 1
