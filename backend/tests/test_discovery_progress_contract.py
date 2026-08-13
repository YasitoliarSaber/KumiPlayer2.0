"""Preflight 0：durable discovery 进度合同。

规划员要求：durable discovery task 的 progress/result 必须使用真实
discovery_* phases（discovery_scan / discovery_unit / discovery_done），
并且不得声明虚假的整树 total（旧 recursive scan 的整体总量语义不得复活）。
"""

import pytest

from app.catalog import store as catalog_store
from app.db.database import close_connection, init_db
from app.integrations.openlist.models import OpenListEntry
from app.integrations.openlist.scanner import OpenListDirectoryScanner


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "progress.db"
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


class FakeClient:
    def __init__(self, tree):
        self.tree = tree
        self.calls: list[str] = []

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        self.calls.append(path)
        items = self.tree.get(path, [])
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=f"{path.rstrip('/')}/{name}",
            )
            for name, is_dir, size, modified in chunk
        ]
        return type("Page", (), {"entries": entries, "total": len(items)})()


def _make_root():
    catalog_store.create_source(source_id="openlist-test", source_type="openlist")
    root = catalog_store.create_source_root(source_id="openlist-test", remote_locator="/动画")
    generation = catalog_store.bump_generation(root.root_id)
    return root, generation


def test_discovery_progress_uses_real_phases_without_fake_total(monkeypatch):
    """progress 只使用 discovery_* phases；载荷/结果不声明整树 total。"""
    from app.core import config as core_config
    from app.pipeline import discovery_handler

    tree = {
        "/动画": [("作品", True, None, None)],
        "/动画/作品": [("Season 1", True, None, None)],
        "/动画/作品/Season 1": [("作品 - S01E01.mkv", False, 100, 1.0)],
    }
    client = FakeClient(tree)

    # handler 构造扫描器时禁用网络：注入带 FakeClient 的分页扫描器
    def _fake_build_scanner(root):
        return OpenListDirectoryScanner(client, rate_per_second=0)

    monkeypatch.setattr(discovery_handler, "_build_scanner", _fake_build_scanner)
    monkeypatch.setattr(core_config, "load_config", lambda: _FakeConfig())

    root, generation = _make_root()
    events: list[tuple[int, str, dict]] = []

    def on_progress(progress, message, payload):
        events.append((progress, message, payload or {}))

    result = discovery_handler.handle_discovery_scan(
        {"root_id": root.root_id, "generation": generation},
        progress_callback=on_progress,
    )
    summary = result["summary"]

    # 1) 进度事件必须携带真实 discovery 阶段（catalog_directory 是目录级
    #    diff 的真实阶段；旧 recursive scan 的整体 total 语义不得出现）
    assert events, "应产生进度事件"
    phases = [p.get("phase") for (_, _, p) in events if "phase" in p]
    assert phases, "进度事件必须带 phase"
    for phase in phases:
        assert phase in {
            "discovery_scan",
            "discovery_unit",
            "discovery_done",
            "catalog_directory",
        }, f"非法的进度阶段: {phase}"

    # 2) 任何进度载荷不得声明整树 total（旧 recursive scan 伪造总量禁止复活）
    for (_, _, payload) in events:
        assert "total" not in payload, f"进度载荷不得伪造整树总量: {payload}"

    # 3) 结果 summary 使用真实计数：failed_count 为整数，且不声明整树 total
    assert isinstance(summary["failed_count"], int)
    assert "total" not in summary
