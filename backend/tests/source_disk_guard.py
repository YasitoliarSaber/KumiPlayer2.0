"""源盘 I/O 哨兵（TXT 离线链路共用）。

按给定虚构源根前缀拦截文件系统访问，统一覆盖：
- ``Path.is_file/is_dir/exists/open/stat/glob/rglob``
- ``os.scandir``
- ``os.path.exists/isdir/isfile``

只拦源根前缀内的调用；TXT 输入、临时 SQLite 与镜像输出不在源根内，
不受影响。
"""

from __future__ import annotations

import os
from pathlib import Path


def guard_source_disk_io(monkeypatch, roots) -> None:
    """源根路径限定的 I/O 哨兵：命中任何 stat/open/枚举/解析即失败。"""

    prefixes = tuple(
        str(Path(root).expanduser()).replace("/", "\\").rstrip("\\").casefold()
        for root in roots
    )
    sentinel = AssertionError("TXT 离线链路不得访问源盘")

    def _hit(target) -> bool:
        try:
            value = str(target).replace("/", "\\").rstrip("\\").casefold()
        except (TypeError, ValueError):
            return False
        return any(value == prefix or value.startswith(prefix + "\\") for prefix in prefixes)

    original_is_file = Path.is_file
    original_is_dir = Path.is_dir
    original_exists = Path.exists
    original_open = Path.open
    original_stat = Path.stat
    original_glob = Path.glob
    original_rglob = Path.rglob
    original_os_scandir = os.scandir
    original_os_exists = os.path.exists
    original_os_isdir = os.path.isdir
    original_os_isfile = os.path.isfile

    def guarded_is_file(self, *args, **kwargs):
        if _hit(self):
            raise sentinel
        return original_is_file(self, *args, **kwargs)

    def guarded_is_dir(self, *args, **kwargs):
        if _hit(self):
            raise sentinel
        return original_is_dir(self, *args, **kwargs)

    def guarded_exists(self, *args, **kwargs):
        if _hit(self):
            raise sentinel
        return original_exists(self, *args, **kwargs)

    def guarded_open(self, *args, **kwargs):
        if _hit(self):
            raise sentinel
        return original_open(self, *args, **kwargs)

    def guarded_stat(self, *args, **kwargs):
        if _hit(self):
            raise sentinel
        return original_stat(self, *args, **kwargs)

    def guarded_glob(self, *args, **kwargs):
        if _hit(self):
            raise sentinel
        return original_glob(self, *args, **kwargs)

    def guarded_rglob(self, *args, **kwargs):
        if _hit(self):
            raise sentinel
        return original_rglob(self, *args, **kwargs)

    def guarded_scandir(path="."):
        if _hit(path):
            raise sentinel
        return original_os_scandir(path)

    def guarded_os_exists(path):
        if _hit(path):
            raise sentinel
        return original_os_exists(path)

    def guarded_os_isdir(path):
        if _hit(path):
            raise sentinel
        return original_os_isdir(path)

    def guarded_os_isfile(path):
        if _hit(path):
            raise sentinel
        return original_os_isfile(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
    monkeypatch.setattr(Path, "exists", guarded_exists)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "glob", guarded_glob)
    monkeypatch.setattr(Path, "rglob", guarded_rglob)
    monkeypatch.setattr(os, "scandir", guarded_scandir)
    monkeypatch.setattr(os.path, "exists", guarded_os_exists)
    monkeypatch.setattr(os.path, "isdir", guarded_os_isdir)
    monkeypatch.setattr(os.path, "isfile", guarded_os_isfile)
