# -*- coding: utf-8 -*-
"""来源注册表测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_get_pan115_adapter():
    """get_source_adapter("pan115")"""
    from app.sources.registry import get_source_adapter, reset_registry
    reset_registry()
    adapter = get_source_adapter("pan115")
    assert adapter.source_id == "pan115"
    reset_registry()


def test_get_baidu_adapter():
    """get_source_adapter("baidu")"""
    from app.sources.registry import get_source_adapter, reset_registry
    reset_registry()
    adapter = get_source_adapter("baidu")
    assert adapter.source_id == "baidu"
    reset_registry()


def test_get_local_adapter():
    """get_source_adapter("local")"""
    from app.sources.registry import get_source_adapter, reset_registry
    reset_registry()
    adapter = get_source_adapter("local")
    assert adapter.source_id == "local"
    reset_registry()


def test_unknown_source_raises():
    """未知 source 抛 ValueError"""
    from app.sources.registry import get_source_adapter, reset_registry
    reset_registry()
    try:
        get_source_adapter("unknown")
        assert False, "应该抛出 ValueError"
    except ValueError as e:
        assert "未知来源" in str(e)
    finally:
        reset_registry()


def test_get_source_root_from_config():
    """get_source_root 读取配置"""
    from app.sources.registry import get_source_root
    from app.core.config import AppConfig
    import app.core.config as cfg

    old = cfg._cached_config
    cfg._cached_config = AppConfig(pan115_root="H:\\115open")

    root = get_source_root("pan115")
    assert root == "H:\\115open"

    cfg._cached_config = old


def test_explicit_root_priority():
    """显式 source_root 优先"""
    from app.sources.registry import get_source_root
    root = get_source_root("pan115", explicit_root="X:\\custom")
    assert root == "X:\\custom"


def test_list_sources():
    """list_sources 返回全部可用来源（含 OpenList 试点）"""
    from app.sources.registry import list_sources
    sources = list_sources()
    assert len(sources) == 4
    source_ids = {s["source"] for s in sources}
    assert source_ids == {"pan115", "baidu", "local", "openlist"}
    openlist = next(s for s in sources if s["source"] == "openlist")
    assert openlist["mirror_namespace"] == "openlist"
    assert openlist["available"] is False  # 未配置时不可用


def test_sources_package_exports():
    """sources 包导出三个适配器"""
    from app.sources import BaiduAdapter, LocalScanner, Pan115Adapter
    assert Pan115Adapter().source_id == "pan115"
    assert BaiduAdapter().source_id == "baidu"
    assert LocalScanner().source_id == "local"


if __name__ == "__main__":
    tests = [
        test_get_pan115_adapter,
        test_get_baidu_adapter,
        test_get_local_adapter,
        test_unknown_source_raises,
        test_get_source_root_from_config,
        test_explicit_root_priority,
        test_list_sources,
        test_sources_package_exports,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
