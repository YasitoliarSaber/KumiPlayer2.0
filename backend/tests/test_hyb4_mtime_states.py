"""HYB-4 验收：Baseline Learning + mtime 三态增量下钻。

必须证明：
- mtime unchanged（双方有值且相等）→ subtree 不请求；
- mtime changed（双方有值且不等）→ 只下钻变化 branch；
- mtime unknown（old=None，TXT bootstrap 遗留）→ 不误判 unchanged，
  也不立刻全树展开（记录 mtime、不 requeue 子树）；
- 新目录（无旧 node）→ 当轮发现并扫描；
- 不为检查 mtime 多发请求（复用父目录 list 的 modified）。
"""
from __future__ import annotations

import pytest

from app.catalog import store as catalog_store
from app.catalog.service import scan_directory_paginated
from app.catalog.models import DirectoryPage, SourceNodeInput


class TrackingScanner:
    """记录 enumerate 调用序列，用于断言下钻行为。"""

    source = "openlist"

    def __init__(self):
        self.calls: list[str] = []
        self.tree: dict[str, list[tuple[str, bool, float | None]]] = {}

    def seed(self, root: str, children: list[tuple[str, bool, float | None]]):
        self.tree[root] = children

    def enumerate_directory(self, remote_path, page=1, per_page=100):
        self.calls.append(remote_path)
        entries = [
            SourceNodeInput(
                remote_path=f"{remote_path}/{name}" if remote_path != "/" else f"/{name}",
                name=name, kind="dir" if is_dir else "file",
                parent_path=remote_path, size=None if is_dir else 10,
                mtime=mtime,
            )
            for name, is_dir, mtime in self.tree.get(remote_path, [])
        ]
        start = (page - 1) * per_page
        return DirectoryPage(entries=entries[start:start + per_page], total=len(entries))


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "hyb4.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _make_root(source_id: str = "openlist-test"):
    catalog_store.create_source(
        source_id=source_id, source_type="openlist",
        provider_id="openlist", ingest_method="openlist_api",
        connection_key=source_id, display_name="测试",
    )
    root = catalog_store.create_source_root(
        source_id=source_id, remote_locator="/根",
        local_locator=r"K:\根", import_family="anime",
    )
    return catalog_store.get_source_root(root.root_id)


