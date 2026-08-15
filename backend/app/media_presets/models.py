# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class MediaTreeVersion:
    version_id: str = ""
    preset_id: str = ""
    original_name: str = ""
    archive_path: str = ""
    sha256: str = ""
    size: int = 0
    created_at: str = ""
    source_tree_path: str = ""
    snapshot_id: str = ""
    plan_id: str = ""
    diff_id: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)
    path_validation: Dict[str, Any] = field(default_factory=dict)
    input_type: str = "directory_tree"  # directory_tree / local_scan / openlist
    remote_locator: str = ""  # OpenList 远端选中目录（仅 openlist 来源）
    provider_id: str = ""  # 内容提供商（pan115/baidu/quark/other/local）
    ingest_method: str = ""  # openlist_api / directory_tree / local_scan
    source_route_id: str = ""  # 使用的 OpenList 提供商路由，可为空


@dataclass
class MediaLibraryPreset:
    preset_id: str = ""
    name: str = ""
    source: str = ""
    source_root: str = ""
    import_family: str = ""
    import_scope: str = ""
    update_mode: str = "directory_tree"
    remote_locator: str = ""  # OpenList 远端选中目录（独立于本地 source_root）
    provider_id: str = ""  # 内容提供商（pan115/baidu/quark/other/local）
    ingest_method: str = ""  # openlist_api / directory_tree / local_scan
    source_route_id: str = ""  # 使用的 OpenList 提供商路由，可为空
    catalog_root_id: str = ""  # 关联的 Source Catalog source_root（OpenList 来源卡的权威关联）
    created_at: str = ""
    updated_at: str = ""
    current_snapshot_id: str = ""
    current_plan_id: str = ""
    current_version_id: str = ""
    version_count: int = 0
    work_count: int = 0
    video_count: int = 0
    lifecycle_status: str = "draft"
    versions: List[MediaTreeVersion] = field(default_factory=list)
