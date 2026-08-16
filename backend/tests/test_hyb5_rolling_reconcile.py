"""HYB-5 验收：Rolling Reconciliation、预算与首次对账保护。

必须证明：
- baseline learning：单轮不超过 rolling budget（50 个未验证目录入队）；
- rolling verification：next_verify_at 带 stable jitter 分散（非全部同时到期）；
- first reconcile preflight：错绑（快照与远端完全无重叠）→ abort，
  0 tombstone、不生成 revisions；
- 正常差异（TXT 旧快照允许增减）→ 放行；
- full reconcile 仅显式触发（无自动定时）。
"""
from __future__ import annotations

import hashlib

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

    db_path = tmp_path / "hyb5.db"
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


def _seed_snapshot_only_dirs(root_id: str, paths: list[str], generation: int):
    """模拟 TXT bootstrap：写入 n 个 complete 目录，last_remote_verified_at=''。"""
    from app.db.database import get_connection

    conn = get_connection()
    for i, path in enumerate(paths):
        # 唯一且递增的 last_verified_at（秒数拉开，避免字符串排序歧义）
        seconds = 1000 + i
        verified = f"2026-08-16T00:{seconds // 60:02d}:{seconds % 60:02d}"
        conn.execute(
            """
            INSERT INTO source_directories (
                root_id, remote_path, parent_path, depth, state,
                entry_count, last_verified_at, next_verify_at
            ) VALUES (?, ?, ?, ?, 'complete', 0, ?, '')
            """,
            (root_id, path, "/根", 1, verified),
        )
        conn.execute(
            """
            INSERT INTO source_nodes (
                root_id, remote_path, parent_path, name, kind, size, mtime,
                logical_locator, first_seen_generation, last_seen_generation,
                tombstone, provider_id, route_id
            ) VALUES (?, ?, ?, ?, 'dir', NULL, NULL, ?, ?, ?, '', '', '')
            """,
            (root_id, path, "/根", path.rsplit("/", 1)[-1],
             r"K:\根" + path, generation, generation),
        )
    conn.commit()

class TestBaselineBudget:
    def test_baseline_learning_respects_budget(self):
        """snapshot-only 目录每轮只入队 budget 个（默认 50）。"""
        root = _make_root()
        catalog_store.bump_generation(root.root_id)
        # 120 个 snapshot-only 目录（> budget 50）
        paths = [f"/根/作品{i:03d}" for i in range(120)]
        _seed_snapshot_only_dirs(root.root_id, paths, 1)

        catalog_store.prepare_scan(root.root_id, generation=2, mode="incremental")
        pending = catalog_store.list_pending_directories(root.root_id, limit=1000)
        queued_paths = [p["remote_path"] for p in pending if p["state"] == "queued"]
        # 预算 50：只有最旧的一批（last_verified_at 升序前 50）入队
        assert len(queued_paths) == catalog_store.BASELINE_VERIFY_BUDGET
        assert queued_paths == paths[:50]

    def test_full_mode_queues_everything(self):
        """full 模式不受预算限制（显式全量）。"""
        root = _make_root()
        catalog_store.bump_generation(root.root_id)
        paths = [f"/根/作品{i:03d}" for i in range(80)]
        _seed_snapshot_only_dirs(root.root_id, paths, 1)
        catalog_store.prepare_scan(root.root_id, generation=2, mode="full")
        pending = catalog_store.list_pending_directories(root.root_id, limit=1000)
        queued_paths = [p["remote_path"] for p in pending if p["state"] == "queued"]
        assert len(queued_paths) == 80


