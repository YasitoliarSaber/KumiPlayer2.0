"""OpenList 目录清单读取（legacy 读路径）。

历史扫描清单由旧递归导入链写入 ``data/openlist_manifests/``；
模块4 切到 Source Catalog 后不再写新清单，但来源适配器
（``app.sources.openlist``）仍需读取历史清单转换为 RawSnapshot，
因此保留只读接口。

清单内容只包含白名单字段；直链、缩略图、哈希、存储内部路径不落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_FORMAT = "kumiplayer-openlist-manifest"
MANIFEST_FORMAT_VERSION = 1


def read_manifest(path: Path) -> dict:
    """读取清单；格式或字段非法时返回空 dict（不抛异常）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        return {}
    return data
