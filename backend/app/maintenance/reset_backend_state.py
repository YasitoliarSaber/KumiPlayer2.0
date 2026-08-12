"""KumiPlayer 管理数据一次性重置服务（后端数据流 V2 切换）。

- 默认只预览（preview）：列出将被删除的 KumiPlayer 受管数据与镜像目录；
- ``apply=True`` 时执行删除；执行前再次验证每个目标都不是来源根、
  磁盘根或用户主目录；
- 真实媒体、本地来源目录、网盘文件和 OpenList 远端内容是绝对只读边界，
  永远不允许作为删除目标。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import load_config
from app.core.paths import get_data_dir, get_mirror_root

#: 数据目录下由 KumiPlayer 管理的子目录/文件（可整体重建，不含用户数据）
_MANAGED_DATA_ENTRIES = (
    "import_plans",
    "media_presets",
    "raw_snapshots",
    "openlist_cache",
    "openlist_manifests",
    "library",
    "scrape",
    "playback",
    "user_overrides.json",
    "kumiplayer.db",
    "audits",
)


class ResetProtectionError(Exception):
    """目标路径越出受管范围。"""


def _configured_source_roots() -> list[Path]:
    """配置中的来源根（绝对只读边界）。"""
    config = load_config()
    roots = [
        config.pan115_root,
        config.baidu_root,
        config.local_root,
        getattr(config, "openlist_mount_root", "") or "",
    ]
    return [Path(root).expanduser() for root in roots if root.strip()]


def _is_unsafe_target(path: Path) -> bool:
    """目标是否为磁盘根、用户主目录或来源根（禁止删除）。"""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    if resolved == resolved.anchor or resolved.parent == resolved:
        return True
    home = Path.home().resolve()
    if resolved == home or home in resolved.parents:
        return True
    for root in _configured_source_roots():
        try:
            if resolved == root.resolve() or root.resolve() in resolved.parents:
                return True
        except OSError:
            continue
    return False


def preview_reset() -> dict:
    """预览重置影响范围（不删除任何东西）。"""
    data_dir = get_data_dir().resolve()
    mirror_root = get_mirror_root().resolve()

    targets: list[str] = []
    for entry in _MANAGED_DATA_ENTRIES:
        path = data_dir / entry
        if path.exists():
            targets.append(str(path))
    if mirror_root != data_dir and mirror_root.exists():
        targets.append(str(mirror_root))

    return {
        "data_dir": str(data_dir),
        "mirror_root": str(mirror_root),
        "targets": targets,
        "source_roots_protected": [str(root) for root in _configured_source_roots()],
    }


def apply_reset() -> dict:
    """执行重置：只删除受管数据与受管镜像目录；任何越界目标立即中止。"""
    import shutil

    data_dir = get_data_dir().resolve()
    mirror_root = get_mirror_root().resolve()

    if _is_unsafe_target(data_dir):
        raise ResetProtectionError(f"数据根越出受管范围，拒绝重置: {data_dir}")
    if _is_unsafe_target(mirror_root):
        raise ResetProtectionError(f"镜像根越出受管范围，拒绝重置: {mirror_root}")

    removed: list[str] = []
    for entry in _MANAGED_DATA_ENTRIES:
        path = data_dir / entry
        if not path.exists():
            continue
        if _is_unsafe_target(path):
            raise ResetProtectionError(f"受管条目越出受管范围，拒绝删除: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        removed.append(str(path))

    if mirror_root != data_dir and mirror_root.exists():
        if _is_unsafe_target(mirror_root):
            raise ResetProtectionError(f"镜像根越出受管范围，拒绝删除: {mirror_root}")
        shutil.rmtree(mirror_root)
        removed.append(str(mirror_root))

    return {"removed": removed, "data_dir": str(data_dir), "mirror_root": str(mirror_root)}
