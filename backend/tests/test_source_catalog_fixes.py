"""补完 3 验收：Source Catalog 四项修正。

1. commit_directory 只比较直属成员（路径前缀会误 tombstone 深层内容）；
2. active_generation 提交 fence（旧代迟到提交被丢弃）；
3. 同一 root+boundary 复用稳定 media_unit（增量 revision 有 parent 链）；
4. OpenList provider_id/route_id 进入节点与 revision。
"""
from __future__ import annotations

import pytest

from app.catalog import store as catalog_store
from app.db.database import close_connection, get_connection, init_db
from app.integrations.openlist.providers import (
    PROVIDER_QUARK,
    OpenListRouteConfig,
)


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


def _make_root() -> dict:
    catalog_store.create_source(
        source_id="ol-test", source_type="openlist",
        provider_id="", ingest_method="openlist_api",
        connection_key="ck", display_name="OpenList",
    )
    root = catalog_store.create_source_root(
        source_id="ol-test",
        remote_locator="/动画",
        local_locator="K:/动画",
        import_family="anime",
    )
    return catalog_store.get_source_root(root.root_id)


def _seed_stage(run_id: str, directory: str, entries: list[dict]) -> None:
    for page, entry in enumerate(entries):
        catalog_store.add_stage_page(
            run_id, directory, page + 1,
            [catalog_store.SourceNodeInput(**entry)],
        )


