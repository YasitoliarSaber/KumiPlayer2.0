# -*- coding: utf-8 -*-
"""路径清洗与安全文件名工具

公共路径入口：项目根、数据目录、镜像目录、缓存目录。
所有模块应通过这些函数获取路径，不再各自推导。
"""

import os
from pathlib import Path
from typing import Optional

from app.core.runtime import get_default_data_dir

# Windows 非法字符
_ILLEGAL_CHARS = set('\\/:*?"<>|')

# 控制字符
_CONTROL_CHARS = set(chr(i) for i in range(32))


def sanitize_filename(name: str) -> str:
    """清洗文件名，过滤非法字符

    规则：
    - 过滤 Windows 非法字符：\\ / : * ? " < > |
    - 过滤控制字符（ASCII 0-31）
    - 去除首尾空格和点号
    - 空字符串返回 "unnamed"
    """
    if not name:
        return "unnamed"
    cleaned = "".join(
        "_" if c in _ILLEGAL_CHARS or c in _CONTROL_CHARS else c
        for c in name
    )
    cleaned = cleaned.strip(" .")
    return cleaned or "unnamed"


def reject_path_traversal(path: str) -> str:
    """检查路径是否包含遍历攻击

    返回清洗后的路径。
    如果检测到路径遍历，抛出 ValueError。
    """
    if not path:
        return ""

    # 检查 .. 组件
    parts = Path(path).parts
    for part in parts:
        if part == "..":
            raise ValueError(f"路径包含遍历组件: {path}")

    # 检查绝对路径注入
    if path.startswith("/") or (len(path) >= 2 and path[1] == ":"):
        raise ValueError(f"路径包含绝对路径: {path}")

    return path


def safe_join(base: Path, *parts: str) -> Path:
    """安全拼接路径

    拼接后验证结果路径仍在 base 下。
    如果越界，抛出 ValueError。
    """
    result = base
    for part in parts:
        cleaned = reject_path_traversal(part)
        result = result / cleaned

    # 验证结果路径在 base 下
    try:
        result.resolve().relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"路径越界: {result} 不在 {base} 下")

    return result


# ============================================================
# 公共路径入口
# ============================================================

def get_project_root() -> Path:
    """获取项目根目录（backend 的上一级）"""
    return Path(__file__).parent.parent.parent.parent


def get_data_dir() -> Path:
    """获取数据目录；源码版为项目 data，安装版为当前用户 LocalAppData。"""
    override = os.environ.get("KUMIPLAYER_DATA_DIR")
    data_dir = Path(override).expanduser() if override else get_default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_mirror_root(mirror_root: Optional[str] = None) -> Path:
    """获取镜像根目录

    优先级：
    1. 显式传入的 mirror_root 参数
    2. config.mirror_dir
    3. data/mirror（默认）
    """
    if mirror_root:
        return Path(mirror_root)
    # 延迟导入避免循环引用
    from app.core.config import load_config
    config = load_config()
    if config.mirror_dir:
        return Path(config.mirror_dir)
    return get_data_dir() / "mirror"


def get_cache_dir() -> Path:
    """获取缓存目录（data/cache），不存在则创建"""
    cache_dir = get_data_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def configured_mount_source(path: str | Path) -> str:
    """Return the configured cloud source containing path, otherwise an empty string."""
    from app.core.config import load_config

    candidate = os.path.normcase(os.path.abspath(str(path)))
    config = load_config()
    for source, root in (
        ("pan115", config.pan115_root),
        ("baidu", config.baidu_root),
        ("openlist", config.openlist_mount_root),
    ):
        if not root:
            continue
        mount_root = os.path.normcase(os.path.abspath(root))
        try:
            if os.path.commonpath((candidate, mount_root)) == mount_root:
                return source
        except ValueError:
            continue
    return ""
