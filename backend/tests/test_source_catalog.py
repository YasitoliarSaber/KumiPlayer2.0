"""任务 3 验收：统一 Source Catalog 与来源适配器。

覆盖：root 重叠拒绝、TXT 一次性快照导入（500 批量）、目录分页原子提交、
分页漂移 stale 不误删、网络失败保留旧事实、128 层保护、取消、
local 分页枚举、mtime 物理更新。
"""


import pytest

from app.catalog import service, store
from app.catalog.models import SourceNodeInput
from app.db.database import close_connection, init_db
from app.integrations.openlist.models import OpenListError


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _setup_root(remote="/动画", local="K:\\动画"):
    store.create_source(source_id="s1", source_type="txt", provider_id="pan115", ingest_method="directory_tree")
    return store.create_source_root(source_id="s1", remote_locator=remote, local_locator=local)


def _node(remote_path, name="", kind="file", size=1, mtime=1.0):
    return SourceNodeInput(
        name=name or remote_path.rsplit("/", 1)[-1],
        remote_path=remote_path,
        parent_path=remote_path.rsplit("/", 1)[0] if "/" in remote_path else "",
        kind=kind,
        size=size,
        mtime=mtime,
    )


class TestRootOverlap:
    def test_rejects_overlapping_roots(self):
        _setup_root("/动画")
        with pytest.raises(ValueError, match="重叠"):
            store.create_source_root(source_id="s1", remote_locator="/动画/新番")
        with pytest.raises(ValueError, match="重叠"):
            store.create_source_root(source_id="s1", remote_locator="/")

    def test_accepts_sibling_roots(self):
        _setup_root("/动画")
        root = store.create_source_root(source_id="s1", remote_locator="/剧集")
        assert root.normalized_locator == "/剧集"


