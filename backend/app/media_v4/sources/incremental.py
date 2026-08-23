"""V4 目录树基线与 OpenList 风险受控增量枚举。

检查点只决定下一次优先核对哪些远端目录，是可删除、可重建的扫描加速数据；
完整文件集合仍来自上一份 confirmed revision，并在本轮扫描后生成新的完整
SourceEvidence 集合。目录修改时间只用于提高优先级，不能单独证明子树未变化。
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from pathlib import Path, PurePosixPath

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir
from app.integrations.openlist.client import normalize_remote_path
from app.integrations.openlist.models import OpenListEntry, OpenListScanLimitExceeded
from app.integrations.openlist.providers import OpenListRouteConfig, derive_local_path, provider_for_remote
from app.media_v4.domain.models import SourceEvidence
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
from app.media_v4.sources.scanner import VIDEO_SUFFIXES

STATE_VERSION = 1
DEFAULT_VERIFICATION_BUDGET = 12
MAX_VERIFICATION_BUDGET = 50


def _parent(relative_path: str) -> str:
    parent = PurePosixPath(relative_path).parent.as_posix()
    return "" if parent == "." else parent


def _join_remote(root: str, relative_path: str) -> str:
    if not relative_path:
        return normalize_remote_path(root)
    return normalize_remote_path(f"{root.rstrip('/')}/{relative_path.lstrip('/')}")


def _relative(remote_root: str, remote_path: str) -> str:
    return PurePosixPath(normalize_remote_path(remote_path)).relative_to(
        PurePosixPath(normalize_remote_path(remote_root))
    ).as_posix()


def build_tree_baseline_state(
    root_id: str,
    remote_root: str,
    evidence: list[SourceEvidence],
) -> dict:
    """由 TXT 文件路径建立零请求目录基线；时间事实保持 unknown。"""

    directories: dict[str, dict[str, float | None]] = {
        "": {"modified": None, "last_verified_at": 0},
    }
    for item in evidence:
        parts = PurePosixPath(item.relative_path).parts[:-1]
        for length in range(1, len(parts) + 1):
            relative = PurePosixPath(*parts[:length]).as_posix()
            directories.setdefault(relative, {"modified": None, "last_verified_at": 0})
    return {
        "version": STATE_VERSION,
        "root_id": root_id,
        "remote_root": normalize_remote_path(remote_root),
        "remote_verified": False,
        "directories": directories,
    }


def build_full_scan_state(
    root_id: str,
    remote_root: str,
    directories: dict[str, float | None],
    *,
    now: float | None = None,
) -> dict:
    """把一次显式完整枚举转成后续增量核对所需的可重建检查点。"""

    timestamp = float(time.time() if now is None else now)
    return {
        "version": STATE_VERSION,
        "root_id": root_id,
        "remote_root": normalize_remote_path(remote_root),
        "remote_verified": True,
        "directories": {
            path: {"modified": modified, "last_verified_at": timestamp}
            for path, modified in sorted(directories.items(), key=lambda item: item[0].casefold())
        },
    }


def _state_root() -> Path:
    return get_data_dir() / "openlist_incremental"


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _active_path(root_id: str) -> Path:
    return _state_root() / "active" / f"{_safe_key(root_id)}.json"


def _staged_path(scan_id: str) -> Path:
    return _state_root() / "staged" / f"{_safe_key(scan_id)}.json"


def stage_scan_state(scan_id: str, state: dict) -> None:
    """暂存扫描检查点；未确认的扫描绝不能影响后续增量判断。"""

    path = _staged_path(scan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with DATA_WRITE_LOCK:
        write_json_atomic(path, state)


def load_active_state(root_id: str) -> dict | None:
    try:
        payload = json.loads(_active_path(root_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    directories = payload.get("directories") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != STATE_VERSION
        or payload.get("root_id") != root_id
        or not isinstance(payload.get("remote_root"), str)
        or not isinstance(directories, dict)
        or any(not isinstance(path, str) or not isinstance(value, dict) for path, value in directories.items())
    ):
        return None
    return payload


def activate_scan_state(scan_id: str) -> bool:
    """revision 确认后原子发布对应检查点；无暂存状态时保持无操作。"""

    staged = _staged_path(scan_id)
    try:
        state = json.loads(staged.read_text(encoding="utf-8"))
        root_id = str(state["root_id"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return False
    active = _active_path(root_id)
    active.parent.mkdir(parents=True, exist_ok=True)
    with DATA_WRITE_LOCK:
        write_json_atomic(active, state)
        staged.unlink(missing_ok=True)
    return True


def _list_all(
    client,
    remote_path: str,
    *,
    counter: list[int],
    max_entries: int,
) -> list[OpenListEntry]:
    entries: list[OpenListEntry] = []
    page = 1
    while True:
        result = client.list_dir(remote_path, page=page, per_page=100, refresh=False)
        counter[0] += len(result.entries)
        if counter[0] > max_entries:
            raise OpenListScanLimitExceeded()
        entries.extend(result.entries)
        total = int(result.total or 0)
        if len(result.entries) < 100 or (total and page * 100 >= total):
            return entries
        page += 1


def _clone_for_scan(item: SourceEvidence, scan_id: str) -> SourceEvidence:
    return to_source_evidence(SourceEntry(
        root_id=item.root_id,
        scan_id=scan_id,
        provider=item.provider,
        ingest_method=item.ingest_method,
        relative_path=item.relative_path,
        source_key=item.source_key or item.relative_path,
        source_locator=item.source_locator,
        playback_locator=item.playback_locator,
        raw_file_id=item.raw_file_id,
        source_route_id=item.source_route_id,
        size=item.size,
        mtime=item.mtime,
        fingerprint=item.fingerprint,
        entry_kind=item.entry_kind,
        tmdb_hint_id=item.tmdb_hint_id,
        tmdb_hint_type=item.tmdb_hint_type,
        import_family=item.import_family,
        target_filename=item.target_filename,
        observed_at=item.observed_at,
    ))


def scan_openlist_incremental(
    client,
    *,
    baseline: list[SourceEvidence],
    state: dict,
    mapping_root: str,
    mount_root: str,
    default_provider: str = "unknown",
    routes: list[OpenListRouteConfig] | None = None,
    verification_budget: int = DEFAULT_VERIFICATION_BUDGET,
    max_entries: int = 20_000,
    max_depth: int = 32,
    now: float | None = None,
) -> tuple[str, list[SourceEvidence], dict, dict[str, int]]:
    """在 confirmed revision 文件全集上合并一轮受控 OpenList 核对。"""

    if not baseline:
        raise ValueError("增量扫描缺少已确认的 TXT 基线")
    root_id = str(state.get("root_id") or "")
    if not root_id or any(item.root_id != root_id for item in baseline):
        raise ValueError("增量检查点与已确认来源不一致")
    remote_root = normalize_remote_path(str(state.get("remote_root") or "/"))
    mapping_root = normalize_remote_path(mapping_root)
    timestamp = float(time.time() if now is None else now)
    budget = max(0, min(int(verification_budget), MAX_VERIFICATION_BUDGET))
    scan_id = "scan_" + uuid.uuid4().hex
    next_state = copy.deepcopy(state)
    directories: dict[str, dict] = next_state["directories"]
    files = {item.relative_path: _clone_for_scan(item, scan_id) for item in baseline}
    rolling = sorted(
        (path for path in directories if path),
        key=lambda path: (float(directories[path].get("last_verified_at") or 0), path.casefold()),
    )[:budget]
    queue = ["", *rolling]
    queued = set(queue)
    processed: set[str] = set()
    changed_queued: set[str] = set()
    observed_counter = [0]
    route_configs = routes or []
    first_root_entries: list[OpenListEntry] | None = None

    while queue:
        relative_dir = queue.pop(0)
        if relative_dir in processed or relative_dir not in directories:
            continue
        if len(PurePosixPath(relative_dir).parts) > max_depth:
            raise OpenListScanLimitExceeded("OpenList 目录层级超过安全上限，请选择更精确的目录")
        remote_dir = _join_remote(remote_root, relative_dir)
        entries = _list_all(client, remote_dir, counter=observed_counter, max_entries=max_entries)
        processed.add(relative_dir)
        if relative_dir == "":
            first_root_entries = entries

        remote_directories: dict[str, OpenListEntry] = {}
        remote_videos: dict[str, OpenListEntry] = {}
        for item in entries:
            item_relative = _relative(remote_root, item.remote_path)
            if item.is_dir:
                remote_directories[item_relative] = item
            elif Path(item.name).suffix.casefold() in VIDEO_SUFFIXES:
                remote_videos[item_relative] = item

        known_children = {
            path for path in list(directories)
            if path and _parent(path) == relative_dir
        }
        if relative_dir == "":
            known_direct_files = {path for path in files if _parent(path) == ""}
            remote_names = {
                PurePosixPath(path).name.casefold()
                for path in (*remote_directories, *remote_videos)
            }
            known_names = {
                PurePosixPath(path).name.casefold()
                for path in (*known_children, *known_direct_files)
            }
            overlap = remote_names & known_names
            if known_names and not overlap:
                raise ValueError("已确认基线与所选 OpenList 目录没有共同条目，请检查远端根或执行完整校验")
            if len(known_names) >= 20 and len(overlap) * 5 < len(known_names):
                raise ValueError("OpenList 根目录内容骤减，自动增量已停止；确认远端状态后请执行完整校验")

        for missing_dir in known_children - set(remote_directories):
            prefix = missing_dir + "/"
            for path in list(directories):
                if path == missing_dir or path.startswith(prefix):
                    directories.pop(path, None)
            for path in list(files):
                if path.startswith(prefix):
                    files.pop(path, None)

        for child_path, item in remote_directories.items():
            previous = directories.get(child_path)
            previous_modified = previous.get("modified") if previous else None
            current_modified = item.modified
            is_new = previous is None
            directories.setdefault(child_path, {"modified": current_modified, "last_verified_at": 0})
            directories[child_path]["modified"] = current_modified
            changed = (
                previous_modified is not None
                and current_modified is not None
                and previous_modified != current_modified
            )
            if (is_new or changed) and child_path not in queued:
                queue.append(child_path)
                queued.add(child_path)
                changed_queued.add(child_path)

        existing_direct_files = {path for path in files if _parent(path) == relative_dir}
        for missing_file in existing_direct_files - set(remote_videos):
            files.pop(missing_file, None)
        for relative_file, item in remote_videos.items():
            remote_path = normalize_remote_path(item.remote_path)
            route_id, routed_provider = provider_for_remote(route_configs, remote_path)
            provider = routed_provider if route_id else default_provider
            playback_locator = (
                derive_local_path(mount_root, mapping_root, remote_path)
                if mount_root else ""
            )
            files[relative_file] = to_source_evidence(SourceEntry(
                root_id=root_id,
                scan_id=scan_id,
                provider=provider,
                ingest_method="openlist_api",
                relative_path=relative_file,
                source_key=remote_path,
                source_locator=remote_path,
                playback_locator=playback_locator,
                source_route_id=route_id,
                size=item.size,
                mtime=item.modified,
            ))
        directories[relative_dir]["last_verified_at"] = timestamp

    if first_root_entries is None:
        raise RuntimeError("OpenList 根目录未被核对")
    next_state["remote_verified"] = True
    stats = {
        "requested_directories": len(processed),
        "rolling_verified": sum(1 for path in rolling if path in processed),
        "changed_directories": len(changed_queued),
    }
    return scan_id, [files[path] for path in sorted(files, key=str.casefold)], next_state, stats
