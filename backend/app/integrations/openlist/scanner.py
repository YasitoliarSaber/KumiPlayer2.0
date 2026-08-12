"""OpenList 目录分页枚举（Source Catalog 契约）。

- 固定并发 1（由调用方单线程 + 连接级 governor 限速保证）；
- 限速收口：连接级请求间隔由 OpenListClient 内部 governor 统一负责
  （默认 1 次/秒/连接，模块 1 安全默认），本扫描器不再维护实例级计时；
- 429/5xx/超时分类透传给上层（job attempt 层处理重试）；
- 只产出物理条目（SourceNodeInput），不复制识别规则。
"""

from __future__ import annotations

import time

from app.catalog.models import DirectoryPage, SourceNodeInput

DEFAULT_RATE_PER_SECOND = 1.0


class OpenListDirectoryScanner:
    def __init__(self, client, rate_per_second: float = DEFAULT_RATE_PER_SECOND):
        self.client = client
        self.rate_per_second = rate_per_second
        #: 兼容未接入 governor 的调用方（如测试假客户端）的实例级计时回退
        self._last_request_at = [0.0]

    def rate_limit(self) -> None:
        """限速已由 client 内部 governor 统一负责（模块 1 阶段 B 收口）。

        真实 OpenListClient 每次请求（login/_post）前都会 acquire 连接级
        governor，这里必须为空操作，否则与 client 内部 acquire 双重计时
        会把速率减半。仅对未接入 governor 的假客户端回退实例级计时，
        保持旧调用方与既有测试语义不变。
        """
        governor = getattr(self.client, "_governor", None)
        conn_key = getattr(self.client, "_conn_key", None)
        if governor is not None and conn_key is not None:
            return
        # 兼容回退：无 governor 的客户端（测试假客户端）用实例级计时
        if self.rate_per_second <= 0:
            return
        interval = 1.0 / self.rate_per_second
        elapsed = time.monotonic() - self._last_request_at[0]
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_at[0] = time.monotonic()

    def enumerate_directory(self, remote_path: str, page: int = 1, per_page: int = 100) -> DirectoryPage:
        """分页枚举单个目录（不递归）。"""
        self.rate_limit()
        dir_page = self.client.list_dir(
            remote_path, page=page, per_page=per_page, refresh=False,
        )
        entries = [
            SourceNodeInput(
                name=item.name,
                remote_path=item.remote_path,
                parent_path=remote_path,
                kind="dir" if item.is_dir else "file",
                size=item.size,
                mtime=item.modified,
            )
            for item in dir_page.entries
        ]
        return DirectoryPage(entries=entries, total=dir_page.total)