class TestMtimeThreeState:
    def test_same_mtime_skips_subtree(self):
        """双方有值且相等 → 不下钻（无额外 list 请求）。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        scanner.seed("/根", [("作品A", True, 100.0)])
        scanner.seed("/根/作品A", [("视频.mkv", False, 5.0)])
        # 第一轮：新目录 → 全扫
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        scan_directory_paginated(
            scanner, root.root_id, "/根/作品A", generation,
            parent_path="/根", depth=1, per_page=100,
        )
        calls_after_full = list(scanner.calls)

        # 第二轮 incremental：mtime 相同 → 只扫根，不下钻作品A
        scanner.calls = []
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation + 1,
            parent_path="", depth=0, per_page=100,
        )
        assert scanner.calls == ["/根"]
        assert "/根/作品A" not in scanner.calls

    def test_changed_mtime_drills_only_changed_branch(self):
        """mtime changed → 只下钻变化 branch，未变化 branch 不请求。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        scanner.seed("/根", [("作品A", True, 100.0), ("作品B", True, 200.0)])
        scanner.seed("/根/作品A", [("视频A.mkv", False, 5.0)])
        scanner.seed("/根/作品B", [("视频B.mkv", False, 5.0)])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        for path in ("/根/作品A", "/根/作品B"):
            scan_directory_paginated(
                scanner, root.root_id, path, generation,
                parent_path="/根", depth=1, per_page=100,
            )
        # 变化：作品A mtime 100→300
        scanner.seed("/根", [("作品A", True, 300.0), ("作品B", True, 200.0)])
        scanner.calls = []
        # 提交根：作品A changed → queued；作品B same → 保持
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation + 1,
            parent_path="", depth=0, per_page=100,
        )
        pending = catalog_store.list_pending_directories(root.root_id, limit=10)
        pending_paths = {p["remote_path"] for p in pending}
        assert "/根/作品A" in pending_paths
        assert "/根/作品B" not in pending_paths

    def test_unknown_mtime_not_drilled_and_not_unchanged(self):
        """TXT bootstrap（mtime=None）→ OpenList 首扫：UNKNOWN 不下钻全树。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        # 第一轮模拟 TXT 快照：目录 mtime=None（旧 node 无远端基线）
        catalog_store.upsert_directory(root.root_id, "/根", parent_path="", depth=0)
        catalog_store.update_directory(root.root_id, "/根", state="complete")
        from app.catalog.models import SourceNodeInput
        from app.db.database import get_connection

        conn = get_connection()
        conn.execute(
            """
            INSERT INTO source_nodes (
                root_id, remote_path, parent_path, name, kind, size, mtime,
                logical_locator, first_seen_generation, last_seen_generation,
                tombstone, provider_id, route_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '')
            """,
            (root.root_id, "/根/作品A", "/根", "作品A", "dir", None, None,
             r"K:\根\作品A", generation, generation),
        )
        conn.execute(
            """
            INSERT INTO source_directories (
                root_id, remote_path, parent_path, depth, state
            ) VALUES (?, ?, ?, ?, 'complete')
            """,
            (root.root_id, "/根/作品A", "/根", 1),
        )
        conn.commit()
        # TXT 提交的目录 last_remote_verified_at 为空（HYB-3 语义）
        assert catalog_store.get_directory(root.root_id, "/根/作品A")["last_remote_verified_at"] == ""

        # OpenList 首扫根：child 出现真实 mtime（None → 300）→ UNKNOWN
        scanner.seed("/根", [("作品A", True, 300.0)])
        scanner.calls = []
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation + 1,
            parent_path="", depth=0, per_page=100,
        )
        # 不立即全树展开：作品A 不应被 requeue 下钻
        assert scanner.calls == ["/根"]
        pending = catalog_store.list_pending_directories(root.root_id, limit=10)
        assert not any(p["remote_path"] == "/根/作品A" for p in pending)
        # 但 node mtime 已记录（不误判 unchanged）
        node = conn.execute(
            "SELECT mtime FROM source_nodes WHERE root_id = ? AND remote_path = ?",
            (root.root_id, "/根/作品A"),
        ).fetchone()
        assert node is not None and node["mtime"] == 300.0
        # 且远端未验证标记保持空（baseline learning 待验证）
        assert catalog_store.get_directory(root.root_id, "/根/作品A")["last_remote_verified_at"] == ""

    def test_new_directory_scanned_this_round(self):
        """新目录（无旧 node）→ 当轮发现并扫描。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        scanner.seed("/根", [])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        # 新目录出现
        scanner.seed("/根", [("新番", True, 400.0)])
        scanner.seed("/根/新番", [("新番.S01E01.mkv", False, 1.0)])
        scanner.calls = []
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation + 1,
            parent_path="", depth=0, per_page=100,
        )
        # 新目录立即 queued（INSERT 回退），且发现器会下钻
        pending = catalog_store.list_pending_directories(root.root_id, limit=10)
        assert any(p["remote_path"] == "/根/新番" for p in pending)

    def test_no_extra_metadata_requests(self):
        """三态判定零额外请求：只复用父目录 list 返回的 modified。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        scanner.seed("/根", [("作品A", True, 100.0)])
        scanner.seed("/根/作品A", [("视频.mkv", False, 5.0)])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        scan_directory_paginated(
            scanner, root.root_id, "/根/作品A", generation,
            parent_path="/根", depth=1, per_page=100,
        )
        before = len(scanner.calls)
        # 重复提交根（incremental 一轮）：只允许 1 次 list
        scanner.calls = []
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation + 1,
            parent_path="", depth=0, per_page=100,
        )
        assert len(scanner.calls) == 1
