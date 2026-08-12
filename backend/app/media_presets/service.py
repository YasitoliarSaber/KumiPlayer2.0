import hashlib
import os
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, UploadFile

from app.core.paths import get_data_dir, sanitize_filename
from app.integrations.openlist.providers import compat_ingest, compat_provider
from app.import_plan.diff import compute_diff
from app.import_plan.incremental import build_incremental_plan, merge_incremental_plan
from app.import_plan.service import build_preview
from app.import_plan.store import load_import_plan, save_diff_result, save_import_plan
from app.media_presets.models import MediaLibraryPreset, MediaTreeVersion
from app.media_presets.store import get_presets_root, list_presets, save_preset, version_archive_dir
from app.raw.store import load_raw_snapshot, save_raw_snapshot

_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_ALLOWED_SUFFIXES = {".txt", ".tree", ".log"}
_PAN115_TREE_LINE = re.compile(r"^(?:\|——.+|(?:\| )*\|-.+)$", re.MULTILINE)
_BAIDU_TREE_LINE = re.compile(
    r"^(?:(?:│ {2,3})|(?: {3,4}))*[├└]─{1,2}\s?.+$",
    re.MULTILINE,
)


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def preset_to_dict(preset: MediaLibraryPreset) -> dict:
    data = asdict(preset)
    data["is_library_indexed"] = preset.lifecycle_status == "ready"
    return data


_LIFECYCLE_ORDER = {
    "draft": 0,
    "confirmed": 1,
    "mirrored": 2,
    "needs_attention": 3,
    "ready": 4,
}


def mark_preset_lifecycle(plan_id: str, status: str) -> MediaLibraryPreset | None:
    """按当前计划推进目录树档案状态，旧版本任务不能覆盖新版状态。"""
    if status not in _LIFECYCLE_ORDER:
        raise ValueError(f"不支持的媒体库状态: {status}")
    preset = next((item for item in list_presets() if item.current_plan_id == plan_id), None)
    if preset is None:
        return None
    current_rank = _LIFECYCLE_ORDER.get(preset.lifecycle_status, 0)
    target_rank = _LIFECYCLE_ORDER[status]
    if status == "needs_attention" or target_rank >= current_rank:
        preset.lifecycle_status = status
        preset.updated_at = now_iso()
        save_preset(preset)
    return preset


def _number_label(number: int) -> str:
    values = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
    return values.get(number, str(number))


def next_preset_name(import_family: str, import_scope: str) -> str:
    prefix = "新番" if import_scope == "seasonal" else "动画" if import_family == "anime" else "剧集"
    count = sum(
        1 for item in list_presets()
        if item.import_family == import_family and item.import_scope == import_scope
    )
    return f"{prefix}{_number_label(count + 1)}"


def _normalized_library_root(value: str) -> str:
    if not value:
        return ""
    normalized = os.path.abspath(os.path.expanduser(value))
    return os.path.normcase(os.path.normpath(normalized))


def find_matching_preset(
    source: str,
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    tree_name: str = "",
    sha256: str = "",
) -> MediaLibraryPreset | None:
    """按解析后的实际媒体根复用卡片，TXT 文件名不参与媒体库身份。"""
    expected_root = _normalized_library_root(source_root)
    candidates = [
        item
        for item in list_presets()
        if item.source == source
        and item.import_family == import_family
        and item.import_scope == import_scope
        and _normalized_library_root(item.source_root) == expected_root
    ]
    if sha256:
        exact = next((item for item in candidates if find_version_by_sha256(item, sha256)), None)
        if exact is not None:
            return exact
    return min(candidates, key=lambda item: (item.created_at or item.updated_at, item.preset_id)) if candidates else None


def _tree_name_anchor(value: str) -> str:
    stem = Path(value).stem.casefold().strip()
    stem = re.sub(r"(?:[_\-\s]*(?:文件目录|目录树))?[_\-\s]*\d{8,14}$", "", stem)
    return re.sub(r"[_\-\s]+", "", stem)


def find_version_by_sha256(preset: MediaLibraryPreset, sha256: str) -> MediaTreeVersion | None:
    return next((item for item in preset.versions if sha256 and item.sha256 == sha256), None)


