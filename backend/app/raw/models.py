# -*- coding: utf-8 -*-
"""RawSnapshot / RawFile 数据模型

来源适配器的唯一标准输出。
每个文件都有 RawFile，RawFile 不判断作品、季、集。
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RawFile:
    """来源适配器输出的单个文件条目"""

    id: str = ""
    snapshot_id: str = ""
    source: str = ""  # pan115 / baidu / local
    source_root: str = ""  # 挂载根目录，如 H:\115open
    virtual_root: str = ""  # 分类层目录名，如 "动画"
    source_path_parts: List[str] = field(default_factory=list)  # 路径各层
    relative_path: str = ""  # 相对路径，如 动画/冰菓.2012/视频.mkv
    real_path: str = ""  # 真实播放路径
    name: str = ""  # 文件名或目录名
    stem: str = ""  # 不含扩展名的文件名
    ext: str = ""  # 扩展名，如 .mkv
    depth: int = 0  # 在目录树中的深度
    parent_path: str = ""  # 父目录相对路径
    is_file: bool = True  # True=文件, False=目录
    resource_hint: str = ""  # 资源类型提示（由来源适配器给出，不作为最终判断）
    size: Optional[int] = None  # 文件大小（本地来源可有）
    mtime: Optional[float] = None  # 修改时间（本地来源可有）
    content_fingerprint: str = ""  # 本地目录可用轻量摘要；网盘挂载扫描保持为空


@dataclass
class RawSnapshot:
    """来源适配器的一次完整解析结果"""

    snapshot_id: str = ""
    source: str = ""  # pan115 / baidu / local（兼容字段；真实提供商见 provider_id）
    provider_id: str = ""  # 内容提供商：pan115/baidu/quark/other/local
    ingest_method: str = ""  # openlist_api / directory_tree / local_scan
    source_route_id: str = ""  # 使用的 OpenList 提供商路由，可为空
    source_root: str = ""
    # 选中目录名（OpenList remote_locator 的 basename）：无分类层/作品目录层时
    # 作为系列名候选（如选中“飞跃巅峰 内封中字”，其下 S1/S2 季目录归同一系列）
    root_container: str = ""
    import_family: str = ""  # anime / live，本次导入由用户选择的大类
    import_scope: str = ""  # seasonal 表示从新番追更目录导入
    created_at: str = ""
    input_file: str = ""  # 输入文件路径（目录树 txt 或本地目录）
    file_count: int = 0
    video_count: int = 0
    files: List[RawFile] = field(default_factory=list)
