"""HYB-1 验收：解耦 Source Identity 与 Scan Channel。

覆盖：
- enqueue_scan payload 持久化 scan_channel；
- 旧 job（无 scan_channel）按 source_id 前缀 fallback，行为不变；
- 显式 channel 优先分派（snapshot_pan115 / snapshot_baidu / openlist / local）；
- TXT scanner remote_root / local_root 拆分：remote_path 用 remote root
  前缀，adapter 收到 local root（logical_locator/real_path 用）；
- 本地/远端路径不串。
"""
from __future__ import annotations

import pytest

from app.catalog.scanner import SourceCatalogScanner
from app.catalog.models import SourceNodeInput
from app.pipeline import orchestrator


class RecordingTxtAdapter:
    """记录收到的 local_root，并校验 remote_path 是否带 remote root 前缀。"""

    def __init__(self):
        self.last_source_root = None

    def snapshot_entries(self, input_path: str, source_root: str):
        self.last_source_root = source_root
        return [
            SourceNodeInput(
                remote_path="动画/作品A/视频1.mkv", name="视频1.mkv",
                kind="file", parent_path="动画/作品A", size=100, mtime=1.0,
            ),
            SourceNodeInput(
                remote_path="动画/作品B", name="作品B", kind="dir",
                parent_path="动画", size=None, mtime=3.0,
            ),
        ]


class TestScannerRemoteLocalRootSplit:
    """TXT 快照通道：remote root 与 local root 拆开。"""

    def test_remote_root_prefix_and_local_root_adapter(self):
        """remote_path 以 remote root 为前缀；adapter 收到 local root。"""
        adapter = RecordingTxtAdapter()
        scanner = SourceCatalogScanner(
            source="pan115", adapter=adapter,
            input_path="tree.txt",
            source_root="/115网盘/动画",
            local_root=r"K:\动画",
        )
        page = scanner.enumerate_directory("/115网盘/动画/动画/作品A", page=1, per_page=100)
        assert page.total == 1
        assert page.entries[0].remote_path == "/115网盘/动画/动画/作品A/视频1.mkv"
        # adapter 收到的是本地挂载根（拼 logical_locator 用），不是远端前缀
        assert adapter.last_source_root == r"K:\动画"

    def test_local_root_defaults_to_remote_root_compat(self):
        """local_root 缺省时回退 source_root（旧调用兼容）。"""
        adapter = RecordingTxtAdapter()
        scanner = SourceCatalogScanner(
            source="pan115", adapter=adapter,
            input_path="tree.txt", source_root="/根",
        )
        scanner.enumerate_directory("/根/动画/作品A", page=1, per_page=100)
        assert adapter.last_source_root == "/根"

    def test_local_source_keeps_passthrough(self):
        """local 通道不受拆分影响：枚举直通，local_root 不参与。"""
        from tests.test_source_catalog_scanner import FakeLocalAdapter

        scanner = SourceCatalogScanner(
            source="local", adapter=FakeLocalAdapter(), source_root="/本地",
            local_root=r"K:\本地",
        )
        page = scanner.enumerate_directory("/本地", page=1, per_page=2)
        assert page.total == 3
        assert len(page.entries) == 2


