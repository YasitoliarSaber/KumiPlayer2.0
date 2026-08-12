# -*- coding: utf-8 -*-
"""模块 1 阶段 B：discovery_scan 冷却拦截测试。

OpenList 连接处于 cooling_down 时，``handle_discovery_scan`` 必须：
- 不构造扫描器、不跑 DiscoveryEngine、不发任何远程请求；
- 直接返回 cooling_down 摘要（job 以 completed 结束）；
- 保留 Source Catalog 已有数据（不删除）。
"""

import pytest

from app.catalog import source_health
from app.db.database import close_connection, init_db
from app.integrations.openlist.governor import governor_connection_key


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """临时 SQLite（隔离 source_health 表与 catalog 表）。"""
    db_path = tmp_path / "discovery_cooldown.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


class _FakeConfig:
    openlist_server_url = "https://ol.example.com"
    openlist_username = "quark-user"
    openlist_password = "p@ss"


def _make_root():
    from app.catalog import store as catalog_store

    catalog_store.create_source(
        source_id="openlist-test", source_type="openlist",
        provider_id="", ingest_method="openlist_api",
        connection_key="ck", display_name="OpenList",
    )
    catalog_store.create_source_root(
        source_id="openlist-test",
        remote_locator="/动画",
        local_locator="K:/动画",
        import_family="anime",
    )
    root = catalog_store.list_source_roots("openlist-test")[0]
    generation = catalog_store.bump_generation(root.root_id)
    return root, generation


class _FakeEngine:
    """不产生任何网络行为的最小引擎替身。"""

    def run(self, should_cancel=None, progress_callback=None, on_unit=None, rate_limiter=None):
        return []


def _patch_config(monkeypatch):
    """让 handler 冷却检查读取到假配置（与 _build_scanner 同源）。"""
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: _FakeConfig(),
    )


class TestDiscoveryCooldown:
    def test_cooling_down_skips_scan_entirely(self, monkeypatch):
        """冷却中：不构造扫描器（monkeypatch 抛异常反证），直接返回冷却摘要。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)

        def _must_not_build(*args, **kwargs):
            raise AssertionError("冷却中不应构造扫描器")

        monkeypatch.setattr("app.pipeline.discovery_handler._build_scanner", _must_not_build)

        source_health.enter_cooldown(
            governor_connection_key(_FakeConfig.openlist_server_url, _FakeConfig.openlist_username),
            reason_kind="risk_control",
            cooldown_seconds=3600,
        )

        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert result["root_id"] == root.root_id
        assert result["generation"] == generation
        assert result["units"] == []
        summary = result["summary"]
        assert summary["health"] == "cooling_down"
        assert summary["plan_ready"] == 0
        assert summary["needs_review"] == 0
        assert summary["mirror_enqueued"] == 0
        assert summary["failed_count"] == 0
        assert summary["failed_paths"] == []
        assert "访问保护" in summary["message"]

    def test_cooling_down_with_429_reason(self, monkeypatch):
        """429 触发的冷却同样拦截 discovery（reason_kind 仅记录，不改变行为）。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应构造扫描器")),
        )
        source_health.enter_cooldown(
            governor_connection_key(_FakeConfig.openlist_server_url, _FakeConfig.openlist_username),
            reason_kind="rate_limit",
            cooldown_seconds=3600,
        )
        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert result["summary"]["health"] == "cooling_down"
        assert result["units"] == []

    def test_healthy_connection_still_scans(self, monkeypatch):
        """无冷却记录时保持原行为：正常构造扫描器并跑引擎。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)

        built: list = []
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda root: built.append(1) or object(),
        )
        monkeypatch.setattr(
            "app.pipeline.discovery_handler.DiscoveryEngine",
            lambda *a, **k: _FakeEngine(),
        )

        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert built == [1]  # 扫描器确实被构造
        assert result["summary"].get("health") != "cooling_down"
        assert result["units"] == []

    def test_cooldown_expired_becomes_probe_and_scans(self, monkeypatch):
        """冷却期结束后 can_request 放行（probe），扫描正常执行。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        built: list = []
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda root: built.append(1) or object(),
        )
        monkeypatch.setattr(
            "app.pipeline.discovery_handler.DiscoveryEngine",
            lambda *a, **k: _FakeEngine(),
        )
        key = governor_connection_key(_FakeConfig.openlist_server_url, _FakeConfig.openlist_username)
        # 冷却窗口 [1000, 1100)：now=2000 已过期，can_request 应放行为 probe
        source_health.enter_cooldown(
            key, reason_kind="risk_control",
            cooldown_seconds=100, now=1000.0,
        )
        allowed, record = source_health.can_request(key, now=2000.0)
        assert allowed and record.state == "probe"
        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert built == [1]
