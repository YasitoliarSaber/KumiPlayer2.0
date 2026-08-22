# -*- coding: utf-8 -*-
"""TXT baseline → OpenList 增量的全链路请求数验证。

用户核心问题：「首次 TXT 建库后，OpenList 刷新是不是真正的增量，
而不是重新完整读一遍？」

本文件用带调用计数的假扫描器走真实 handle_discovery_scan（绑定增量扫描
按钮触发的同一 handler），以数字证明：
- 第 1/2/3 轮增量：每轮只 list 根目录 + 滚动批次（≤ 50+根+对账），
  远小于基线目录总数；
- 滚动验证耗尽后的稳态轮：只 list 根目录 1 次（外加根目录对账）。
"""

import pytest

from app.catalog import store as catalog_store
from app.catalog.models import DirectoryPage, SourceNodeInput


class TrackingScanner:
    """记录每次远端枚举的假 OpenList 扫描器（source=openlist 通道语义）。"""

    source = "openlist"

    def __init__(self, tree: dict[str, list[tuple[str, bool]]]):
        self.tree = tree
        self.calls: list[str] = []

    def enumerate_directory(self, remote_path, page=1, per_page=100):
        self.calls.append(remote_path)
        children = self.tree.get(remote_path, [])
        entries = [
            SourceNodeInput(
                remote_path=f"{remote_path}/{name}",
                name=name,
                kind="dir" if is_dir else "file",
                parent_path=remote_path,
                size=None if is_dir else 10,
                mtime=None,
            )
            for name, is_dir in children
        ]
        start = (page - 1) * per_page
        return DirectoryPage(entries=entries[start:start + per_page], total=len(entries))


TOTAL_WORKS = 120
ROOT = "/115网盘/动画"


def _build_tree() -> dict[str, list[tuple[str, bool]]]:
    tree: dict[str, list[tuple[str, bool]]] = {
        ROOT: [(f"作品{i:03d}", True) for i in range(TOTAL_WORKS)],
    }
    for i in range(TOTAL_WORKS):
        tree[f"{ROOT}/作品{i:03d}"] = [(f"第01话.mkv", False)]
    return tree


@pytest.fixture()
def provider_root(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    monkeypatch.setattr("app.db.database._db_path", tmp_path / "inc.db")
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()

    catalog_store.create_source(
        source_id="pan115-test", source_type="pan115",
        provider_id="pan115", ingest_method="directory_tree",
        connection_key="pan115-test", display_name="115 目录树",
    )
    root = catalog_store.create_source_root(
        source_id="pan115-test", remote_locator=ROOT,
        local_locator=r"K:\115网盘\动画", import_family="anime",
    )
    root = catalog_store.get_source_root(root.root_id)

    # 模拟 TXT bootstrap 结果：120 个作品目录 complete 且从未被远端验证
    from app.db.database import get_connection

    conn = get_connection()
    # 根目录行（真实 TXT bootstrap 扫描根后必然存在）：最旧，滚动批次首个入队
    conn.execute(
        """
        INSERT INTO source_directories (
            root_id, remote_path, parent_path, depth, state,
            entry_count, last_verified_at, next_verify_at
        ) VALUES (?, ?, '', 0, 'complete', 0, '2026-08-16T00:00:10', '')
        """,
        (root.root_id, ROOT),
    )
    for i in range(TOTAL_WORKS):
        path = f"{ROOT}/作品{i:03d}"
        seconds = 1000 + i
        verified = f"2026-08-16T00:{seconds // 60:02d}:{seconds % 60:02d}"
        conn.execute(
            """
            INSERT INTO source_directories (
                root_id, remote_path, parent_path, depth, state,
                entry_count, last_verified_at, next_verify_at
            ) VALUES (?, ?, ?, 1, 'complete', 0, ?, '')
            """,
            (root.root_id, path, ROOT, verified),
        )
        conn.execute(
            """
            INSERT INTO source_nodes (
                root_id, remote_path, parent_path, name, kind, size, mtime,
                logical_locator, first_seen_generation, last_seen_generation,
                tombstone, provider_id, route_id
            ) VALUES (?, ?, ?, ?, 'file', 10, NULL, ?, 1, 1, '', 'pan115', '')
            """,
            (root.root_id, f"{path}/第01话.mkv", path, "第01话.mkv",
             rf"K:\115网盘\动画\作品{i:03d}\第01话.mkv"),
        )
    conn.commit()
    catalog_store.mark_baseline_completed(root.root_id, 1)
    yield root
    close_connection()


def _run_incremental_round(monkeypatch, root, round_no: int) -> TrackingScanner:
    """跑一轮绑定增量扫描（与 /bound-roots/rescan 同一条 handler 链）。"""
    from app.pipeline import discovery_handler, orchestrator

    scanner = TrackingScanner(_build_tree())
    monkeypatch.setattr(discovery_handler, "_build_scanner", lambda payload: scanner)
    monkeypatch.setattr(orchestrator, "enqueue_mirror", lambda *a, **k: "job-mirror")

    generation = catalog_store.bump_generation(root.root_id)
    orchestrator.enqueue_scan(
        root.root_id, generation, "pan115-test",
        scan_mode="incremental", scan_channel="openlist",
    )
    discovery_handler.handle_discovery_scan(
        {"root_id": root.root_id, "generation": generation, "scan_channel": "openlist"},
    )
    return scanner


class TestTrueIncrementalByRequestCount:
    def test_round1_incremental_lists_root_plus_rolling_batch_not_everything(self, provider_root, monkeypatch):
        scanner = _run_incremental_round(monkeypatch, provider_root, 1)
        listed = set(scanner.calls)
        # 根目录必然重新验证（发现根下新增/移除作品）
        assert ROOT in listed
        # 全部 121 个目录（根 + 120 作品）绝不能被整棵重读
        assert len(scanner.calls) < 60, f"第 1 轮增量 list 了 {len(scanner.calls)} 个目录，超出滚动预算"
        # 被验证的目录从此获得远端验证时间戳
        from app.db.database import get_connection

        verified = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ? AND last_remote_verified_at != ''",
            (provider_root.root_id,),
        ).fetchone()["c"]
        assert verified > 0

    def test_steady_state_incremental_lists_root_only(self, provider_root, monkeypatch):
        # 跑 3 轮耗尽滚动预算（120 / 50 → 3 轮），再跑第 4 轮看稳态
        for round_no in range(1, 4):
            _run_incremental_round(monkeypatch, provider_root, round_no)

        from app.db.database import get_connection

        verified = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ? AND last_remote_verified_at != ''",
            (provider_root.root_id,),
        ).fetchone()["c"]
        assert verified >= TOTAL_WORKS, f"滚动验证未完成：{verified}/{TOTAL_WORKS}"

        scanner = _run_incremental_round(monkeypatch, provider_root, 4)
        # 稳态：只有根目录需要 list（对账 preflight 已因全部验证过而不再触发，
        # 目录均处于 24h 验证周期的未来）
        assert set(scanner.calls) == {ROOT}, (
            f"稳态增量仍 list 了 {len(scanner.calls)} 个目录：{sorted(scanner.calls)[:5]}…"
        )
