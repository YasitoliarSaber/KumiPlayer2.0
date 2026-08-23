"""V4 一次性媒体状态重置。

Only the explicit managed-state manifest is removable. Configuration, account
credentials, MPV state, logs and all source roots remain outside the manifest.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.core.config import load_config
from app.core.paths import get_data_dir, get_mirror_root

MANAGED_DATA_ENTRIES = (
    "kumiplayer.db",
    "kumiplayer.db-wal",
    "kumiplayer.db-shm",
    "import_plans",
    "raw_snapshots",
    "media_presets",
    "library",
    "library_snapshots",
    "mirror",
    "scrape",
    "playback",
    "user_assets",
    "openlist_cache",
    "openlist_incremental",
    "openlist_manifests",
    "cache",
    "tmp",
    "audits",
    "user_overrides.json",
)


class ResetProtectionError(RuntimeError):
    """拒绝删除数据根、来源根、用户目录或越界链接。"""


def _safe_resolve(path: Path) -> Path:
    """网络盘暂不可用时仍能完成安全路径比较，不触发真实挂载。"""

    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _configured_source_roots() -> list[Path]:
    config = load_config()
    values = (
        config.pan115_root,
        config.baidu_root,
        config.local_root,
        getattr(config, "openlist_mount_root", "") or "",
    )
    return [_safe_resolve(Path(value)) for value in values if str(value).strip()]


def _is_protected(path: Path) -> bool:
    resolved = _safe_resolve(path)
    if resolved.parent == resolved or resolved == _safe_resolve(Path.home()):
        return True
    return any(
        resolved == root or root in resolved.parents or resolved in root.parents
        for root in _configured_source_roots()
    )


def _validate_data_dir(data_dir: Path) -> Path:
    resolved = _safe_resolve(data_dir)
    if _is_protected(resolved):
        raise ResetProtectionError(f"数据目录受保护，拒绝重置: {resolved}")
    return resolved


def _validate_external_mirror(data_dir: Path, mirror_root: Path) -> Path | None:
    expanded = Path(os.path.abspath(os.fspath(mirror_root.expanduser())))
    if _is_link_or_junction(expanded):
        raise ResetProtectionError(f"镜像目录不能是符号链接或目录联接: {expanded}")
    resolved = _safe_resolve(mirror_root)
    if resolved == data_dir:
        return None
    if _is_protected(resolved):
        raise ResetProtectionError(f"镜像目录与来源目录重叠或属于受保护路径，拒绝重置: {resolved}")
    if resolved.parent == resolved:
        raise ResetProtectionError(f"镜像目录不能是磁盘根: {resolved}")
    return resolved


def _targets() -> tuple[Path, list[Path]]:
    data_dir = _validate_data_dir(get_data_dir())
    external_mirror = _validate_external_mirror(data_dir, get_mirror_root())
    managed: list[Path] = []
    seen: set[Path] = set()
    for name in MANAGED_DATA_ENTRIES:
        candidate = data_dir / name
        if candidate not in seen:
            managed.append(candidate)
            seen.add(candidate)
    if external_mirror is not None:
        if external_mirror not in seen:
            managed.append(external_mirror)
    return data_dir, managed


def preview_reset() -> dict:
    data_dir, candidates = _targets()
    targets = [path for path in candidates if path.exists() or path.is_symlink()]
    return {
        "data_dir": str(data_dir),
        "targets": [str(path) for path in targets],
        "managed_entries": list(MANAGED_DATA_ENTRIES),
        "source_roots_protected": [str(path) for path in _configured_source_roots()],
        "preserved_entries": [
            "config.json",
            "bangumi_account.json",
            "mpv-state",
            "skills",
            "logs",
            "_cleanup_backup_20260812",
        ],
    }


def apply_reset() -> dict:
    data_dir, candidates = _targets()
    removed: list[str] = []
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        if path.parent == data_dir and path.name not in MANAGED_DATA_ENTRIES:
            raise ResetProtectionError(f"目标不在 V4 白名单: {path}")
        if _is_link_or_junction(path):
            raise ResetProtectionError(f"符号链接或目录联接不允许作为重置目标: {path}")
        if _is_protected(path):
            raise ResetProtectionError(f"目标路径受保护，拒绝删除: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(str(path))
    return {"data_dir": str(data_dir), "removed": removed}