def snapshots_have_same_media(left, right) -> bool:
    """比较解析后的视频集合，忽略 TXT 文件名、编码和换行差异。"""
    from app.recognition.resource_type import VIDEO_EXTS

    def signature(snapshot) -> tuple[tuple[str, int, str], ...]:
        return tuple(sorted(
            (
                os.path.normcase(os.path.normpath(item.relative_path or item.real_path)),
                int(item.size or 0),
                item.content_fingerprint or "",
            )
            for item in snapshot.files
            if item.is_file and item.ext.lower() in VIDEO_EXTS
        ))

    return bool(left and right) and signature(left) == signature(right)


def discard_candidate_snapshot(snapshot_id: str) -> None:
    """删除尚未激活到任何卡片的解析快照。"""
    if not snapshot_id:
        return
    path = get_data_dir() / "raw_snapshots" / f"{snapshot_id}.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def discard_archived_version(version: MediaTreeVersion) -> None:
    """丢弃尚未写入预设索引的临时归档，并清理空的临时预设目录。"""
    if version.archive_path:
        archive = get_data_dir() / version.archive_path
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            return
    temporary_root = get_presets_root() / version.preset_id
    try:
        versions_dir = temporary_root / "versions"
        if versions_dir.is_dir() and not any(versions_dir.iterdir()):
            versions_dir.rmdir()
        if temporary_root.is_dir() and not any(temporary_root.iterdir()):
            temporary_root.rmdir()
    except OSError:
        pass


def _discard_openlist_manifest(manifest_path: Path, *, if_unreferenced: bool = False) -> None:
    """清理本次扫描写入的 OpenList 清单。

    if_unreferenced=True 时仅在没有预设版本引用它的情况下删除，
    避免误删已被 blocked 版本记录引用的审计清单。
    """
    try:
        relative = manifest_path.relative_to(get_data_dir()).as_posix()
    except ValueError:
        return
    if if_unreferenced:
        referenced = any(
            version.archive_path == relative
            for preset in list_presets()
            for version in preset.versions
        )
        if referenced:
            return
    try:
        manifest_path.unlink(missing_ok=True)
    except OSError:
        pass


def move_archived_version(version: MediaTreeVersion, preset_id: str) -> Path:
    """把识别阶段的临时归档转入已存在媒体库的受控版本目录。"""
    archive = get_data_dir() / version.archive_path
    destination = version_archive_dir(preset_id) / archive.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(archive, destination)
    old_preset_id = version.preset_id
    version.preset_id = preset_id
    version.archive_path = destination.relative_to(get_data_dir()).as_posix()
    temporary_root = get_presets_root() / old_preset_id
    try:
        versions_dir = temporary_root / "versions"
        if versions_dir.is_dir() and not any(versions_dir.iterdir()):
            versions_dir.rmdir()
        if temporary_root.is_dir() and not any(temporary_root.iterdir()):
            temporary_root.rmdir()
    except OSError:
        pass
    return destination


def _validate_tree_bytes(data: bytes) -> None:
    if not data:
        raise HTTPException(status_code=400, detail="目录树文件为空")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="目录树文件超过 64 MB")


def _archive_tree_bytes(
    preset_id: str,
    original_name: str,
    data: bytes,
    source_tree_path: str = "",
    provider_id: str = "",
    ingest_method: str = "",
    source_route_id: str = "",
) -> tuple[MediaTreeVersion, Path]:
    version_id = uuid.uuid4().hex
    safe_name = sanitize_filename(original_name)
    archive = version_archive_dir(preset_id) / f"{version_id}-{safe_name}"
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, archive)
    relative = archive.relative_to(get_data_dir()).as_posix()
    version = MediaTreeVersion(
        version_id=version_id,
        preset_id=preset_id,
        original_name=original_name,
        archive_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        created_at=now_iso(),
        source_tree_path=source_tree_path,
        provider_id=provider_id,
        ingest_method=ingest_method,
        source_route_id=source_route_id,
    )
    return version, archive


async def archive_upload(preset_id: str, upload: UploadFile) -> tuple[MediaTreeVersion, Path]:
    original_name = Path(upload.filename or "目录树.txt").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 .txt、.tree 或 .log 目录树文件")
    data = await upload.read(_MAX_UPLOAD_BYTES + 1)
    _validate_tree_bytes(data)
    return _archive_tree_bytes(preset_id, original_name, data)