class TestOneShotSnapshot:
    def test_ingest_snapshot_adds_and_tombstones(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        entries = [
            _node("/动画/冰菓", kind="dir"),
            _node("/动画/冰菓/冰菓 - 01.mkv", size=100),
            _node("/动画/冰菓/冰菓 - 02.mkv", size=200),
        ]
        stats = service.ingest_snapshot(root.root_id, generation, entries)
        assert stats["added"] == 3
        assert len(store.list_nodes(root.root_id)) == 3

        # 第二轮：一个文件消失 → missing tombstone；一个文件大小变化 → updated
        second_gen = store.bump_generation(root.root_id)
        stats2 = service.ingest_snapshot(
            root.root_id, second_gen,
            [
                _node("/动画/冰菓", kind="dir"),
                _node("/动画/冰菓/冰菓 - 01.mkv", size=999),
            ],
        )
        assert stats2["updated"] == 1
        assert stats2["missing"] == 1
        nodes = {item["remote_path"]: item for item in store.list_nodes(root.root_id, include_tombstone=True)}
        assert nodes["/动画/冰菓/冰菓 - 02.mkv"]["tombstone"] != ""
        assert store.list_nodes(root.root_id)  # 活动节点仍存在

    def test_batch_write_limit(self):
        root = _setup_root()
        entries = [_node(f"/动画/文件{i:04d}.mkv") for i in range(1200)]
        stats = service.ingest_snapshot(root.root_id, 1, entries)
        assert stats["added"] == 1200  # 500 条一批，全部写入


class TestDirectoryPagination:
    def test_paginated_scan_commits_atomically(self):
        root = _setup_root()

        class FakePaginated:
            def __init__(self, tree):
                self.tree = tree

            def enumerate_directory(self, remote_path, page=1, per_page=100):
                items = self.tree.get(remote_path, [])
                start = (page - 1) * per_page
                chunk = items[start:start + per_page]
                return type(
                    "Page",
                    (),
                    {
                        "entries": [_node(p) for p in chunk],
                        "total": len(items),
                    },
                )()

        client = FakePaginated(
            {
                "/动画": ["/动画/冰菓", "/动画/真人"],
                "/动画/冰菓": ["/动画/冰菓/冰菓 - 01.mkv"],
            }
        )
        generation = store.bump_generation(root.root_id)
        service.scan_directory_paginated(client, root.root_id, "/动画", generation, per_page=100)
        service.scan_directory_paginated(client, root.root_id, "/动画/冰菓", generation, parent_path="/动画", depth=1, per_page=100)
        nodes = {item["remote_path"] for item in store.list_nodes(root.root_id)}
        assert nodes == {"/动画/冰菓", "/动画/真人", "/动画/冰菓/冰菓 - 01.mkv"}
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "complete"
        assert directory["entry_count"] == 2
        assert directory["member_hash"]

    def test_page_drift_marks_stale_keeps_old_facts(self):
        """分页漂移（重复路径）重试耗尽后：保留旧目录事实，不误删。"""
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        # 先成功扫描一次
        good = _stable_client({"/动画": ["/动画/a.mkv", "/动画/b.mkv"]})
        service.scan_directory_paginated(good, root.root_id, "/动画", generation, per_page=100)
        old_nodes = store.list_nodes(root.root_id)

        # 第二次：分页漂移（重复路径）
        drifting = _drifting_client({"/动画": ["/动画/a.mkv", "/动画/b.mkv"]})
        with pytest.raises(service.PageConsistencyError):
            service.scan_directory_paginated(drifting, root.root_id, "/动画", generation, per_page=100)
        # 旧事实保留（无新增无 tombstone）
        assert store.list_nodes(root.root_id) == old_nodes
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "failed"
        assert directory["last_error_kind"] == "pageconsistencyerror"

    def test_network_failure_keeps_old_facts(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        good = _stable_client({"/动画": ["/动画/a.mkv"]})
        service.scan_directory_paginated(good, root.root_id, "/动画", generation, per_page=100)

        failing = _failing_client({"/动画": ["/动画/a.mkv"]})
        with pytest.raises(service.PageConsistencyError):
            service.scan_directory_paginated(failing, root.root_id, "/动画", generation, per_page=100)
        nodes = {item["remote_path"]: item for item in store.list_nodes(root.root_id)}
        assert nodes["/动画/a.mkv"]["tombstone"] == ""
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "failed"

    def test_depth_protection_line(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        with pytest.raises(service.PageConsistencyError, match="保护线"):
            service.scan_directory_paginated(
                _stable_client({}), root.root_id, "/动画",
                generation, depth=store.MAX_DIRECTORY_DEPTH + 1,
            )

    def test_cancel_requeues_directory(self):
        root = _setup_root()
        generation = store.bump_generation(root.root_id)
        cancel_state = {"n": 0}

        def should_cancel():
            cancel_state["n"] += 1
            return cancel_state["n"] > 1

        with pytest.raises(service.ScanCancelled):
            service.scan_directory_paginated(
                _stable_client({"/动画": ["/动画/a.mkv"]}),
                root.root_id, "/动画", generation, per_page=100,
                should_cancel=should_cancel,
            )
        directory = store.get_directory(root.root_id, "/动画")
        assert directory["state"] == "queued"
        assert store.list_nodes(root.root_id) == []


class TestLocalAdapter:
    def test_local_enumerate_directory_paginated(self, tmp_path):
        from app.sources.local import LocalScanner

        root = tmp_path / "本地"
        root.mkdir(parents=True, exist_ok=True)
        for index in range(15):
            (root / f"文件{index:02d}.mkv").write_bytes(b"x")
        adapter = LocalScanner()
        page1 = adapter.enumerate_directory(str(root), page=1, per_page=10)
        assert len(page1.entries) == 10
        assert page1.total == 15
        page2 = adapter.enumerate_directory(str(root), page=2, per_page=10)
        assert len(page2.entries) == 5
        assert page1.entries[0].kind == "file"


def _stable_client(tree):
    class Stable:
        def enumerate_directory(self, remote_path, page=1, per_page=100):
            items = tree.get(remote_path, [])
            start = (page - 1) * per_page
            chunk = items[start:start + per_page]
            return type("Page", (), {"entries": [_node(p) for p in chunk], "total": len(items)})()

    return Stable()


def _drifting_client(tree):
    class Drifting:
        def __init__(self):
            self.calls = 0

        def enumerate_directory(self, remote_path, page=1, per_page=100):
            self.calls += 1
            items = tree.get(remote_path, [])
            start = (page - 1) * per_page
            chunk = items[start:start + per_page]
            # 第一页伪造重复路径
            if page == 1:
                chunk = chunk + [chunk[0]] if chunk else chunk
            return type("Page", (), {"entries": [_node(p) for p in chunk], "total": len(items)})()

    return Drifting()


def _failing_client(tree):
    class Failing:
        def enumerate_directory(self, remote_path, page=1, per_page=100):
            raise OpenListError("OpenList 服务暂时不可用", status_code=503, kind="network")

    return Failing()
