# -*- coding: utf-8 -*-
"""来源适配器注册表

统一管理所有来源适配器。
提供 get_source_adapter / list_sources / get_source_root。
"""

from typing import Dict, List

from app.sources.base import SourceAdapter


# 适配器注册表（延迟初始化）
_adapters: Dict[str, SourceAdapter] = {}


def _get_or_create(source: str) -> SourceAdapter:
    """获取或创建适配器"""
    if source in _adapters:
        return _adapters[source]

    adapter: SourceAdapter
    if source == "pan115":
        from app.sources.pan115 import Pan115Adapter
        adapter = Pan115Adapter()
    elif source == "baidu":
        from app.sources.baidu import BaiduAdapter
        adapter = BaiduAdapter()
    elif source == "local":
        from app.sources.local import LocalScanner
        adapter = LocalScanner()
    elif source == "openlist":
        from app.sources.openlist import OpenListAdapter
        adapter = OpenListAdapter()
    else:
        raise ValueError(f"未知来源: {source}")

    _adapters[source] = adapter
    return adapter


def get_source_adapter(source: str) -> SourceAdapter:
    """获取来源适配器

    参数:
        source: 来源标识（pan115 / baidu / local）

    返回:
        SourceAdapter 实例

    异常:
        ValueError: 未知来源
    """
    return _get_or_create(source)


def list_sources() -> List[Dict[str, object]]:
    """列出所有可用来源"""
    sources: List[Dict[str, object]] = [
        {"source": "pan115", "mirror_namespace": "115"},
        {"source": "baidu", "mirror_namespace": "baidu"},
        {"source": "local", "mirror_namespace": "local"},
        {"source": "openlist", "mirror_namespace": "openlist"},
    ]

    # 检查配置中的可用性
    from app.core.config import load_config
    config = load_config()

    for s in sources:
        src = str(s["source"])
        if src == "pan115":
            s["available"] = bool(config.pan115_root)
        elif src == "baidu":
            s["available"] = bool(config.baidu_root)
        elif src == "local":
            s["available"] = bool(config.local_root)
        elif src == "openlist":
            s["available"] = bool(config.openlist_server_url and config.openlist_mount_root)
        else:
            s["available"] = False

    return sources


def get_source_root(source: str, explicit_root: str = "") -> str:
    """获取来源根目录

    优先使用显式传入的 explicit_root，否则从配置读取。

    参数:
        source: 来源标识
        explicit_root: 显式指定的根目录

    返回:
        来源根目录路径
    """
    if explicit_root:
        return explicit_root

    from app.core.config import load_config
    config = load_config()

    if source == "pan115":
        return config.pan115_root
    elif source == "baidu":
        return config.baidu_root
    elif source == "local":
        return config.local_root
    elif source == "openlist":
        # OpenList 的来源根是「本地挂载根 + 选中远端目录相对映射根的相对路径」，
        # 由 api/openlist 在导入时计算；这里返回配置的本地挂载根作为兜底。
        return config.openlist_mount_root
    else:
        raise ValueError(f"未知来源: {source}")


def reset_registry() -> None:
    """重置注册表（测试用）"""
    global _adapters
    _adapters = {}
