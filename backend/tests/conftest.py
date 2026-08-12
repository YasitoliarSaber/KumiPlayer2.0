# -*- coding: utf-8 -*-
"""pytest 测试隔离。

项目运行时配置位于项目根 data/config.json，包含真实路径和本机 token。
测试期间把 app.core.config.CONFIG_FILE 指向测试专用配置，避免读取、删除或
覆盖真实配置文件。
"""

import os
import shutil
import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).parent.parent.parent
_REAL_DATA_DIR = (_PROJECT_ROOT / "data").resolve()
_COLLECTION_DATA_DIR = _PROJECT_ROOT / "backend" / ".pytest_runtime" / "data"
os.environ.setdefault("KUMIPLAYER_DATA_DIR", str(_COLLECTION_DATA_DIR))

for _path in (_PROJECT_ROOT, _PROJECT_ROOT / "backend"):
    _path_text = str(_path)
    if _path_text not in sys.path:
        sys.path.insert(0, _path_text)


def _is_real_data_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return resolved == _REAL_DATA_DIR or _REAL_DATA_DIR in resolved.parents


def _install_real_config_guard() -> None:
    """防止任何测试路径（含后台线程）写入真实 data/config.json。

    与 rmtree 守卫同理：即使 CONFIG_FILE monkeypatch 被撤销、或残留后台线程
    调用 load/save_config，写入真实配置文件也会被拒绝。
    """
    from app.core import config as core_config

    original_persist = core_config._persist_config_payload

    def guarded_persist(config) -> None:
        target = core_config.get_config_file()
        if _is_real_data_path(target):
            raise AssertionError(f"测试禁止写入真实配置文件: {target}")
        return original_persist(config)

    core_config._persist_config_payload = guarded_persist


_install_real_config_guard()


@pytest.fixture(autouse=True)
def isolate_runtime_data(tmp_path, monkeypatch, request):
    """Route runtime data/config to pytest temp paths and protect real data."""
    test_data_dir = tmp_path / "data"
    test_config_file = test_data_dir / "config.json"
    test_data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(test_data_dir))

    for attr in ("_DATA_DIR", "_TEST_DATA_DIR"):
        if hasattr(request.module, attr):
            monkeypatch.setattr(request.module, attr, test_data_dir, raising=False)
    if hasattr(request.module, "_TEST_DIR"):
        monkeypatch.setattr(request.module, "_TEST_DIR", test_data_dir / "_test_local", raising=False)

    original_rmtree = shutil.rmtree

    def guarded_rmtree(path, *args, **kwargs):
        target = Path(path)
        if _is_real_data_path(target):
            raise AssertionError(f"测试禁止删除真实运行数据目录: {target}")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", guarded_rmtree)

    try:
        from app.core import config as core_config

        monkeypatch.setattr(core_config, "CONFIG_FILE", test_config_file)
        test_config_file.unlink(missing_ok=True)
        core_config.invalidate_config_cache()
    except Exception:
        pass

    try:
        yield
    finally:
        try:
            from app.core import config as core_config

            core_config.invalidate_config_cache()
            test_config_file.unlink(missing_ok=True)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def isolate_path_config_alias(monkeypatch):
    """Keep modules that imported load_config indirectly on the isolated config."""
    try:
        from app.core import config as core_config
        from app.core import paths as core_paths

        if hasattr(core_paths, "load_config"):
            monkeypatch.setattr(core_paths, "load_config", core_config.load_config)
    except Exception:
        pass

    yield
