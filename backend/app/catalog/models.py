"""Source Catalog 数据模型（物理事实，不含作品/季度/TMDB 判断）。

每个来源适配器输出同一种 SourceNodeInput；
SQLite 保存 source_nodes / source_directories 物理事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceNodeInput:
    """来源适配器输出的单个物理条目（文件或目录）。"""

    name: str = ""
    remote_path: str = ""
    parent_path: str = ""
    kind: str = "file"  # file / dir
    size: int | None = None
    mtime: float | None = None
    etag: str = ""
    content_hash: str = ""
    remote_id: str = ""
    logical_locator: str = ""


@dataclass
class DirectoryPage:
    """单页目录枚举结果。"""

    entries: list[SourceNodeInput] = field(default_factory=list)
    total: int | None = None  # 服务端总数；未知为 None


@dataclass
class SourceRootRecord:
    root_id: str = ""
    source_id: str = ""
    remote_locator: str = ""
    normalized_locator: str = ""
    local_locator: str = ""
    import_family: str = "anime"
    import_scope: str = ""
    scan_policy: str = "standard"
    active_generation: int = 0
    last_successful_scan_at: str = ""
    # RWK-3：可选 OpenList 增量通道 binding（空 = 未绑定）
    openlist_conn_hash: str = ""
    openlist_remote_locator: str = ""
    # RWK-25/30：TXT snapshot baseline 状态机（target + completed）
    baseline_target_generation: int = 0
    baseline_completed_generation: int = 0
    baseline_completed_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> SourceRootRecord:
        data = dict(row)
        return cls(
            root_id=data["root_id"],
            source_id=data["source_id"],
            remote_locator=data.get("remote_locator") or "",
            normalized_locator=data.get("normalized_locator") or "",
            local_locator=data.get("local_locator") or "",
            import_family=data.get("import_family") or "anime",
            import_scope=data.get("import_scope") or "",
            scan_policy=data.get("scan_policy") or "standard",
            active_generation=data.get("active_generation") or 0,
            last_successful_scan_at=data.get("last_successful_scan_at") or "",
            openlist_conn_hash=data.get("openlist_conn_hash") or "",
            openlist_remote_locator=data.get("openlist_remote_locator") or "",
            baseline_target_generation=int(data.get("baseline_target_generation") or 0),
            baseline_completed_generation=int(data.get("baseline_completed_generation") or 0),
            baseline_completed_at=data.get("baseline_completed_at") or "",
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
        )
