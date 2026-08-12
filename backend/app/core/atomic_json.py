# -*- coding: utf-8 -*-
"""小型 JSON 状态文件的原子写入工具。"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.core.data_lock import DATA_WRITE_LOCK

#: 原子替换重试：Windows 杀软/索引服务会瞬时锁定文件，单次 os.replace
#: 会偶发 PermissionError（WinError 5），重试后通常成功。
_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY = 0.05  # 秒


def _replace_with_retry(temp_name: str, path: Path) -> None:
    """os.replace 带有限重试，处理 Windows 瞬时文件锁。"""
    last_error: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(temp_name, path)
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 < _REPLACE_RETRIES:
                time.sleep(_REPLACE_RETRY_DELAY)
    raise last_error


def write_json_atomic(path: Path, data: Any) -> None:
    """先写同目录临时文件，再原子替换目标文件（替换带有限重试）。"""
    with DATA_WRITE_LOCK:
        _write_json_atomic_unlocked(path, data)


def _write_json_atomic_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_text_atomic(path: Path, text: str) -> None:
    """以 UTF-8、LF 和同目录原子替换写入普通文本（替换带有限重试）。"""
    with DATA_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
