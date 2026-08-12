"""OpenList 目录分页枚举（Source Catalog 契约）。

- 固定并发 1（由调用方单线程 + 本扫描器限流保证）；
- 默认 2 次/秒；429/5xx/超时分类透传给上层（job attempt 层处理重试）；
- 只产出物理条目（SourceNodeInput），不复制识别规则。
"""

from __future__ import annotations

import time

from app.catalog.models import DirectoryPage, SourceNodeInput

DEFAULT_RATE_PER_SECOND = 2.0


class OpenListDirectoryScanner:
    def __init__(self, client, rate_per_second: float = DEFAULT_RATE_PER_SECOND):
        self.client = client
        self.rate_per_second = rate_per_second
        self._last_request_at = [0.0]

    def rate_limit(self) -> None:
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