_MOUNT_FS_ERROR_HINT = (
    "目录树文件所在挂载盘不支持文件系统解析，请确认挂载在线后重试，"
    "或在本地磁盘选择该 TXT 副本"
)


def _is_mount_fs_error(exc: OSError) -> bool:
    """判断 OSError 是否来自挂载层拒绝文件系统解析。

    WinError 1005 = ERROR_UNRECOGNIZED_VOLUME：115 挂载盘（WinFSP）允许
    打开与读取文件，却不支持真实路径解析（等价 GetFinalPathNameByHandle）。
    这类错误不是“文件不存在”或“文件损坏”，需要与普通 OSError 区分。
    ``OSError(1005, ...)`` 手工构造时 errno=1005，真实 WinError 时
    winerror=1005，两个属性都判断。
    """
    return exc.errno == 1005 or getattr(exc, "winerror", None) == 1005


def _read_dropped_tree(path: Path) -> bytes:
    if path.suffix.lower() != ".txt":
        raise HTTPException(status_code=400, detail="拖放导入仅支持 .txt 目录树文件")
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="拖放目录树必须使用本机绝对路径")
    if not path.is_file():
        # is_file 会吞掉 OSError 并返回 False，先区分“路径不存在”
        # 与“路径存在但不是文件”，避免把挂载层问题误报成文件缺失。
        if not path.exists():
            raise HTTPException(status_code=400, detail="目录树文件不存在或路径错误")
        raise HTTPException(status_code=400, detail="拖放路径不是可读取文件")
    try:
        size = path.stat().st_size
    except OSError as exc:
        if _is_mount_fs_error(exc):
            raise HTTPException(status_code=400, detail=_MOUNT_FS_ERROR_HINT) from exc
        raise HTTPException(status_code=400, detail="无法读取拖放目录树文件") from exc
    if size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="目录树文件超过 64 MB")
    try:
        data = path.read_bytes()
    except OSError as exc:
        if _is_mount_fs_error(exc):
            raise HTTPException(status_code=400, detail=_MOUNT_FS_ERROR_HINT) from exc
        raise HTTPException(status_code=400, detail="无法读取拖放目录树文件") from exc
    _validate_tree_bytes(data)
    return data


def _decode_tree_candidates(data: bytes) -> list[str]:
    encodings = ["utf-8-sig", "gb18030"]
    if data[:2] in {b"\xff\xfe", b"\xfe\xff"}:
        encodings.insert(0, "utf-16")
    else:
        encodings.append("utf-16")

    candidates: list[str] = []
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if text not in candidates:
            candidates.append(text)
    return candidates


def _detect_tree_source_bytes(data: bytes) -> str:
    detected: set[str] = set()
    for text in _decode_tree_candidates(data):
        pan115_count = len(_PAN115_TREE_LINE.findall(text))
        baidu_count = len(_BAIDU_TREE_LINE.findall(text))
        if pan115_count and not baidu_count:
            detected.add("pan115")
        elif baidu_count and not pan115_count:
            detected.add("baidu")

    if len(detected) != 1:
        raise HTTPException(
            status_code=400,
            detail="无法识别目录树来源，请确认 TXT 是 115 或百度网盘导出的目录树",
        )
    return detected.pop()


def detect_tree_source(tree_path: Path | str) -> str:
    """根据目录树正文识别 115 或百度格式，不使用易误判的文件名。"""
    path = Path(tree_path).expanduser()
    return _detect_tree_source_bytes(_read_dropped_tree(path))


