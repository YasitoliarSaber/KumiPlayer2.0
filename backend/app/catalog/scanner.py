"""补完 5：通用来源扫描器（把 115/百度/本地统一为分页枚举契约）。

- local：适配器分页枚举（真实目录遍历）；
- pan115 / baidu：目录树 TXT 一次性快照 → 分页枚举（内存分页切片）；
- OpenList：保持 client 分页枚举（原 OpenListDirectoryScanner）。
DiscoveryEngine 只依赖 enumerate_directory，不再绑定 OpenList client。
"""

from __future__ import annotations

from app.catalog.models import DirectoryPage, SourceNodeInput
from app.integrations.openlist.scanner import OpenListDirectoryScanner


class SourceCatalogScanner:
    """按 source 构造统一扫描器，暴露 enumerate_directory(remote_path, page, per_page)。

    目录语义：remote_path 使用与 OpenList 一致的绝对路径（/ 开头）；
    115/百度 TXT 快照的 relative_path 已是相对路径，这里以 root 为前缀拼接。
    """

    def __init__(self, source: str, adapter=None, client=None, input_path: str = "", source_root: str = "/"):
        self.source = source
        self._adapter = adapter
        self._input_path = input_path
        self._source_root = source_root
        self._snapshot: list[SourceNodeInput] | None = None
        self._dir_index: dict[str, list[SourceNodeInput]] = {}
        self._local_root = source_root if source == "local" else ""
        if source == "openlist" and client is not None:
            self._openlist = OpenListDirectoryScanner(client)
        else:
            self._openlist = None

    # -- 快照加载（115/百度 one-shot） ---------------------------------

    def _ensure_snapshot(self) -> None:
        if self._snapshot is not None:
            return
        if self._adapter is None or not self._input_path:
            raise RuntimeError(f"{self.source} 需要目录树 TXT 输入（adapter + input_path）")
        entries = self._adapter.snapshot_entries(self._input_path, self._source_root)
        self._snapshot = list(entries)
        # 按父目录聚合：remote_path 以 root 前缀规范化
        root = self._source_root.rstrip("/") or ""
        entries: list[SourceNodeInput] = []
        for entry in self._snapshot:
            raw = entry.remote_path
            if not raw.startswith("/"):
                raw = f"{root}/{raw.lstrip('/')}" if root else f"/{raw.lstrip('/')}"
            entries.append(entry)
            entry.remote_path = raw
        # 补全父目录链条目（快照可能只列文件，父目录没有显式 dir 条目）
        seen = {entry.remote_path for entry in entries}
        for entry in list(entries):
            parent = entry.remote_path.rsplit("/", 1)[0] or "/"
            while parent not in seen and parent != "/":
                dir_entry = SourceNodeInput(
                    remote_path=parent,
                    name=parent.rsplit("/", 1)[-1],
                    kind="dir",
                    parent_path=parent.rsplit("/", 1)[0] or "/",
                )
                entries.append(dir_entry)
                seen.add(parent)
                parent = parent.rsplit("/", 1)[0] or "/"
        # 按规范化后的 remote_path 计算父目录，保证与枚举键一致
        for entry in entries:
            parent = entry.remote_path.rsplit("/", 1)[0] or "/"
            self._dir_index.setdefault(parent, []).append(entry)

    # -- 契约 ----------------------------------------------------------

    def enumerate_directory(self, remote_path: str, page: int = 1, per_page: int = 100) -> DirectoryPage:
        if self._openlist is not None:
            return self._openlist.enumerate_directory(remote_path, page=page, per_page=per_page)
        if self.source == "local" and self._adapter is not None:
            return self._adapter.enumerate_directory(remote_path, page=page, per_page=per_page)
        self._ensure_snapshot()
        members = self._dir_index.get(remote_path.rstrip("/") or "/", [])
        total = len(members)
        start = (page - 1) * per_page
        chunk = members[start:start + per_page]
        return DirectoryPage(entries=chunk, total=total)
