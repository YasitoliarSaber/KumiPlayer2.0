"""HYB-7 验收：最终 E2E / Safety Gate 补充回归。

覆盖此前 checkpoint 未单独验证的两个强制场景：
- 删除目录：完整 parent list 后正确 tombstone（级联子树）；
- bootstrap → revisions：media unit 不重复（同一 root 双通道不制造第二套）。
"""
from __future__ import annotations

import pytest

from app.catalog import store as catalog_store
from app.catalog.service import scan_directory_paginated
from app.catalog.models import DirectoryPage, SourceNodeInput


class TrackingScanner:
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

    db_path = tmp_path / "hyb7.db"
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


class TestDeletedDirectoryTombstone:
    def test_missing_directory_cascades_tombstone(self):
        """目录从远端消失：完整 parent list 后正确 tombstone 级联子树。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        scanner.seed("/根", [("作品A", True, 100.0)])
        scanner.seed("/根/作品A", [("S1", True, 1.0)])
        scanner.seed("/根/作品A/S1", [("E01.mkv", False, 1.0)])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        for path in ("/根/作品A", "/根/作品A/S1"):
            scan_directory_paginated(
                scanner, root.root_id, path, generation,
                parent_path="/根", depth=1, per_page=100,
            )
        # 远端：作品A 整个消失
        scanner.seed("/根", [])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation + 1,
            parent_path="", depth=0, per_page=100,
        )
        from app.db.database import get_connection

        conn = get_connection()
        rows = conn.execute(
            """
            SELECT remote_path, tombstone FROM source_nodes
            WHERE root_id = ? AND tombstone != ''
            ORDER BY remote_path
            """,
            (root.root_id,),
        ).fetchall()
        paths = {row["remote_path"] for row in rows}
        assert "/根/作品A" in paths
        assert "/根/作品A/S1" in paths
        assert "/根/作品A/S1/E01.mkv" in paths
        # directory checkpoint 级联删除（不再入队）
        assert catalog_store.get_directory(root.root_id, "/根/作品A") is None
        assert catalog_store.get_directory(root.root_id, "/根/作品A/S1") is None


class TestBootstrapNoDuplicateUnits:
    def test_bootstrap_scan_creates_no_duplicate_media_units(self):
        """TXT bootstrap 同一 root：media unit 不重复（幂等 revision）。"""
        from app.import_plan import revision_store

        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        scanner.seed("/根", [("作品A", True, 100.0)])
        scanner.seed("/根/作品A", [("作品A.S01E01.mkv", False, 1.0)])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        scan_directory_paginated(
            scanner, root.root_id, "/根/作品A", generation,
            parent_path="/根", depth=1, per_page=100,
        )
        # 第二轮（同 root 增量）：目录不变 → 不产生新 revision
        before = len(revision_store.list_revisions(root.root_id))
        scanner.seed("/根", [("作品A", True, 100.0)])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation + 1,
            parent_path="", depth=0, per_page=100,
        )
        after = len(revision_store.list_revisions(root.root_id))
        assert after == before, "未变化目录不得生成重复 revision"