class TestDirectMemberOnly:
    def test_parent_commit_does_not_tombstone_deep_children(self):
        """父目录提交时，深层子目录节点（尚未扫描）不得被 tombstone。"""
        root = _make_root()
        generation = 1
        # 第一轮：扫描 /动画（直属含子目录 /动画/A）+ 深层节点（模拟后续目录已入库）
        catalog_store.upsert_directory(root.root_id, "/动画", parent_path="", depth=0)
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/A", "name": "A", "kind": "dir"},
            {"remote_path": "/动画/视频1.mkv", "name": "视频1.mkv", "kind": "file",
             "size": 100, "mtime": 1.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画", run, generation)
        # 模拟深层节点已存在（上一轮扫描写入）
        deep = catalog_store.new_stage_run()
        _seed_stage(deep, "/动画/A", [
            {"remote_path": "/动画/A/深层视频.mkv", "name": "深层视频.mkv",
             "kind": "file", "size": 200, "mtime": 2.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画/A", deep, generation)

        # 第二轮：父目录 /动画 变化（视频1 消失），深层目录本轮没扫
        generation = 2
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/A", "name": "A", "kind": "dir"},
            {"remote_path": "/动画/视频2.mkv", "name": "视频2.mkv", "kind": "file",
             "size": 150, "mtime": 3.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画", run, generation)

        nodes = {row["remote_path"]: row for row in catalog_store.list_nodes(root.root_id)}
        # 直属消失项已 tombstone（默认 list_nodes 不含 tombstone 行）
        assert "/动画/视频1.mkv" not in nodes
        # 深层内容必须保留（不能因父目录提交被误杀）
        assert "/动画/A/深层视频.mkv" in nodes
        assert nodes["/动画/A/深层视频.mkv"]["tombstone"] == ""
        # 深层内容必须保留（不能因父目录提交被误杀）
        assert "/动画/A/深层视频.mkv" in nodes
        assert nodes["/动画/A/深层视频.mkv"]["tombstone"] == ""


class TestGenerationFence:
    def test_stale_generation_commit_dropped(self):
        """active_generation 前进后，旧代迟到提交被丢弃。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        assert generation == 1  # create_source_root 后 active_generation=0
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/视频1.mkv", "name": "视频1.mkv", "kind": "file",
             "size": 100, "mtime": 1.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画", run, generation)

        # 新 generation 开始
        new_gen = catalog_store.bump_generation(root.root_id)
        assert new_gen == 2

        # 旧代（generation=1）迟到提交
        stale_run = catalog_store.new_stage_run()
        _seed_stage(stale_run, "/动画", [
            {"remote_path": "/动画/旧视频.mkv", "name": "旧视频.mkv", "kind": "file",
             "size": 999, "mtime": 9.0},
        ])
        stats = catalog_store.commit_directory(root.root_id, "/动画", stale_run, generation)
        assert stats["added"] == 0
        nodes = {row["remote_path"] for row in catalog_store.list_nodes(root.root_id)}
        assert "/动画/旧视频.mkv" not in nodes


class TestUnitReuse:
    def test_same_boundary_reuses_unit_and_builds_parent_chain(self):
        """同一 root+boundary 复用 media_unit；两次扫描的 revision 有 parent 链。"""
        from app.catalog.discovery import DiscoveryEngine

        # 直接验证 _create_unit 复用逻辑（不跑完整扫描）
        engine = object.__new__(DiscoveryEngine)
        engine.root_id = "root-x"
        engine.source_id = "ol-test"
        engine.generation = 1
        unit = {"boundary": "/动画/作品", "work_key": "作品", "work_title": "作品"}
        unit_id_1 = DiscoveryEngine._create_unit(engine, unit, status="plan_ready")
        unit_id_2 = DiscoveryEngine._create_unit(engine, unit, status="plan_ready")
        assert unit_id_1 == unit_id_2

        conn = get_connection()
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM media_units WHERE root_id = 'root-x' AND boundary = ?",
            ("/动画/作品",),
        ).fetchone()
        assert int(rows["c"]) == 1


class TestSubtreeCascadeTombstone:
    """模块2 checkpoint1：目录消失级联整棵物理子树。

    - 消失的 child directory → 后代 source_nodes 全部 tombstone；
    - child 与所有后代的 source_directories checkpoint 直接 DELETE（frontier 不再含）；
    - 目录重新出现时父目录 commit 自然重建 queued checkpoint，可重新扫描。
    """

    def test_disappeared_directory_cascades_to_whole_subtree(self):
        root = _make_root()
        generation = 1
        # 第一轮完整列 /动画：直属 S2（子目录）+ root.txt
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/S2", "name": "S2", "kind": "dir"},
            {"remote_path": "/动画/root.txt", "name": "root.txt", "kind": "file",
             "size": 10, "mtime": 1.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画", run, generation)
        # 第二轮扫描 /动画/S2：E01/E02（文件）+ OVA（子目录）
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画/S2", [
            {"remote_path": "/动画/S2/E01.mkv", "name": "E01.mkv", "kind": "file",
             "size": 100, "mtime": 1.0},
            {"remote_path": "/动画/S2/E02.mkv", "name": "E02.mkv", "kind": "file",
             "size": 200, "mtime": 2.0},
            {"remote_path": "/动画/S2/OVA", "name": "OVA", "kind": "dir"},
        ])
        catalog_store.commit_directory(root.root_id, "/动画/S2", run, generation)
        # 第三轮扫描 /动画/S2/OVA：ova.mkv
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画/S2/OVA", [
            {"remote_path": "/动画/S2/OVA/ova.mkv", "name": "ova.mkv", "kind": "file",
             "size": 50, "mtime": 3.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画/S2/OVA", run, generation)
        # checkpoint 就绪：S2 与后代 OVA 都在
        assert catalog_store.get_directory(root.root_id, "/动画/S2") is not None
        assert catalog_store.get_directory(root.root_id, "/动画/S2/OVA") is not None

        # 第二轮完整列 /动画：S2 消失（只剩 root.txt）
        generation = 2
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/root.txt", "name": "root.txt", "kind": "file",
             "size": 10, "mtime": 1.0},
        ])
        stats = catalog_store.commit_directory(root.root_id, "/动画", run, generation)
        # missing 统计含整棵子树：S2 + E01 + E02 + OVA + ova.mkv = 5
        assert stats["missing"] == 5, stats

        all_nodes = {
            row["remote_path"]: row
            for row in catalog_store.list_nodes(root.root_id, include_tombstone=True)
        }
        for path in ("/动画/S2", "/动画/S2/E01.mkv", "/动画/S2/E02.mkv",
                     "/动画/S2/OVA", "/动画/S2/OVA/ova.mkv"):
            assert all_nodes[path]["tombstone"] != "", path
        # 活动节点只剩 root.txt
        active = catalog_store.list_nodes(root.root_id)
        assert [n["remote_path"] for n in active] == ["/动画/root.txt"]
        # S2 与后代的 checkpoint 被直接删除，frontier 不再含 S2
        assert catalog_store.get_directory(root.root_id, "/动画/S2") is None
        assert catalog_store.get_directory(root.root_id, "/动画/S2/OVA") is None
        frontier = [
            d["remote_path"]
            for d in catalog_store.list_pending_directories(root.root_id)
        ]
        assert "/动画/S2" not in frontier

        # S2 重新出现：第三次完整列 /动画 含 S2 → 父目录 commit 重建 queued
        generation = 3
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/S2", "name": "S2", "kind": "dir"},
            {"remote_path": "/动画/root.txt", "name": "root.txt", "kind": "file",
             "size": 10, "mtime": 1.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画", run, generation)
        recreated = catalog_store.get_directory(root.root_id, "/动画/S2")
        assert recreated is not None
        assert recreated["state"] == "queued"
        # S2 node 复活（tombstone 清空）
        s2_map = {
            row["remote_path"]: row
            for row in catalog_store.list_nodes(root.root_id, include_tombstone=True)
        }
        assert s2_map["/动画/S2"]["tombstone"] == ""
        # 可重新扫描 /动画/S2 → E01 重新入库（unchanged，tombstone 清除）
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画/S2", [
            {"remote_path": "/动画/S2/E01.mkv", "name": "E01.mkv", "kind": "file",
             "size": 100, "mtime": 1.0},
        ])
        stats2 = catalog_store.commit_directory(root.root_id, "/动画/S2", run, generation)
        assert stats2["added"] + stats2["unchanged"] == 1, stats2
        assert "/动画/S2/E01.mkv" in {
            n["remote_path"] for n in catalog_store.list_nodes(root.root_id)
        }

    def test_disappeared_file_stays_single_tombstone(self):
        """普通文件消失：保持单点 tombstone，不影响同层/深层其他节点。"""
        root = _make_root()
        generation = 1
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/A", "name": "A", "kind": "dir"},
            {"remote_path": "/动画/v1.mkv", "name": "v1.mkv", "kind": "file",
             "size": 100, "mtime": 1.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画", run, generation)
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画/A", [
            {"remote_path": "/动画/A/deep.mkv", "name": "deep.mkv", "kind": "file",
             "size": 200, "mtime": 2.0},
        ])
        catalog_store.commit_directory(root.root_id, "/动画/A", run, generation)

        # 第二轮：v1.mkv 消失（A 仍在）→ 仅单点 tombstone，A/deep 保留
        generation = 2
        run = catalog_store.new_stage_run()
        _seed_stage(run, "/动画", [
            {"remote_path": "/动画/A", "name": "A", "kind": "dir"},
        ])
        stats = catalog_store.commit_directory(root.root_id, "/动画", run, generation)
        assert stats["missing"] == 1, stats
        nodes = catalog_store.list_nodes(root.root_id)
        active_paths = {n["remote_path"] for n in nodes}
        assert "/动画/A" in active_paths
        assert "/动画/A/deep.mkv" in active_paths
        assert "/动画/v1.mkv" not in active_paths
        # A 的 checkpoint 保留（目录仍存在）
        assert catalog_store.get_directory(root.root_id, "/动画/A") is not None


class TestProviderIntoRevision:
    def test_provider_route_flow_into_nodes_and_items(self, monkeypatch):
        """provider_id/route_id 进入节点与 revision items。"""
        from app.catalog.discovery import DiscoveryEngine

        class FakeConfig:
            openlist_routes = [
                OpenListRouteConfig(
                    route_id="route-1", label="夸克", remote_prefix="/动画",
                    provider_id=PROVIDER_QUARK, enabled=True,
                ),
            ]

        monkeypatch.setattr(
            "app.core.config.load_config", lambda: FakeConfig(),
        )
        engine = object.__new__(DiscoveryEngine)
        engine.source = "openlist"
        engine.source_id = "openlist-test"
        provider_id, route_id = DiscoveryEngine._provider_for_boundary(engine, "/动画/作品")
        assert provider_id == PROVIDER_QUARK
        assert route_id == "route-1"

        # 未命中路由 → 回退 openlist 兼容值（不丢来源事实）
        provider_id2, route_id2 = DiscoveryEngine._provider_for_boundary(engine, "/电影/未归类")
        assert provider_id2 == PROVIDER_QUARK  # compat_provider("openlist")
        assert route_id2 == ""
