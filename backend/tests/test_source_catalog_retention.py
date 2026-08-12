# -*- coding: utf-8 -*-
"""模块 1 阶段 C：风险失败 → Source Catalog 旧节点完全保留。

覆盖两条入口：

1. ``discovery_handler.handle_discovery_scan`` 冷却拦截路径（阶段 B 已实现）：
   第一次扫描成功建立 source_nodes；连接被置为 cooling_down 后再次扫描 →
   不构造扫描器、不发任何远程请求（FakeClient 调用计数不变）、旧节点与
   directory checkpoint 全部保留（无删除、无 tombstone、状态不被清除）。

2. ``DiscoveryEngine`` 直接 run 时 client 抛 ``OpenListRiskControlError``：
   engine.failed_paths 记录失败目录，已建立节点原样保留（无删除、无 tombstone），
   directory checkpoint 未被清除（子目录保持 complete，根目录标记 failed 供重试）。

全程临时 SQLite + FakeClient（httpx 无真实传输），不访问网络。
"""
from __future__ import annotations

import pytest

from app.catalog import source_health
from app.catalog import store as catalog_store
from app.catalog.discovery import DiscoveryEngine
from app.catalog.scanner import SourceCatalogScanner
from app.db.database import close_connection, init_db
from app.integrations.openlist.governor import governor_connection_key
from app.integrations.openlist.models import OpenListEntry, OpenListRiskControlError


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """临时 SQLite（隔离 source_health 表与 catalog 表）。"""
    db_path = tmp_path / "retention.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


class _FakeConfig:
    """与 test_discovery_cooldown 同款假配置：handler 冷却检查读取。"""

    openlist_server_url = "https://ol.example.com"
    openlist_username = "quark-user"
    openlist_password = "p@ss"


# 远端目录树：path -> [(name, is_dir, size, modified)]
TREE = {
    "/动画": [
        ("作品A", True, None, None),
        ("作品B", True, None, None),
    ],
    "/动画/作品A": [("作品A - 01.mkv", False, 100, 1.0)],
    "/动画/作品B": [("作品B - 01.mkv", False, 200, 2.0)],
}


class FakeOpenListClient:
    """无网络假客户端：按 TREE 提供 list_dir 分页，可切换到抛风控错误。

    提供 _governor/_conn_key 属性与真实 OpenListClient 对齐（限速由 client
    内部 governor 负责），避免 OpenListDirectoryScanner 的实例级回退限速
    计时拖慢测试。
    """

    _governor = object()
    _conn_key = "fake-conn"

    def __init__(self, tree=None):
        self.tree = {path: list(items) for path, items in dict(tree or TREE).items()}
        self.calls: list[str] = []
        #: 第 N 次 list_dir 调用之后开始抛 OpenListRiskControlError（None=永不）
        self.fail_after: int | None = None

    def list_dir(self, remote_path, page=1, per_page=100, refresh=False):
        self.calls.append(remote_path)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise OpenListRiskControlError()
        items = self.tree.get(remote_path, [])
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=f"{remote_path.rstrip('/')}/{name}",
            )
            for name, is_dir, size, modified in items
        ]
        start = (page - 1) * per_page
        return type(
            "Page", (),
            {"entries": entries[start:start + per_page], "total": len(entries)},
        )()


def _make_root(source_id="openlist-test"):
    catalog_store.create_source(
        source_id=source_id, source_type="openlist",
        provider_id="", ingest_method="openlist_api",
        connection_key="ck", display_name="OpenList",
    )
    catalog_store.create_source_root(
        source_id=source_id,
        remote_locator="/动画",
        local_locator="K:/动画",
        import_family="anime",
    )
    return catalog_store.list_source_roots(source_id)[0]


def _make_scanner(client):
    """openlist 来源统一扫描器（内部包 OpenListDirectoryScanner → client.list_dir）。"""
    return SourceCatalogScanner(source="openlist", client=client)


def _node_paths(root_id):
    return {row["remote_path"] for row in catalog_store.list_nodes(root_id)}


def _assert_no_tombstones(root_id):
    rows = catalog_store.list_nodes(root_id, include_tombstone=True)
    assert rows, "第一次扫描应已建立节点"
    assert all(row["tombstone"] == "" for row in rows), "风险失败路径不得产生 tombstone"


