"""补完 5：通用来源扫描器（把 115/百度/本地统一为分页枚举契约）。

- local：适配器分页枚举（真实目录遍历）；
- pan115 / baidu：目录树 TXT 一次性快照 → 分页枚举（内存分页切片）；
- OpenList：保持 client 分页枚举（原 OpenListDirectoryScanner）。
DiscoveryEngine 只依赖 enumerate_directory，不再绑定 OpenList client。
"""

from __future__ import annotations

from dataclasses import replace

from app.catalog.models import DirectoryPage, SourceNodeInput
from app.integrations.openlist.scanner import OpenListDirectoryScanner


class SourceCatalogScanner:
    """按 source 构造统一扫描器，暴露 enumerate_directory(remote_path, page, per_page)。

    目录语义：remote_path 使用与 OpenList 一致的绝对路径（/ 开头）；
    115/百度 TXT 快照的 relative_path 已是相对路径，这里以 root 为前缀拼接。

    RWK-8（Bound OpenList）：当 Provider root 绑定了 OpenList 增量通道
    （openlist_root != canonical_root 且均非空）时，enumerate 的入参
    （frontier 中的 canonical path）先映射为 OpenList 物理路径再请求；
    返回的物理条目再映射回 canonical namespace 写入 source_nodes——
    保证 TXT snapshot 与 OpenList 增量看到同一套 node identity，
    不会因双 namespace 产生重复 media unit 或错误 tombstone。
    """

    def __init__(
        self,
        source: str,
        adapter=None,
        client=None,
        input_path: str = "",
        source_root: str = "/",
        local_root: str = "",
        canonical_root: str = "",
        openlist_root: str = "",
    ):
        self.source = source
        self._adapter = adapter
        self._input_path = input_path
        # HYB-1：remote root（OpenList 风格绝对路径前缀）与 local root（TXT
        # 挂载根，用于拼 logical_locator/real_path）正式拆开，禁止同一个
        # source_root 同时承担两种语义。local_root 缺省回退 source_root，
        # 兼容旧调用（纯 TXT 链路 remote 前缀 == 本地挂载根）。
        # local 来源：枚举以 remote_path 为准，local root 仅在快照语义下
        # 回退 remote 前缀（历史行为保持）。
        self._source_root = source_root
        self._local_root = (local_root or source_root) if source != "local" else ""
        # RWK-8：bound 映射层（canonical Provider namespace ↔ OpenList physical）
        self._canonical_root = self._norm_root(canonical_root or source_root)
        self._openlist_root = self._norm_root(openlist_root)
        self._snapshot: list[SourceNodeInput] | None = None
        self._dir_index: dict[str, list[SourceNodeInput]] = {}
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
        # HYB-1：adapter 使用 local root 生成 logical_locator/real_path；
        # remote_path 规范化仍以 remote root（_source_root）为前缀。
        entries = self._adapter.snapshot_entries(self._input_path, self._local_root)
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

    def _bound(self) -> bool:
        """是否处于 bound 模式：OpenList 通道 + 双 namespace 均已配置且不同。"""
        return (
            self.source == "openlist"
            and self._openlist_root
            and self._canonical_root
            and self._openlist_root != self._canonical_root
        )

    @staticmethod
    def _norm_root(value: str) -> str:
        """规范化 namespace 根：去尾部斜杠。

        - 空输入 → 空串（未配置，bound 不生效）；
        - 显式 "/" → "/"（根）；其余去尾斜杠。
        """
        value = (value or "").strip()
        if not value:
            return ""
        stripped = value.rstrip("/")
        return stripped or "/"

    def _join(self, root: str, rest: str) -> str:
        """root + rest 拼接，杜绝双斜杠（rest 以 / 开头时去重）。"""
        rest = rest or ""
        if root == "/":
            return "/" + rest.lstrip("/")
        return (root.rstrip("/") + "/" + rest.lstrip("/")).rstrip("/") or "/"

    def _to_physical(self, canonical_path: str) -> str:
        """canonical（frontier 存证）→ OpenList 物理路径（请求用）。"""
        if not self._bound():
            return canonical_path
        base = self._norm_root(self._canonical_root)
        path = canonical_path.rstrip("/") or "/"
        if base == "/":
            # canonical 根即 "/"：任何绝对路径都在其下
            return self._join(self._openlist_root, path)
        if path == base or path.startswith(base + "/"):
            return self._join(self._openlist_root, path[len(base):])
        return canonical_path

    def _to_canonical(self, physical_path: str) -> str:
        """OpenList 物理路径（返回）→ canonical namespace（落库用）。"""
        if not self._bound():
            return physical_path
        base = self._norm_root(self._openlist_root)
        path = physical_path.rstrip("/") or "/"
        if base == "/":
            # OpenList 根即 "/"：返回路径原样映射到 canonical 根下
            return self._join(self._canonical_root, path)
        if path == base or path.startswith(base + "/"):
            return self._join(self._canonical_root, path[len(base):])
        return physical_path

    def enumerate_directory(self, remote_path: str, page: int = 1, per_page: int = 100) -> DirectoryPage:
        if self._openlist is not None:
            physical_path = self._to_physical(remote_path)
            page_result = self._openlist.enumerate_directory(
                physical_path, page=page, per_page=per_page
            )
            if not self._bound():
                return page_result
            # bound 模式：返回条目映射回 canonical namespace（parent 同步）
            entries = []
            for entry in page_result.entries:
                canonical = self._to_canonical(entry.remote_path)
                parent = self._to_canonical(entry.parent_path)
                entries.append(replace(entry, remote_path=canonical, parent_path=parent))
            return DirectoryPage(entries=entries, total=page_result.total)
        if self.source == "local" and self._adapter is not None:
            return self._adapter.enumerate_directory(remote_path, page=page, per_page=per_page)
        self._ensure_snapshot()
        members = self._dir_index.get(remote_path.rstrip("/") or "/", [])
        total = len(members)
        start = (page - 1) * per_page
        chunk = members[start:start + per_page]
        return DirectoryPage(entries=chunk, total=total)
