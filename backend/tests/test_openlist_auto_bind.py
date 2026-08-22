# -*- coding: utf-8 -*-
"""OpenList 自动联动（TXT baseline 后按本地挂载路径反推并自动绑定）回归。

覆盖：
- derive_remote_path 反推（挂载内 / 大小写不敏感 / 挂载外返回 None / 非 / 远端根拼接）；
- try_auto_bind_provider_root 状态机：bound / already / unconfigured /
  skipped（provider 不一致、不在挂载根下、基线未就绪）。
"""

import pytest

from app.catalog import store as catalog_store
from app.integrations.openlist.providers import (
    OpenListRouteConfig,
    derive_remote_path,
)


class TestDeriveRemotePath:
    def test_under_mount_root_maps_to_remote(self):
        assert derive_remote_path("K:\\", "/", r"K:\115网盘\动画") == "/115网盘/动画"

    def test_drive_letter_case_insensitive(self):
        assert derive_remote_path("k:\\", "/", r"K:\115网盘\动画") == "/115网盘/动画"

    def test_nested_mount_and_remote_root(self):
        assert derive_remote_path(r"K:\挂载", "/media", r"K:\挂载\115\动画") == "/media/115/动画"

    def test_outside_mount_returns_none(self):
        assert derive_remote_path("K:\\", "/", r"D:\115网盘\动画") is None
        assert derive_remote_path(r"K:\115", "/", r"K:\百度网盘\动画") is None

    def test_mount_root_itself_is_none(self):
        # 反推结果必须是具体目录，不能是远端根
        assert derive_remote_path("K:\\", "/", "K:\\") is None


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    monkeypatch.setattr("app.db.database._db_path", tmp_path / "auto.db")
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _make_provider_root(local_locator=r"K:\115网盘\动画"):
    catalog_store.create_source(
        source_id="pan115-auto", source_type="pan115",
        provider_id="pan115", ingest_method="directory_tree",
        connection_key="pan115-auto", display_name="115 目录树",
    )
    root = catalog_store.create_source_root(
        source_id="pan115-auto", remote_locator="/115网盘/动画",
        local_locator=local_locator, import_family="anime",
    )
    return catalog_store.get_source_root(root.root_id)


def _fake_config(monkeypatch, *, server=True, mount="K:\\", routes=None):
    from app.core import config as core_config

    class _Cfg:
        openlist_server_url = "http://localhost:5244" if server else ""
        openlist_remote_root = "/"
        openlist_mount_root = mount
        openlist_routes = routes if routes is not None else [
            OpenListRouteConfig(
                route_id="r1", label="115", remote_prefix="/115网盘",
                provider_id="pan115", enabled=True,
            ),
        ]

    monkeypatch.setattr(core_config, "load_config", lambda: _Cfg())
    monkeypatch.setattr(
        core_config, "resolve_openlist_credentials",
        lambda: ("admin", "pwd", "ok"),
    )


def _make_baseline_ready(root_id, *, ready=True):
    from app.db.database import get_connection

    conn = get_connection()
    for i in range(5):
        conn.execute(
            """
            INSERT INTO source_directories (
                root_id, remote_path, parent_path, depth, state
            ) VALUES (?, ?, '/115网盘/动画', 1, 'complete')
            """,
            (root_id, f"/115网盘/动画/作品{i:02d}"),
        )
    conn.commit()
    catalog_store.set_baseline_target(root_id, 1)
    if ready:
        catalog_store.mark_baseline_completed(root_id, 1)


class TestTryAutoBind:
    def test_binds_when_route_and_mount_match(self, db, monkeypatch):
        from app.catalog.binding import try_auto_bind_provider_root

        root = _make_provider_root()
        _fake_config(monkeypatch)
        _make_baseline_ready(root.root_id)

        assert try_auto_bind_provider_root(root) == "bound"

        fresh = catalog_store.get_source_root(root.root_id)
        assert fresh.openlist_conn_hash
        assert fresh.openlist_remote_locator == "/115网盘/动画"

    def test_already_bound_returns_already(self, db, monkeypatch):
        from app.catalog.binding import try_auto_bind_provider_root

        root = _make_provider_root()
        _fake_config(monkeypatch)
        _make_baseline_ready(root.root_id)
        assert try_auto_bind_provider_root(root) == "bound"
        assert try_auto_bind_provider_root(root) == "already"

    def test_unconfigured_openlist_returns_unconfigured(self, db, monkeypatch):
        from app.catalog.binding import try_auto_bind_provider_root

        root = _make_provider_root()
        _fake_config(monkeypatch, server=False)
        _make_baseline_ready(root.root_id)

        assert try_auto_bind_provider_root(root) == "unconfigured"
        assert not catalog_store.get_source_root(root.root_id).openlist_conn_hash

    def test_provider_mismatch_route_skips(self, db, monkeypatch):
        from app.catalog.binding import try_auto_bind_provider_root

        root = _make_provider_root()
        _fake_config(monkeypatch, routes=[
            OpenListRouteConfig(
                route_id="r2", label="百度", remote_prefix="/115网盘",
                provider_id="baidu", enabled=True,
            ),
        ])
        _make_baseline_ready(root.root_id)

        status = try_auto_bind_provider_root(root)
        assert status.startswith("skipped:")
        assert not catalog_store.get_source_root(root.root_id).openlist_conn_hash

    def test_outside_mount_skips(self, db, monkeypatch):
        from app.catalog.binding import try_auto_bind_provider_root

        root = _make_provider_root(local_locator=r"D:\115网盘\动画")
        _fake_config(monkeypatch)
        _make_baseline_ready(root.root_id)

        assert try_auto_bind_provider_root(root) == "skipped:not-under-mount"

    def test_baseline_not_ready_skips(self, db, monkeypatch):
        from app.catalog.binding import try_auto_bind_provider_root

        root = _make_provider_root()
        _fake_config(monkeypatch)
        _make_baseline_ready(root.root_id, ready=False)

        status = try_auto_bind_provider_root(root)
        assert status == "skipped:baseline-not-ready"