class TestHandlerCooldownRetention:
    def test_risk_cooldown_keeps_old_nodes_and_makes_no_requests(self, monkeypatch):
        """第一次扫描建库；置冷却后第二次扫描：零请求、零删除、checkpoint 保留。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        client = FakeOpenListClient()
        monkeypatch.setattr("app.core.config.load_config", lambda: _FakeConfig())
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda *a, **k: _make_scanner(client),
        )

        # 第一次扫描：正常建库
        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert result["summary"]["failed_count"] == 0
        assert _node_paths(root.root_id)  # 节点已建立
        calls_after_first = len(client.calls)
        assert calls_after_first >= 3  # 根目录 + 作品A + 作品B
        dirs_before = catalog_store.list_all_directories(root.root_id)

        # 连接被风控：进入冷却（与 OpenListClient 上报相同的连接键）
        key = governor_connection_key(
            _FakeConfig.openlist_server_url, _FakeConfig.openlist_username
        )
        source_health.enter_cooldown(
            key, reason_kind="risk_control", cooldown_seconds=3600
        )
        allowed, record = source_health.can_request(key)
        assert not allowed and record.state == "cooling_down"

        # 第二次扫描：冷却拦截，零请求、零删除
        result2 = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert result2["summary"]["health"] == "cooling_down"
        assert "访问保护" in result2["summary"]["message"]
        assert result2["units"] == []
        assert len(client.calls) == calls_after_first  # 没有向 client 发出任何新请求
        assert _node_paths(root.root_id)  # 旧节点全部保留
        _assert_no_tombstones(root.root_id)

        # directory checkpoint 未被清除、状态未被改动
        dirs_after = catalog_store.list_all_directories(root.root_id)
        assert [d["remote_path"] for d in dirs_after] == [
            d["remote_path"] for d in dirs_before
        ]
        assert all(d["state"] == "complete" for d in dirs_after)


class TestEngineRiskFailureRetention:
    def test_risk_control_error_records_failed_paths_and_keeps_nodes(self, monkeypatch):
        """engine 直接 run：client 抛 OpenListRiskControlError → failed_paths 记录、旧节点保留。"""
        monkeypatch.setattr("app.core.config.load_config", lambda: _FakeConfig())
        root = _make_root()
        client = FakeOpenListClient()

        # 第一次：正常扫描建库
        engine = DiscoveryEngine(
            _make_scanner(client),
            source_id=root.source_id,
            root_id=root.root_id,
            generation=catalog_store.bump_generation(root.root_id),
        )
        results = engine.run(should_cancel=lambda: False)
        assert engine.failed_paths == []
        assert results  # 至少一个作品单元（作品A/作品B 直属视频）
        before = _node_paths(root.root_id)
        assert {"/动画/作品A", "/动画/作品B"} <= before
        calls_before = len(client.calls)

        # 第二次：新一轮扫描，client 现在抛风控错误（从下一次调用开始）
        new_gen = catalog_store.bump_generation(root.root_id)
        catalog_store.prepare_scan(root.root_id, generation=new_gen, mode="incremental")
        client.fail_after = len(client.calls)

        engine2 = DiscoveryEngine(
            _make_scanner(client),
            source_id=root.source_id,
            root_id=root.root_id,
            generation=new_gen,
        )
        results2 = engine2.run(should_cancel=lambda: False)
        # 失败目录被 engine 记录（根目录重扫失败即停，不扩散到子目录）
        assert engine2.failed_paths == ["/动画"]
        # 只对失败目录发过一次请求；未向任何已完成目录发出新请求
        assert len(client.calls) == calls_before + 1
        # 结尾基于持久目录重新结算已完成边界（数据库事实，零请求）
        assert {r["boundary"] for r in results2} == {"/动画/作品A", "/动画/作品B"}

        # 旧节点原样保留：无删除、无 tombstone
        assert _node_paths(root.root_id) == before
        _assert_no_tombstones(root.root_id)

        # directory checkpoint 未被清除：子目录保持 complete，根目录标记 failed（可重试）
        by_path = {
            d["remote_path"]: d
            for d in catalog_store.list_all_directories(root.root_id)
        }
        assert by_path["/动画/作品A"]["state"] == "complete"
        assert by_path["/动画/作品B"]["state"] == "complete"
        assert by_path["/动画"]["state"] == "failed"