def archive_local_tree(
    preset_id: str,
    tree_path: str,
) -> tuple[str, MediaTreeVersion, Path, str]:
    """只读复制桌面拖入 / 选择器选中的 TXT，并返回自动识别的来源。

    新根路径合同：用户选择的 TXT 绝对路径是权威事实，source_root 就是它的父目录。
    返回 (source, version, archive, source_root)。

    挂载网盘（WinFSP / 115 官方挂载）可能不支持真实路径解析：
    ``resolve(strict=True)`` 会抛 WinError 1005。与 OpenList 链路一致，
    只验证可访问性，不强制解析符号链接。
    """
    path = Path(tree_path).expanduser()
    resolved = path.absolute()  # 不调用 resolve(strict=True)，挂载盘可能抛 WinError 1005
    data = _read_dropped_tree(resolved)
    source = _detect_tree_source_bytes(data)
    source_root = str(resolved.parent)
    version, archive = _archive_tree_bytes(
        preset_id,
        resolved.name,
        data,
        source_tree_path=str(resolved),
        provider_id=compat_provider(source),
        ingest_method=compat_ingest(source),
    )
    return source, version, archive, source_root


def parse_archived_tree(
    source: str,
    archive: Path,
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    build_plan: bool = True,
    tree_name: str = "",
    source_root_is_exact: bool = False,
):
    from app.api.sources import (
        _import_scope_for_baidu_tree_content,
        _import_scope_for_tree,
        _normalize_import_family,
        _normalize_import_scope,
        _resolve_final_import_scope,
        _resolve_tree_source_root,
    )
    from app.sources.path_validation import (
        resolve_baidu_snapshot_root,
        validate_snapshot_paths,
    )
    from app.sources.registry import get_source_adapter, get_source_root

    if source not in {"pan115", "baidu"}:
        raise HTTPException(status_code=400, detail="媒体库预设当前仅支持 115 或百度目录树")
    family = _normalize_import_family(import_family)
    manual_scope = _normalize_import_scope(import_scope, family)
    tree_hint = tree_name or str(archive)
    try:
        resolved_root = get_source_root(source, source_root)
        # 手动重新绑定时直接使用用户选择的精确根；常规导入则按原始文件名
        # 在配置根的直属子目录中解析作用域，例如 01动画 或新番。
        effective_root = (
            Path(resolved_root)
            if source_root_is_exact
            else _resolve_tree_source_root(tree_hint, resolved_root)
        )
        snapshot = get_source_adapter(source).parse(str(archive), str(effective_root))
        path_validation = (
            resolve_baidu_snapshot_root(snapshot, tree_hint, str(effective_root))
            if source == "baidu"
            else validate_snapshot_paths(snapshot)
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析失败: {exc}") from exc
    snapshot.import_family = family
    auto_scope = _import_scope_for_tree(tree_hint, Path(snapshot.source_root))
    if source == "baidu":
        auto_scope = _resolve_final_import_scope(auto_scope, _import_scope_for_baidu_tree_content(snapshot))
    snapshot.import_scope = _resolve_final_import_scope(manual_scope, auto_scope)
    # 预设版本在用户激活前只是候选快照，不应改变来源级增量基线。
    save_raw_snapshot(snapshot, update_latest=False)
    if not build_plan:
        return snapshot, None, path_validation
    plan = build_plan_for_snapshot(snapshot)
    return snapshot, plan, path_validation


def build_plan_for_snapshot(snapshot):
    from app.api.sources import _build_and_recognize_plan

    plan = _build_and_recognize_plan(snapshot)
    save_import_plan(plan, update_latest=False)
    return plan


def rebind_preset_source_root(preset: MediaLibraryPreset, source_root: str):
    """用用户选择的精确目录重新解析当前归档，不创建新版本或触碰真实媒体。"""
    if not source_root.strip():
        raise HTTPException(status_code=400, detail="请先选择实际视频文件夹")
    selected_root = Path(source_root).expanduser()
    if not selected_root.is_dir():
        raise HTTPException(status_code=400, detail="选择的实际视频文件夹不存在或不可访问")
    version = next(
        (item for item in preset.versions if item.version_id == preset.current_version_id),
        preset.versions[-1] if preset.versions else None,
    )
    if version is None or not version.archive_path:
        raise HTTPException(status_code=409, detail="当前媒体库缺少已归档目录树，无法重新验证")
    archive = get_data_dir() / version.archive_path
    if not archive.is_file():
        raise HTTPException(status_code=409, detail="当前媒体库的目录树归档不存在，请重新导入")

    snapshot, plan, path_validation = parse_archived_tree(
        preset.source,
        archive,
        str(selected_root),
        preset.import_family,
        preset.import_scope,
        tree_name=version.original_name,
        source_root_is_exact=True,
    )
    preview = build_preview(plan)
    version.snapshot_id = snapshot.snapshot_id
    version.plan_id = plan.plan_id
    version.summary = dict(preview.summary)
    version.path_validation = path_validation.to_dict()
    preset.source_root = snapshot.source_root
    preset.current_snapshot_id = snapshot.snapshot_id
    preset.current_plan_id = plan.plan_id
    preset.lifecycle_status = "draft"
    preset.updated_at = now_iso()
    save_preset(preset)
    return snapshot, plan, version, path_validation


def create_preset_record(
    source: str,
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    update_mode: str = "directory_tree",
    provider_id: str = "",
    ingest_method: str = "",
    source_route_id: str = "",
) -> MediaLibraryPreset:
    """创建媒体库预设；新记录显式写入 provider/ingest 元数据。

    未显式传入时按 source 兼容映射（115/百度目录树 -> directory_tree，
    local -> local_scan，openlist -> openlist_api + quark 回填）。
    """
    timestamp = now_iso()
    return MediaLibraryPreset(
        preset_id=uuid.uuid4().hex,
        name=next_preset_name(import_family, import_scope),
        source=source,
        source_root=source_root,
        import_family=import_family,
        import_scope=import_scope,
        update_mode=update_mode,
        provider_id=provider_id or compat_provider(source),
        ingest_method=ingest_method or compat_ingest(source),
        source_route_id=source_route_id or "",
        created_at=timestamp,
        updated_at=timestamp,
    )


def scan_local_preset(
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    preset: MediaLibraryPreset | None = None,
):
    """扫描本地目录，并以实际路径为媒体库身份维护增量版本。"""
    from app.api.sources import _normalize_import_family, _normalize_import_scope
    from app.sources.local import LocalScanner
    from app.sources.path_validation import validate_snapshot_paths

    family = _normalize_import_family(import_family)
    scope = _normalize_import_scope(import_scope, family)
    root = Path(source_root).expanduser()
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="本地目录不存在或不可访问") from exc
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="本地目录不存在或不可访问")

    existing = preset or next(
        (
            item for item in list_presets()
            if item.source == "local"
            and item.update_mode == "local_scan"
            and _normalized_library_root(item.source_root) == _normalized_library_root(str(root))
        ),
        None,
    )
    if existing is not None:
        if existing.source != "local" or existing.update_mode != "local_scan":
            raise HTTPException(status_code=409, detail="该媒体库不是本地扫描卡片")
        family = existing.import_family
        scope = existing.import_scope

    try:
        snapshot = LocalScanner().scan(
            str(root),
            source_root=str(root),
            logical_source="local",
            include_root=False,
            metadata_only=False,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"本地目录扫描失败: {exc}") from exc
    if snapshot.video_count <= 0:
        raise HTTPException(status_code=400, detail="所选本地目录中没有识别到视频文件")

    snapshot.import_family = family
    snapshot.import_scope = scope
    path_validation = validate_snapshot_paths(snapshot)
    if not path_validation.ok:
        raise HTTPException(status_code=400, detail=path_validation.message or "本地目录路径验证失败")
    save_raw_snapshot(snapshot, update_latest=False)

    version = MediaTreeVersion(
        version_id=uuid.uuid4().hex,
        original_name=f"{root.name or str(root)}（本地扫描）",
        created_at=now_iso(),
        path_validation=path_validation.to_dict(),
        provider_id=compat_provider("local"),
        ingest_method=compat_ingest("local"),
    )
    if existing is not None:
        current_snapshot = load_raw_snapshot(existing.current_snapshot_id)
        if snapshots_have_same_media(current_snapshot, snapshot):
            discard_candidate_snapshot(snapshot.snapshot_id)
            plan = load_import_plan(plan_id=existing.current_plan_id)
            if plan is None:
                raise HTTPException(status_code=409, detail="本地媒体库缺少当前导入计划，请重新导入")
            selected_version = next(
                (item for item in existing.versions if item.version_id == existing.current_version_id),
                existing.versions[-1],
            )
            return existing, selected_version, plan, None, True, True

        version.preset_id = existing.preset_id
        plan, diff = update_preset_from_tree(existing, version, snapshot)
        return existing, version, plan, diff, True, False

    plan = build_plan_for_snapshot(snapshot)
    plan.import_family = family
    plan.import_scope = scope
    save_import_plan(plan, update_latest=False)
    created = create_preset_record(
        "local",
        str(root),
        family,
        scope,
        update_mode="local_scan",
    )
    created.name = root.name or str(root)
    version.preset_id = created.preset_id
    activate_version(created, version, snapshot, plan)
    return created, version, plan, None, False, False


