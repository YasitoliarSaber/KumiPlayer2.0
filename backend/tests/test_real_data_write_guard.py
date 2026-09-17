"""真实数据目录的写入保护：测试进程一律不许碰真实 data/。

事故背景（2026-09-17）：子代理在系统临时目录用绝对路径跑 pytest，不会加载
``backend/tests/conftest.py``，隔离夹具失效 → 测试请求打到真实 ``data/``，
覆盖了用户的 ``config.json``（OpenList 服务地址与内容路由丢失）。
本用例锁定新的进程级兜底。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.core.paths import assert_test_process_does_not_touch_real_data, get_project_root


def test_guard_is_active_inside_pytest():
    assert os.environ.get("PYTEST_CURRENT_TEST"), "本用例前提：确实运行在 pytest 进程内"


def test_real_data_paths_are_rejected():
    real_config = get_project_root() / "data" / "config.json"
    real_db = get_project_root() / "data" / "kumiplayer.db"

    with pytest.raises(RuntimeError, match="拒绝在测试进程中写入真实数据目录"):
        assert_test_process_does_not_touch_real_data(real_config)
    with pytest.raises(RuntimeError, match="拒绝在测试进程中写入真实数据目录"):
        assert_test_process_does_not_touch_real_data(real_db)


def test_temp_and_isolated_paths_are_allowed(tmp_path):
    # 隔离夹具用的临时目录必须照常放行，否则所有后端测试都会被打死。
    assert_test_process_does_not_touch_real_data(tmp_path / "config.json")
    assert_test_process_does_not_touch_real_data(Path("/tmp/whatever/kumiplayer.db"))


def test_isolated_data_dir_override_is_allowed(tmp_path):
    assert_test_process_does_not_touch_real_data(tmp_path / "data" / "kumiplayer.db")
