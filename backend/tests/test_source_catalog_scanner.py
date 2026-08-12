"""补完 5 验收：115/百度/本地统一接入 Source Catalog（SourceCatalogScanner）。"""
from __future__ import annotations

from app.catalog.scanner import SourceCatalogScanner


class FakeTxtAdapter:
    """模拟 pan115/baidu 目录树 TXT 适配器。"""

    def snapshot_entries(self, input_path: str, source_root: str):
        from app.catalog.models import SourceNodeInput

        return [
            SourceNodeInput(
                remote_path="动画/作品A/视频1.mkv", name="视频1.mkv",
                kind="file", parent_path="动画/作品A", size=100, mtime=1.0,
            ),
            SourceNodeInput(
                remote_path="动画/作品A/视频2.mkv", name="视频2.mkv",
                kind="file", parent_path="动画/作品A", size=200, mtime=2.0,
            ),
            SourceNodeInput(
                remote_path="动画/作品B", name="作品B", kind="dir",
                parent_path="动画", size=None, mtime=3.0,
            ),
        ]


class FakeLocalAdapter:
    """模拟 local 分页枚举适配器。"""

    @property
    def capabilities(self):
        return {"paginated": True}

    def enumerate_directory(self, remote_path, page=1, per_page=100):
        from app.catalog.models import DirectoryPage, SourceNodeInput

        if remote_path == "/本地":
            entries = [
                SourceNodeInput(
                    remote_path=f"/本地/作品{i}", name=f"作品{i}", kind="dir",
                    parent_path="/本地", size=None, mtime=float(i),
                )
                for i in range(3)
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


class TestTxtSnapshotSources:
    def test_pan115_snapshot_paginated(self):
        """115 TXT 快照：目录聚合 + 分页枚举。"""
        scanner = SourceCatalogScanner(
            source="pan115", adapter=FakeTxtAdapter(),
            input_path="tree.txt", source_root="/",
        )
        page1 = scanner.enumerate_directory("/动画/作品A", page=1, per_page=1)
        assert len(page1.entries) == 1
        assert page1.total == 2
        assert page1.entries[0].remote_path.endswith("视频1.mkv")
        page2 = scanner.enumerate_directory("/动画/作品A", page=2, per_page=1)
        assert page2.entries[0].remote_path.endswith("视频2.mkv")

    def test_baidu_snapshot_unknown_dir_returns_empty(self):
        scanner = SourceCatalogScanner(
            source="baidu", adapter=FakeTxtAdapter(),
            input_path="tree.txt", source_root="/",
        )
        page = scanner.enumerate_directory("/不存在的目录", page=1, per_page=100)
        assert page.total == 0
        assert page.entries == []


class TestLocalSource:
    def test_local_paginated_passthrough(self):
        """local 分页直通适配器。"""
        scanner = SourceCatalogScanner(
            source="local", adapter=FakeLocalAdapter(), source_root="/本地",
        )
        page = scanner.enumerate_directory("/本地", page=1, per_page=2)
        assert page.total == 3
        assert len(page.entries) == 2
        page2 = scanner.enumerate_directory("/本地", page=2, per_page=2)
        assert len(page2.entries) == 1