def create_preset_from_folder(
    source: str,
    source_root: str,
    import_family: str,
    import_scope: str,
):
    """从用户明确选择的挂载文件夹建立首个媒体库版本。

    复用追更扫描使用的 ``LocalScanner``，并强制 metadata-only：只枚举
    名称、路径、大小和修改时间，不打开视频、计算内容指纹或生成缩略图。
    """
    from app.api.sources import _normalize_import_family, _normalize_import_scope
    from app.sources.local import LocalScanner
    from app.sources.path_validation import validate_snapshot_paths

    if source != "baidu":
        raise HTTPException(status_code=400, detail="真实文件夹首导当前仅支持百度网盘新番")
    family = _normalize_import_family(import_family)
    scope = _normalize_import_scope(import_scope, family)
    if family != "anime" or scope != "seasonal":
        raise HTTPException(status_code=400, detail="真实文件夹首导仅用于动画新番（追更中）")

    root = Path(source_root).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="选择的新番真实文件夹不存在或不可访问")

    try:
        snapshot = LocalScanner().scan(
            str(root),
            source_root=str(root),
            logical_source=source,
            include_root=True,
            metadata_only=True,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"文件夹扫描失败: {exc}") from exc
    if snapshot.video_count <= 0:
        raise HTTPException(status_code=400, detail="所选文件夹中没有识别到视频文件")

    snapshot.import_family = family
    snapshot.import_scope = scope
    path_validation = validate_snapshot_paths(snapshot)
    if not path_validation.ok:
        raise HTTPException(status_code=400, detail=path_validation.message or "文件夹路径验证失败")

    save_raw_snapshot(snapshot, update_latest=False)
    version = MediaTreeVersion(
        version_id=uuid.uuid4().hex,
        original_name=f"{root.name}（文件夹扫描）",
        created_at=now_iso(),
        path_validation=path_validation.to_dict(),
        provider_id=compat_provider(source),
        ingest_method=compat_ingest(source),
    )
    existing = find_matching_preset(source, str(root), family, scope)
    if existing is not None:
        version.preset_id = existing.preset_id
        plan, _ = update_preset_from_tree(existing, version, snapshot)
        return existing, version, plan, True

    plan = build_plan_for_snapshot(snapshot)
    plan.import_family = family
    plan.import_scope = scope
    save_import_plan(plan, update_latest=False)
    preset = create_preset_record(source, str(root), family, scope)
    version.preset_id = preset.preset_id
    activate_version(preset, version, snapshot, plan)
    return preset, version, plan, False