class TestRollingJitter:
    def test_next_verify_at_has_stable_jitter(self):
        """next_verify_at = now + 24h + stable jitter（分散到期）。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        scanner = TrackingScanner()
        scanner.seed("/根", [("作品A", True, 100.0), ("作品B", True, 100.0)])
        scanner.seed("/根/作品A", [("视频A.mkv", False, 1.0)])
        scanner.seed("/根/作品B", [("视频B.mkv", False, 1.0)])
        scan_directory_paginated(
            scanner, root.root_id, "/根", generation,
            parent_path="", depth=0, per_page=100,
        )
        # 提交子目录后才有各自 next_verify_at（commit_directory 写 checkpoint）
        scan_directory_paginated(
            scanner, root.root_id, "/根/作品A", generation,
            parent_path="/根", depth=1, per_page=100,
        )
        scan_directory_paginated(
            scanner, root.root_id, "/根/作品B", generation,
            parent_path="/根", depth=1, per_page=100,
        )
        dir_a = catalog_store.get_directory(root.root_id, "/根/作品A")
        dir_b = catalog_store.get_directory(root.root_id, "/根/作品B")
        assert dir_a["next_verify_at"] != ""
        assert dir_a["next_verify_at"] != dir_b["next_verify_at"], (
            "不同目录应带不同 jitter 分散到期"
        )
        # jitter 确定性：同一目录重复提交得到相同 next_verify_at 偏移
        from datetime import datetime, timedelta, timezone

        jitter_a = int(hashlib.md5("/根/作品A".encode()).hexdigest(), 16) % int(
            catalog_store.ROLLING_JITTER_WINDOW.total_seconds()
        )
        expected = (
            datetime.now(timezone(timedelta(hours=8)))
            + catalog_store.VERIFY_INTERVAL
            + timedelta(seconds=jitter_a)
        ).isoformat()
        # 允许 ±2 秒（两次调用之间的时钟漂移）
        import time as _time

        actual = datetime.fromisoformat(dir_a["next_verify_at"])
        assert abs((actual - datetime.fromisoformat(expected)).total_seconds()) < 3


class TestFirstReconcilePreflight:
    def test_mismatched_mapping_aborts_zero_tombstone(self):
        """错绑：快照与远端完全无重叠 → abort，0 tombstone。"""
        from app.pipeline.discovery_handler import (
            _needs_first_remote_reconcile,
            _reconcile_preflight,
        )

        root = _make_root()
        catalog_store.bump_generation(root.root_id)
        # TXT 快照 root 直接成员
        _seed_snapshot_only_dirs(root.root_id, ["/根/冰菓", "/根/CLANNAD"], 1)
        # 远端 root 直接成员完全不相干
        scanner = TrackingScanner()
        scanner.seed("/根", [("电影", True, 100.0), ("剧集", True, 200.0)])
        assert _needs_first_remote_reconcile(root.root_id)
        with pytest.raises(ValueError, match="invalid_snapshot_mapping"):
            _reconcile_preflight(scanner, root.root_id, "/根")
        # 0 tombstone
        rows = catalog_store.get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE tombstone != ''"
        ).fetchone()
        assert int(rows["c"]) == 0

    def test_overlapping_mapping_passes(self):
        """正常差异（TXT 旧快照，远端多了新目录）→ 放行。"""
        from app.pipeline.discovery_handler import _reconcile_preflight

        root = _make_root()
        catalog_store.bump_generation(root.root_id)
        _seed_snapshot_only_dirs(root.root_id, ["/根/冰菓", "/根/CLANNAD"], 1)
        scanner = TrackingScanner()
        scanner.seed("/根", [("冰菓", True, 100.0), ("孤独摇滚", True, 200.0)])
        _reconcile_preflight(scanner, root.root_id, "/根")  # 不抛错

    def test_no_preflight_after_remote_verified(self):
        """root 已有 OpenList 验证（coverage>0）→ 不再需要 preflight。"""
        root = _make_root()
        catalog_store.bump_generation(root.root_id)
        _seed_snapshot_only_dirs(root.root_id, ["/根/冰菓"], 1)
        # 模拟一次 OpenList 成功提交 root（verified）
        scanner = TrackingScanner()
        scanner.seed("/根", [("冰菓", True, 100.0)])
        scan_directory_paginated(
            scanner, root.root_id, "/根", 2,
            parent_path="", depth=0, per_page=100,
        )
        from app.pipeline.discovery_handler import _needs_first_remote_reconcile

        assert not _needs_first_remote_reconcile(root.root_id)

    def test_no_automatic_full_reconcile(self):
        """无自动定时 full：incremental 只入队预算内的目录。"""
        root = _make_root()
        catalog_store.bump_generation(root.root_id)
        paths = [f"/根/作品{i:03d}" for i in range(10)]
        _seed_snapshot_only_dirs(root.root_id, paths, 1)
        catalog_store.prepare_scan(root.root_id, generation=2, mode="incremental")
        pending = catalog_store.list_pending_directories(root.root_id, limit=1000)
        queued = [p for p in pending if p["state"] == "queued"]
        assert len(queued) == 10  # ≤ budget 50，全部入队但下一轮不会自动全扫
