# -*- coding: utf-8 -*-
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import List, Optional, Tuple

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir
from app.media_presets.models import MediaLibraryPreset, MediaTreeVersion

_INDEX_LOCK = RLock()


def get_presets_root() -> Path:
    root = get_data_dir() / "media_presets"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _index_path() -> Path:
    return get_presets_root() / "index.json"


def _from_dict(data: dict) -> MediaLibraryPreset:
    versions = [MediaTreeVersion(**item) for item in data.pop("versions", [])]
    preset = MediaLibraryPreset(**data, versions=versions)
    return _apply_compat_fields(preset)


def _apply_compat_fields(preset: MediaLibraryPreset) -> MediaLibraryPreset:
    """旧记录读取时回填 provider / ingest 兼容值，不重写媒体库 ID。

    - source=pan115/baidu/local 映射到对应 provider 与导入方式；
    - source=openlist（旧夸克试点）兼容回填 provider=quark、ingest=openlist_api；
    - 新字段已存在时保持原值。
    """
    from app.integrations.openlist.providers import compat_ingest, compat_provider

    if not preset.provider_id:
        preset.provider_id = compat_provider(preset.source)
    if not preset.ingest_method:
        preset.ingest_method = compat_ingest(preset.source)
    return preset


def list_presets() -> List[MediaLibraryPreset]:
    with _INDEX_LOCK:
        path = _index_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [_from_dict(dict(item)) for item in payload.get("presets", [])]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []


def get_preset(preset_id: str) -> Optional[MediaLibraryPreset]:
    return next((item for item in list_presets() if item.preset_id == preset_id), None)


def save_preset(preset: MediaLibraryPreset) -> None:
    with DATA_WRITE_LOCK, _INDEX_LOCK:
        presets = list_presets()
        replaced = False
        for index, item in enumerate(presets):
            if item.preset_id == preset.preset_id:
                presets[index] = preset
                replaced = True
                break
        if not replaced:
            presets.append(preset)
        write_json_atomic(_index_path(), {"version": 1, "presets": [asdict(item) for item in presets]})


def delete_preset(preset_id: str, *, delete_archives: bool = True) -> bool:
    """删除预设索引和受控目录树归档，不触碰镜像、元数据或真实媒体。"""
    with DATA_WRITE_LOCK, _INDEX_LOCK:
        presets = list_presets()
        remaining = [item for item in presets if item.preset_id != preset_id]
        if len(remaining) == len(presets):
            return False
        presets_root = get_presets_root().resolve(strict=False)
        archive_root = (presets_root / preset_id).resolve(strict=False)
        try:
            archive_root.relative_to(presets_root)
        except ValueError as exc:
            raise ValueError("媒体库归档目录越出受控范围") from exc
        if archive_root == presets_root:
            raise ValueError("拒绝删除媒体库归档根目录")
        write_json_atomic(_index_path(), {"version": 1, "presets": [asdict(item) for item in remaining]})
        if delete_archives and archive_root.exists():
            shutil.rmtree(archive_root)
        return True


def list_presets_for_clear(source: str) -> List[MediaLibraryPreset]:
    """列出媒体库清理时应同步移除的目录树导入档案。"""
    return [
        item
        for item in list_presets()
        if source == "all" or item.source == source
    ]


def delete_presets_for_clear(
    source: str,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """批量删除清理范围内的卡片与受控版本归档。

    先一次性更新索引，确保界面不会残留已清理来源的卡片；随后只删除
    ``data/media_presets`` 下的受控归档，不触碰用户原始目录树文件。
    """
    with DATA_WRITE_LOCK, _INDEX_LOCK:
        presets = list_presets()
        selected = [
            item
            for item in presets
            if source == "all" or item.source == source
        ]
        if not selected:
            return [], []

        selected_ids = {item.preset_id for item in selected}
        remaining = [item for item in presets if item.preset_id not in selected_ids]
        write_json_atomic(_index_path(), {"version": 1, "presets": [asdict(item) for item in remaining]})

        failures: List[Tuple[str, str]] = []
        root = get_presets_root().resolve(strict=False)
        for item in selected:
            archive_root = (root / item.preset_id).resolve(strict=False)
            try:
                archive_root.relative_to(root)
            except ValueError:
                failures.append((str(archive_root), "媒体库归档目录越出受控范围"))
                continue
            if archive_root == root:
                failures.append((str(archive_root), "拒绝删除媒体库归档根目录"))
                continue
            if not archive_root.exists():
                continue
            try:
                shutil.rmtree(archive_root)
            except (OSError, PermissionError) as exc:
                failures.append((str(archive_root), str(exc)))
        return [item.preset_id for item in selected], failures


def version_archive_dir(preset_id: str) -> Path:
    path = get_presets_root() / preset_id / "versions"
    path.mkdir(parents=True, exist_ok=True)
    return path