def activate_version(preset: MediaLibraryPreset, version: MediaTreeVersion, snapshot, plan) -> None:
    preview = build_preview(plan)
    version.snapshot_id = snapshot.snapshot_id
    version.plan_id = plan.plan_id
    version.summary = dict(preview.summary)
    preset.current_snapshot_id = snapshot.snapshot_id
    preset.current_plan_id = plan.plan_id
    preset.current_version_id = version.version_id
    preset.version_count += 1
    preset.work_count = len(preview.groups)
    preset.video_count = int(preview.summary.get("video_count", snapshot.video_count))
    # 新目录树仍需重新确认、生成镜像和刮削，不能沿用旧版本的完成状态。
    preset.lifecycle_status = "draft"
    preset.updated_at = now_iso()
    preset.versions.append(version)
    save_preset(preset)


def update_preset_from_tree(
    preset: MediaLibraryPreset,
    version: MediaTreeVersion,
    snapshot,
    *,
    persist_on_block: bool = True,
):
    old_snapshot = load_raw_snapshot(preset.current_snapshot_id)
    base_plan = load_import_plan(plan_id=preset.current_plan_id)
    if old_snapshot is None or base_plan is None:
        raise HTTPException(status_code=409, detail="该媒体库缺少旧版基线，请重新创建预设")
    diff = compute_diff(old_snapshot, snapshot)
    save_diff_result(diff)
    version.snapshot_id = snapshot.snapshot_id
    version.diff_id = diff.diff_id
    # 纯新增对小型新番库会天然产生很高的“总量变化比例”，但没有删除风险。
    # 此时允许继续；只在存在缺失或不确定变更时执行安全阻断。
    unsafe_change = diff.safety.blocked and (diff.missing_count > 0 or diff.uncertain_count > 0)
    if unsafe_change:
        version.summary = {"blocked": True, "reasons": diff.safety.reasons}
        if persist_on_block:
            # 兼容既有调用方（create / scan / 旧 upload 更新）的历史行为。
            preset.versions.append(version)
            preset.version_count += 1
            preset.updated_at = now_iso()
            save_preset(preset)
        raise HTTPException(status_code=409, detail=f"更新被安全检查阻止：{'；'.join(diff.safety.reasons)}")

    delta = build_incremental_plan(
        diff,
        preset.source,
        snapshot.source_root,
        new_snapshot=snapshot,
        allow_blocked=diff.safety.blocked and not unsafe_change,
    )
    if delta is None:
        cumulative = base_plan
        cumulative.source_snapshot_id = snapshot.snapshot_id
    else:
        cumulative = merge_incremental_plan(base_plan, delta, diff, status="draft")
    cumulative.import_family = preset.import_family
    cumulative.import_scope = preset.import_scope
    save_import_plan(cumulative, update_latest=False)
    activate_version(preset, version, snapshot, cumulative)
    version.diff_id = diff.diff_id
    save_preset(preset)
    return cumulative, diff


