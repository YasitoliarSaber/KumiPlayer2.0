# -*- coding: utf-8 -*-
"""T02 公共路径入口和 TaskManager 单例测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core import config as config_module
from app.core.config import AppConfig
from app.core.paths import get_project_root, get_data_dir, get_mirror_root, get_cache_dir
from app.tasks.registry import get_task_manager, reset_task_manager


def test_get_project_root():
    """项目根目录存在且包含 backend 目录"""
    root = get_project_root()
    assert root.exists()
    assert (root / "backend").exists()


def test_get_data_dir():
    """数据目录存在"""
    data_dir = get_data_dir()
    assert data_dir.exists()
    assert data_dir.name == "data"


def test_get_mirror_root_default():
    """config.mirror_dir 为空时使用 data/mirror"""
    old_config = config_module._cached_config
    try:
        config_module._cached_config = AppConfig(mirror_dir="")
        root = get_mirror_root()
        assert root == get_data_dir() / "mirror"
    finally:
        config_module._cached_config = old_config


def test_get_mirror_root_from_config():
    """未显式传参时读取 config.mirror_dir"""
    old_config = config_module._cached_config
    try:
        config_module._cached_config = AppConfig(mirror_dir="D:\\mirror_from_config")
        root = get_mirror_root()
        assert root == Path("D:\\mirror_from_config")
    finally:
        config_module._cached_config = old_config


def test_get_mirror_root_explicit():
    """显式 mirror_root 参数优先"""
    root = get_mirror_root("C:\\tmp\\test_mirror")
    assert "test_mirror" in str(root)


def test_get_cache_dir():
    """缓存目录存在"""
    cache_dir = get_cache_dir()
    assert cache_dir.exists()
    assert cache_dir.name == "cache"


def test_task_manager_singleton():
    """mirror/scrape/library 获取的是同一个 TaskManager 实例"""
    reset_task_manager()
    m1 = get_task_manager()
    m2 = get_task_manager()
    assert m1 is m2
    reset_task_manager()


def test_reset_task_manager():
    """reset_task_manager 可清理状态"""
    reset_task_manager()
    m1 = get_task_manager()
    reset_task_manager()
    m2 = get_task_manager()
    assert m1 is not m2
    reset_task_manager()


if __name__ == "__main__":
    test_get_project_root()
    test_get_data_dir()
    test_get_mirror_root_default()
    test_get_mirror_root_from_config()
    test_get_mirror_root_explicit()
    test_get_cache_dir()
    test_task_manager_singleton()
    test_reset_task_manager()
    print("ALL PASSED")
