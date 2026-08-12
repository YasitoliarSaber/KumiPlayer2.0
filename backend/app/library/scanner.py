# -*- coding: utf-8 -*-
"""Mirror 目录扫描器

扫描 mirror 目录，收集 .strm 和本地 asset。
不解析文件名中的作品/季/集，不根据目录名猜媒体结构。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# 固定 asset 文件名
_ASSET_KINDS = {
    "tvshow.nfo": "tvshow_nfo",
    "movie.nfo": "movie_nfo",
    "poster.jpg": "poster",
    "poster.png": "poster",
    "poster.webp": "poster",
    "fanart.jpg": "fanart",
    "fanart.png": "fanart",
    "fanart.webp": "fanart",
    "clearlogo.png": "clearlogo",
    "clearlogo.svg": "clearlogo",
    "clearlogo.webp": "clearlogo",
}


@dataclass
class MirrorFile:
    """扫描到的 .strm 文件"""
    source: str = ""
    namespace: str = ""
    strm_path: str = ""
    relative_strm_path: str = ""
    real_path: str = ""
    exists: bool = True
    size: int = 0
    mtime: float = 0.0


@dataclass
class MirrorAsset:
    """扫描到的本地 asset"""
    path: str = ""
    kind: str = ""  # tvshow_nfo / movie_nfo / poster / fanart / clearlogo
    exists: bool = True


@dataclass
class MirrorScanResult:
    """扫描结果"""
    source: str = ""
    mirror_root: str = ""
    scanned_at: str = ""
    strm_files: List[MirrorFile] = field(default_factory=list)
    assets: List[MirrorAsset] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# 来源 → 命名空间映射
_NAMESPACE_MAP = {
    "pan115": "115",
    "baidu": "baidu",
    "local": "local",
    "openlist": "openlist",
}


def _get_mirror_root() -> Path:
    from app.core.paths import get_mirror_root
    return get_mirror_root()


def scan_mirror(
    source: Optional[str] = None,
    mirror_root: Optional[str] = None,
) -> MirrorScanResult:
    """扫描 mirror 目录

    参数:
        source: 指定来源扫描，None 则扫描全部
        mirror_root: mirror 根目录，None 则从配置读取
    """
    from datetime import datetime, timezone, timedelta

    root = Path(mirror_root) if mirror_root else _get_mirror_root()
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()

    result = MirrorScanResult(
        source=source or "all",
        mirror_root=str(root),
        scanned_at=now,
    )

    if not root.exists():
        result.warnings.append(f"mirror 目录不存在: {root}")
        return result

    # 确定要扫描的命名空间
    if source:
        ns = _NAMESPACE_MAP.get(source, source)
        namespaces = [ns]
    else:
        # 扫描所有命名空间
        namespaces = [d.name for d in root.iterdir() if d.is_dir()]

    for ns in namespaces:
        ns_dir = root / ns
        if not ns_dir.exists():
            continue

        # 推断 source
        ns_source = source or _reverse_namespace(ns)

        # 扫描所有文件
        for path in ns_dir.rglob("*"):
            if not path.is_file():
                continue

            name_lower = path.name.lower()

            # .strm 文件
            if name_lower.endswith(".strm"):
                real_path = ""
                try:
                    real_path = path.read_text(encoding="utf-8").strip()
                except (IOError, UnicodeDecodeError):
                    result.warnings.append(f"无法读取 .strm: {path}")

                rel = str(path.relative_to(root))
                mf = MirrorFile(
                    source=ns_source,
                    namespace=ns,
                    strm_path=str(path),
                    relative_strm_path=rel,
                    real_path=real_path,
                    exists=True,
                    size=path.stat().st_size,
                    mtime=path.stat().st_mtime,
                )
                result.strm_files.append(mf)

            # asset 文件
            elif name_lower in _ASSET_KINDS:
                ma = MirrorAsset(
                    path=str(path),
                    kind=_ASSET_KINDS[name_lower],
                    exists=True,
                )
                result.assets.append(ma)

    return result


def _reverse_namespace(ns: str) -> str:
    """命名空间反推 source"""
    for source, namespace in _NAMESPACE_MAP.items():
        if namespace == ns:
            return source
    return ns
