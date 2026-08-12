"""导入卡片及其目录树比对归档的删除预览与执行。"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

from app.core.atomic_json import write_json_atomic
from app.core.paths import get_data_dir
from app.media_presets.store import delete_preset, get_preset, get_presets_root


@dataclass
class PresetDeletePreview:
    preview_id: str = ""
    preset_id: str = ""
    preset_name: str = ""
    preset_updated_at: str = ""
    source: str = ""
    archive_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    preserved_generated_media: bool = True
    preserved_library_data: bool = True

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["archive_version_count"] = len(self.archive_files)
        return payload


def _preview_dir() -> Path:
    path = get_presets_root() / "delete_previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _new_preview_id(preset_id: str, updated_at: str) -> str:
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    value = f"card-archive:{preset_id}:{updated_at}:{now}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _save_preview(preview: PresetDeletePreview) -> None:
    write_json_atomic(_preview_dir() / f"{preview.preview_id}.json", asdict(preview))


def _load_preview(preview_id: str) -> PresetDeletePreview | None:
    path = _preview_dir() / f"{preview_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PresetDeletePreview(**payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _archive_files_for_preset(preset) -> tuple[list[str], list[str]]:
    controlled_root = (get_presets_root() / preset.preset_id).resolve(strict=False)
    files: list[str] = []
    warnings: list[str] = []
    for version in preset.versions:
        if not version.archive_path:
            continue
        path = (get_data_dir() / version.archive_path).resolve(strict=False)
        try:
            path.relative_to(controlled_root)
        except ValueError:
            warnings.append(f"目录树归档越出卡片受控目录，已阻止删除：{path}")
            continue
        if path.is_file():
            files.append(str(path))
    return sorted(set(files)), warnings


def build_preset_delete_preview(preset_id: str) -> PresetDeletePreview:
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="导入卡片不存在")
    archive_files, warnings = _archive_files_for_preset(preset)
    preview = PresetDeletePreview(
        preview_id=_new_preview_id(preset_id, preset.updated_at),
        preset_id=preset_id,
        preset_name=preset.name,
        preset_updated_at=preset.updated_at,
        source=preset.source,
        archive_files=archive_files,
        warnings=warnings,
        blocked=bool(warnings),
    )
    _save_preview(preview)
    return preview


def execute_preset_delete(preset_id: str, preview_id: str) -> dict:
    preview = _load_preview(preview_id)
    if preview is None or preview.preset_id != preset_id:
        raise HTTPException(status_code=404, detail="删除预览不存在或不属于当前导入卡片")
    if preview.blocked:
        raise HTTPException(status_code=409, detail="目录树归档未通过路径安全检查")
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="导入卡片不存在")
    if preset.updated_at != preview.preset_updated_at:
        raise HTTPException(status_code=409, detail="导入卡片已更新，请重新生成删除预览")

    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    try:
        maintenance = manager.maintenance("删除导入卡片")
        with maintenance:
            return _execute_preset_delete(preview)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"{exc}，请等待任务完成后重试") from exc


def _execute_preset_delete(preview: PresetDeletePreview) -> dict:
    archive_failure = _remove_preset_archive_root(preview.preset_id)
    if archive_failure:
        return _delete_result(preview, deleted_preset=False, failures=[archive_failure])

    deleted_record = delete_preset(preview.preset_id, delete_archives=False)
    try:
        (_preview_dir() / f"{preview.preview_id}.json").unlink(missing_ok=True)
    except OSError:
        pass
    return _delete_result(
        preview,
        deleted_preset=deleted_record,
        deleted_archive_count=len(preview.archive_files),
    )


def _remove_preset_archive_root(preset_id: str) -> dict[str, str] | None:
    presets_root = get_presets_root().resolve(strict=False)
    archive_root = (presets_root / preset_id).resolve(strict=False)
    try:
        archive_root.relative_to(presets_root)
    except ValueError:
        return {"path": str(archive_root), "reason": "卡片归档目录越出受控范围"}
    if archive_root == presets_root:
        return {"path": str(archive_root), "reason": "拒绝删除卡片归档根目录"}
    try:
        if archive_root.exists():
            shutil.rmtree(archive_root)
        return None
    except (OSError, PermissionError) as exc:
        return {"path": str(archive_root), "reason": str(exc)}


def _delete_result(
    preview: PresetDeletePreview,
    *,
    deleted_preset: bool,
    deleted_archive_count: int = 0,
    failures: list[dict[str, str]] | None = None,
) -> dict:
    failed = failures or []
    return {
        "preview_id": preview.preview_id,
        "status": "partial_failed" if failed else "succeeded",
        "deleted_preset": deleted_preset,
        "deleted_archive_count": deleted_archive_count,
        "failed": failed,
        "preserved_generated_media": True,
        "preserved_library_data": True,
    }