def scan_openlist_preset(
    client,
    remote_locator: str,
    local_root: str,
    import_family: str,
    import_scope: str,
    *,
    preset: MediaLibraryPreset | None = None,
    provider_id: str = "",
    source_route_id: str = "",
    progress_callback=None,
    should_cancel=None,
):
    """扫描 OpenList 远端目录，以「本地映射根 + 远端定位」为媒体库身份维护增量版本。

    调用方负责构造已认证的 OpenListClient（本函数不读取配置与凭据）。
    ``provider_id`` / ``source_route_id`` 供新建预设时写入提供商元数据；
    已有预设（含旧夸克试点）保留其已回填的兼容值。
    取消、超限或失败时不产生预设、版本、快照残留；安全阻断保留的
    blocked 版本记录是既有目录树语义，其引用的清单不会被误删。
    """
    from app.api.sources import _normalize_import_family, _normalize_import_scope
    from app.integrations.openlist.manifest import canonical_sha256, write_manifest
    from app.integrations.openlist.scan import scan_remote_tree
    from app.sources.openlist import OpenListAdapter, make_snapshot_id
    from app.sources.path_validation import validate_snapshot_paths

    family = _normalize_import_family(import_family)
    scope = _normalize_import_scope(import_scope, family)
    root = Path(local_root).expanduser()
    try:
        # WebDAV / WinFSP 挂载有时能正常枚举和读取文件，却不支持
        # ``realpath``，会在 ``resolve(strict=True)`` 处抛出 WinError 1005。
        # OpenList 路径由受控的远端定位映射而来，因此只验证可访问性，
        # 不强制解析符号链接。
        if not root.is_dir():
            raise OSError("OpenList 本地挂载目录不存在或不可访问")
        root = root.absolute()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="OpenList 本地挂载目录不存在或不可访问") from exc

    existing = preset or next(
        (
            item for item in list_presets()
            if item.source == "openlist"
            and item.update_mode == "openlist_scan"
            and item.remote_locator == remote_locator
            and _normalized_library_root(item.source_root) == _normalized_library_root(str(root))
        ),
        None,
    )
    if existing is not None:
        family = existing.import_family
        scope = existing.import_scope

    if scope == "seasonal":
        # 首发范围：OpenList 自动追更尚未验证，禁止静默进入新番追更库。
        raise HTTPException(
            status_code=400,
            detail="OpenList 首发暂不支持自动追更，请使用「已完结」导入",
        )

    def phase(phase_name: str, progress: int, message: str) -> None:
        if progress_callback is None:
            return
        progress_callback(progress, message, {"phase": phase_name})

    # 1. 递归扫描：任何取消/超限都发生在写入之前，天然无残留
    entries = scan_remote_tree(
        client,
        remote_locator,
        should_cancel=should_cancel,
        progress_callback=progress_callback,
    )

    # 2. 原子写入 KumiPlayer 自有清单（不伪造 TXT），再构建 RawSnapshot
    phase("manifest_write", 92, "目录树读取完成，正在保存扫描清单")
    content_sha = canonical_sha256(entries)
    snapshot_id = make_snapshot_id(remote_locator, content_sha)
    manifest_path, _ = write_manifest(
        snapshot_id,
        entries,
        remote_locator=remote_locator,
        source_root=str(root),
        provider_id=provider_id or compat_provider("openlist"),
        ingest_method="openlist_api",
        source_route_id=source_route_id,
    )
    phase("local_validate", 95, "正在验证本地挂载文件")
    snapshot = OpenListAdapter().parse(str(manifest_path), str(root))

    try:
        if snapshot.video_count <= 0:
            raise HTTPException(status_code=400, detail="所选 OpenList 目录中没有识别到视频文件")
        snapshot.import_family = family
        snapshot.import_scope = scope
        path_validation = validate_snapshot_paths(snapshot)
        if not path_validation.ok:
            raise HTTPException(
                status_code=400,
                detail=path_validation.message or "OpenList 本地挂载路径验证失败",
            )
        save_raw_snapshot(snapshot, update_latest=False)

        version = MediaTreeVersion(
            version_id=uuid.uuid4().hex,
            original_name=f"{PurePosixPath(remote_locator).name or 'OpenList'}（OpenList）",
            created_at=now_iso(),
            sha256=content_sha,
            size=_openlist_manifest_size(manifest_path),
            source_tree_path=remote_locator,
            archive_path=manifest_path.relative_to(get_data_dir()).as_posix(),
            path_validation=path_validation.to_dict(),
            input_type="openlist",
            remote_locator=remote_locator,
            provider_id=provider_id or compat_provider("openlist"),
            ingest_method="openlist_api",
            source_route_id=source_route_id,
        )
        if existing is not None:
            current_snapshot = load_raw_snapshot(existing.current_snapshot_id)
            if snapshots_have_same_media(current_snapshot, snapshot):
                # 远端目录内容未变化：不新增版本，清理本次候选产物
                discard_candidate_snapshot(snapshot.snapshot_id)
                _discard_openlist_manifest(manifest_path)
                plan = load_import_plan(plan_id=existing.current_plan_id)
                if plan is None:
                    raise HTTPException(status_code=409, detail="OpenList 媒体库缺少当前导入计划，请重新导入")
                selected_version = next(
                    (item for item in existing.versions if item.version_id == existing.current_version_id),
                    existing.versions[-1],
                )
                return existing, selected_version, plan, None, True, True

            version.preset_id = existing.preset_id
            plan, diff = update_preset_from_tree(existing, version, snapshot)
            return existing, version, plan, diff, True, False

        phase("plan_build", 97, "正在识别作品并生成导入计划")
        plan = build_plan_for_snapshot(snapshot)
        plan.import_family = family
        plan.import_scope = scope
        save_import_plan(plan, update_latest=False)
        created = create_preset_record("openlist", str(root), family, scope, update_mode="openlist_scan")
        created.remote_locator = remote_locator
        created.name = PurePosixPath(remote_locator).name or "OpenList"
        created.provider_id = provider_id or compat_provider("openlist")
        created.ingest_method = "openlist_api"
        created.source_route_id = source_route_id
        version.preset_id = created.preset_id
        activate_version(created, version, snapshot, plan)
        phase("complete", 99, "扫描完成")
        return created, version, plan, None, False, False
    except Exception:
        # 失败时不保留本次扫描的候选快照；清单仅在没有版本引用时清理
        discard_candidate_snapshot(snapshot.snapshot_id)
        _discard_openlist_manifest(manifest_path, if_unreferenced=True)
        raise


def _openlist_manifest_size(manifest_path: Path) -> int:
    try:
        return manifest_path.stat().st_size
    except OSError:
        return 0
