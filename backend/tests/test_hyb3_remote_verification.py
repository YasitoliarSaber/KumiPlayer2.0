"""HYB-3 验收：Remote Verification Provenance。

必须证明：
- snapshot（TXT）扫描提交目录 → last_remote_verified_at 为空；
- OpenList list 成功提交 → last_remote_verified_at = now；
- remote_baseline_coverage 能真实计算（verified/total）；
- restart 后状态不丢（SQLite 持久化）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.catalog import store as catalog_store
from app.catalog.service import scan_directory_paginated
from app.catalog.scanner import SourceCatalogScanner
from app.catalog.models import DirectoryPage, SourceNodeInput


class FakeOpenListScanner:
    """模拟 OpenList 分页扫描器（client 契约）。"""

    source = "openlist"

    def __init__(self):
        self.calls = []

    def enumerate_directory(self, remote_path, page=1, per_page=100):
        self.calls.append((remote_path, page))
        if remote_path == "/根":
            entries = [
                SourceNodeInput(
                    remote_path="/根/作品A", name="作品A", kind="dir",
                    parent_path="/根", size=None, mtime=1.0,
                ),
                SourceNodeInput(
                    remote_path="/根/作品B", name="作品B", kind="dir",
                    parent_path="/根", size=None, mtime=2.0,
                ),
            ]
        else:
            entries = [
                SourceNodeInput(
                    remote_path=f"{remote_path}/视频.mkv", name="视频.mkv",
                    kind="file", parent_path=remote_path, size=50, mtime=1.0,
                )
            ]
        start = (page - 1) * per_page
        return DirectoryPage(entries=entries[start:start + per_page], total=len(entries))


class FakeTxtScanner:
    """模拟 TXT 快照扫描器（client 契约）。"""

    source = "pan115"

    def __init__(self):
        self.calls = []

    def enumerate_directory(self, remote_path, page=1, per_page=100):
        self.calls.append((remote_path, page))
        entries = [
            SourceNodeInput(
                remote_path="/根/作品A", name="作品A", kind="dir",
                parent_path="/根", size=None, mtime=1.0,
            )
        ]
        return DirectoryPage(entries=entries, total=len(entries))


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "hyb3.db"
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


class TestRemoteVerificationProvenance:
    def test_openlist_commit_sets_remote_verified_at(self):
        """OpenList list 成功提交 → last_remote_verified_at 非空。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = FakeOpenListScanner()
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        record = catalog_store.get_directory(root.root_id, "/根")
        assert record is not None
        assert record["state"] == "complete"
        assert record["last_remote_verified_at"] != ""

    def test_txt_snapshot_commit_leaves_remote_verified_empty(self):
        """TXT 快照提交 → last_remote_verified_at 为空（远端未验证）。"""
        root = _make_root(source_id="pan115-test")
        generation = catalog_store.bump_generation(root.root_id)
        scanner = FakeTxtScanner()
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        record = catalog_store.get_directory(root.root_id, "/根")
        assert record is not None
        assert record["last_remote_verified_at"] == ""

    def test_baseline_coverage_partial(self):
        """coverage 真实反映 verified/total 比例。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = FakeOpenListScanner()
        # 扫描根 + 作品A（2 个目录 → verified），作品B 目录不扫（0 个 → 未验证）
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        # 根提交后会把作品A/B 写入 queued；只扫描作品A
        scan_directory_paginated(
            scanner, root.root_id, "/根/作品A", generation,
            parent_path="/根", depth=1, per_page=100,
        )
        stats = catalog_store.remote_baseline_coverage(root.root_id)
        assert stats["total_directories"] == 3  # 根 + 作品A + 作品B
        assert stats["remote_verified_count"] == 2  # 根 + 作品A
        assert 0 < stats["coverage"] < 1

    def test_baseline_coverage_empty_root_is_full(self):
        root = _make_root()
        stats = catalog_store.remote_baseline_coverage(root.root_id)
        assert stats["total_directories"] == 0
        assert stats["coverage"] == 1.0

    def test_restart_persists_remote_verified(self, tmp_path, monkeypatch):
        """restart 后 last_remote_verified_at 不丢（SQLite 持久化）。"""
        from app.db.database import close_connection, init_db
        import app.db.database as db_mod

        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = FakeOpenListScanner()
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        verified = catalog_store.get_directory(root.root_id, "/根")["last_remote_verified_at"]
        assert verified != ""

        # 模拟重启：关闭连接重新 init（同一 db 文件）
        close_connection()
        if hasattr(db_mod._local, "connection"):
            db_mod._local.connection = None
        init_db()
        record = catalog_store.get_directory(root.root_id, "/根")
        assert record is not None
        assert record["last_remote_verified_at"] == verified