class TestBuildScannerChannelDispatch:
    """_build_scanner 显式通道分派与旧 fallback。"""

    def _fake_openlist_scanner(self, root):
        return SourceCatalogScanner(source="openlist")

    def test_explicit_snapshot_pan115_channel(self, monkeypatch):
        """显式 snapshot_pan115 → TXT scanner，不看 source_id。"""
        from app.pipeline import discovery_handler

        built = {}

        def fake_build_txt(root, provider):
            built["provider"] = provider
            return "txt-scanner"

        monkeypatch.setattr(discovery_handler, "_build_txt_scanner", fake_build_txt)
        result = discovery_handler._build_scanner(
            {"source_id": "openlist-abc", "scan_channel": "snapshot_pan115",
             "remote_locator": "/115网盘/动画", "local_locator": r"K:\动画",
             "input_path": "/data/tree.txt"}
        )
        assert result == "txt-scanner"
        assert built["provider"] == "pan115"

    def test_explicit_snapshot_baidu_channel(self, monkeypatch):
        from app.pipeline import discovery_handler

        built = {}

        def fake_build_txt(root, provider):
            built["provider"] = provider
            return "txt-scanner"

        monkeypatch.setattr(discovery_handler, "_build_txt_scanner", fake_build_txt)
        discovery_handler._build_scanner(
            {"source_id": "openlist-abc", "scan_channel": "snapshot_baidu",
             "remote_locator": "/百度/动画", "local_locator": r"M:\百度",
             "input_path": "/data/tree.txt"}
        )
        assert built["provider"] == "baidu"

    def test_explicit_openlist_channel(self, monkeypatch):
        """显式 openlist 通道 → OpenList scanner，即使 source_id 是 pan115。"""
        from app.pipeline import discovery_handler

        def fake_openlist(root):
            return "openlist-scanner"

        monkeypatch.setattr(discovery_handler, "_build_openlist_scanner", fake_openlist)
        result = discovery_handler._build_scanner(
            {"source_id": "pan115-x", "scan_channel": "openlist",
             "remote_locator": "/115网盘/动画", "local_locator": r"K:\动画"}
        )
        assert result == "openlist-scanner"

    def test_fallback_source_id_prefix(self, monkeypatch):
        """旧 payload 无 scan_channel → 按 source_id 前缀 fallback。"""
        from app.pipeline import discovery_handler

        built = {}

        def fake_openlist(root):
            built["kind"] = "openlist"
            return "openlist-scanner"

        def fake_txt(root, provider):
            built["kind"] = "txt"
            built["provider"] = provider
            return "txt-scanner"

        monkeypatch.setattr(discovery_handler, "_build_openlist_scanner", fake_openlist)
        monkeypatch.setattr(discovery_handler, "_build_txt_scanner", fake_txt)
        # 旧 openlist job
        assert discovery_handler._build_scanner(
            {"source_id": "openlist-abc", "remote_locator": "/", "input_path": ""}
        ) == "openlist-scanner"
        # 旧 pan115 job
        discovery_handler._build_scanner(
            {"source_id": "pan115-x", "remote_locator": "/根",
             "local_locator": r"K:\根", "input_path": "/data/t.txt"}
        )
        assert built["kind"] == "txt" and built["provider"] == "pan115"


class TestEnqueueScanChannelPayload:
    """enqueue_scan payload 持久化 scan_channel，且旧调用兼容。"""

    def _make_root(self, source_id: str):
        from app.catalog import store as catalog_store
        from app.db.database import init_db

        init_db()
        catalog_store.create_source(
            source_id=source_id, source_type="pan115",
            provider_id="pan115", ingest_method="directory_tree",
            connection_key=source_id, display_name="测试来源",
        )
        root = catalog_store.create_source_root(
            source_id=source_id,
            remote_locator="/",
            local_locator=r"K:\动画",
            import_family="anime",
        )
        return catalog_store.get_source_root(root.root_id)

    def test_payload_carries_scan_channel(self):
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.jobs import store as job_store

        source_id = "pan115-chan-test"
        root = self._make_root(source_id)
        gen = catalog_store.bump_generation(root.root_id)
        job_id = orchestrator.enqueue_scan(
            root.root_id, gen, source_id,
            input_path="", scan_mode="incremental",
            scan_channel="snapshot_pan115",
        )
        try:
            job = job_store.get_job(job_id)
            assert job is not None
            assert job.payload.get("scan_channel") == "snapshot_pan115"
            assert job.payload.get("root_id") == root.root_id
        finally:
            get_connection().execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            get_connection().commit()

    def test_old_call_without_channel_has_empty_field(self):
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.jobs import store as job_store

        source_id = "pan115-chan-old"
        root = self._make_root(source_id)
        gen = catalog_store.bump_generation(root.root_id)
        job_id = orchestrator.enqueue_scan(root.root_id, gen, source_id)
        try:
            job = job_store.get_job(job_id)
            assert job is not None
            assert job.payload.get("scan_channel", "") == ""
        finally:
            get_connection().execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            get_connection().commit()
