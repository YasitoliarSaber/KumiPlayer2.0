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
from collections import deque
from pathlib import Path, PurePosixPath

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir
from app.integrations.openlist.client import normalize_remote_path
from app.integrations.openlist.models import (
    OpenListEntry,
    OpenListNotFoundError,
    OpenListScanLimitExceeded,
)
from app.integrations.openlist.providers import OpenListRouteConfig, derive_local_path, provider_for_remote
from app.media_v4.domain.models import SourceEvidence
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
from app.media_v4.sources.scanner import (
    VIDEO_SUFFIXES,
    SourceScanCancelled,
    list_dir_with_retry,
)

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


def _listing_hash(entries) -> str:
    """对目录直接子项的 name/is_dir/size/modified 求稳定哈希。

    网盘目录的 mtime 不一定可靠（同 mtime 内容被替换、中间层缓存等）。清单哈希
    变化同样算"目录发生变化"，避免只依赖 mtime 漏掉变化。
    """

    digest = hashlib.sha256()
    for item in sorted(entries, key=lambda entry: str(entry.name).casefold()):
        line = f"{item.name}|{int(bool(item.is_dir))}|{int(item.size or 0)}|{item.modified}\n"
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()[:32]


def build_tree_baseline_state(
    root_id: str,
    remote_root: str,
    evidence: list[SourceEvidence],
) -> dict:
    """由 TXT 文件路径建立零请求目录基线；时间事实保持 unknown。"""

    # 值里既有时间/浮点（modified、last_verified_at），也有字符串（verification_state、
    # listing_hash），因此按 object 声明；此前写 float | None 与新增字段冲突。
    directories: dict[str, dict[str, object]] = {
        "": {"modified": None, "last_verified_at": 0, "verification_state": "unknown", "listing_hash": ""},
    }
    for baseline_item in evidence:
        parts = PurePosixPath(baseline_item.relative_path).parts[:-1]
        for length in range(1, len(parts) + 1):
            relative = PurePosixPath(*parts[:length]).as_posix()
            directories.setdefault(
                relative,
                {"modified": None, "last_verified_at": 0, "verification_state": "unknown", "listing_hash": ""},
            )
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
            path: {
                "modified": modified,
                "last_verified_at": timestamp,
                "verification_state": "verified",
                # 完整扫描只提供 mtime；清单哈希留待下一次增量核对时写入。
                "listing_hash": "",
            }
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


def discard_scan_state(scan_id: str) -> None:
    """删除某次扫描的暂存检查点；文件不存在时无操作（终态清理用）。

    终态（failed / cancelled）的扫描不会再被确认，暂存文件留着只会随每次失败增长。
    这不会污染后续增量：发布基线用的是 `active/<root_id>.json`。
    """

    path = _staged_path(scan_id)
    with DATA_WRITE_LOCK:
        path.unlink(missing_ok=True)


def _list_all(
    client,
    remote_path: str,
    *,
    counter: list[int],
    max_entries: int,
    should_cancel=None,
    skipped: list[int] | None = None,
) -> list[OpenListEntry]:
    entries: list[OpenListEntry] = []
    page = 1
    # 请求与终止判据必须用同一个 per_page；短页在 total 缺失时不是末页。
    per_page = 100
    while True:
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        result = list_dir_with_retry(
            client,
            remote_path,
            page=page,
            per_page=per_page,
            should_cancel=should_cancel,
        )
        counter[0] += len(result.entries)
        if skipped is not None:
            skipped[0] += int(getattr(result, "skipped_entries", 0) or 0)
        if counter[0] > max_entries:
            raise OpenListScanLimitExceeded()
        entries.extend(result.entries)
        total = int(result.total or 0)
        if not result.entries:
            return entries
        if total and page * per_page >= total:
            return entries
        page += 1


def _clone_for_scan(
    item: SourceEvidence,
    scan_id: str,
    *,
    source_key: str | None = None,
    source_locator: str | None = None,
    playback_locator: str | None = None,
) -> SourceEvidence:
    return to_source_evidence(SourceEntry(
        root_id=item.root_id,
        scan_id=scan_id,
        provider=item.provider,
        ingest_method=item.ingest_method,
        relative_path=item.relative_path,
        source_key=source_key if source_key is not None else (item.source_key or item.relative_path),
        source_locator=source_locator if source_locator is not None else item.source_locator,
        playback_locator=playback_locator if playback_locator is not None else item.playback_locator,
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
    scan_id: str | None = None,
    default_provider: str = "unknown",
    routes: list[OpenListRouteConfig] | None = None,
    verification_budget: int = DEFAULT_VERIFICATION_BUDGET,
    max_entries: int = 20_000,
    max_depth: int = 32,
    now: float | None = None,
    should_cancel=None,
    on_evidence_batch=None,
    on_progress=None,
    batch_size: int = 128,
) -> tuple[str, list[SourceEvidence], dict, dict[str, object]]:
    """在 confirmed revision 文件全集上合并一轮受控 OpenList 核对。"""

    if not baseline:
        raise ValueError("增量扫描缺少已确认的来源基线，请先完成并确认首次完整扫描，或使用 TXT 建立大库基线")
    root_id = str(state.get("root_id") or "")
    if not root_id or any(item.root_id != root_id for item in baseline):
        raise ValueError("增量检查点与已确认来源不一致")
    remote_root = normalize_remote_path(str(state.get("remote_root") or "/"))
    mapping_root = normalize_remote_path(mapping_root)
    timestamp = float(time.time() if now is None else now)
    budget = max(0, min(int(verification_budget), MAX_VERIFICATION_BUDGET))
    actual_scan_id = scan_id or ("scan_" + uuid.uuid4().hex)
    next_state = copy.deepcopy(state)
    directories: dict[str, dict] = next_state["directories"]
    files = {}
    for baseline_item in baseline:
        remote_path = _join_remote(remote_root, baseline_item.relative_path)
        playback_locator = (
            derive_local_path(mount_root, mapping_root, remote_path)
            if mount_root else baseline_item.playback_locator
        )
        files[baseline_item.relative_path] = _clone_for_scan(
            baseline_item,
            actual_scan_id,
            source_key=remote_path,
            source_locator=remote_path,
            playback_locator=playback_locator,
        )
    rolling = sorted(
        (path for path in directories if path),
        key=lambda path: (float(directories[path].get("last_verified_at") or 0), path.casefold()),
    )[:budget]
    queue = deque(["", *rolling])
    queued = set(queue)
    processed: set[str] = set()
    changed_queued: set[str] = set()
    observed_counter = [0]
    skipped_counter = [0]
    #: 上游已不存在的"幽灵目录"（父目录仍列出它，但 fs/list 返回 object not found）
    missing_directories: list[str] = []
    route_configs = routes or []
    first_root_entries: list[OpenListEntry] | None = None
    pending: list[SourceEvidence] = []
    effective_batch_size = max(1, int(batch_size))

    while queue:
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        relative_dir = queue.popleft()
        if relative_dir in processed or relative_dir not in directories:
            continue
        if len(PurePosixPath(relative_dir).parts) > max_depth:
            raise OpenListScanLimitExceeded("OpenList 目录层级超过安全上限，请选择更精确的目录")
        remote_dir = _join_remote(remote_root, relative_dir)
        try:
            entries = _list_all(
                client,
                remote_dir,
                counter=observed_counter,
                max_entries=max_entries,
                should_cancel=should_cancel,
                skipped=skipped_counter,
            )
        except OpenListNotFoundError:
            # 幽灵目录：父目录列表里仍有它，但上游已经移动/改名/删除
            # （实测 /夸克网盘/动画/4k 京阿尼合集/冰菓 返回
            #  "failed get objs: failed get dir: object not found"，OpenList 把它
            #  包成 code=500）。目录不存在，其子树不可能含文件：跳过并记录。
            # 修复前这里会把整个扫描判失败——763 个目录的进度全部作废。
            # 根目录缺失是另一回事（用户选的源就不存在），必须如实报错。
            if relative_dir == "":
                raise
            processed.add(relative_dir)
            missing_directories.append(remote_dir)
            continue
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
        # 本目录的直接清单哈希：网盘 mtime 可能没变但内容已变，哈希变化必须
        # 让子目录重新入队，否则子树变化会被永久漏掉。
        previous_directory = directories.get(relative_dir) or {}
        previous_listing_hash = str(previous_directory.get("listing_hash") or "")
        listing_hash = _listing_hash(entries)
        directory_content_changed = bool(previous_listing_hash) and previous_listing_hash != listing_hash
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
            directories.setdefault(child_path, {
                "modified": current_modified,
                "last_verified_at": 0,
                "verification_state": "unknown",
                "listing_hash": "",
            })
            directories[child_path]["modified"] = current_modified
            changed = (
                previous_modified is not None
                and current_modified is not None
                and previous_modified != current_modified
            )
            if is_new or changed:
                # 新增/修改时间变化都要在本轮核对，且必须计入"变化目录"统计——
                # 即使它本来就被滚动抽样选中（否则统计会把"变化优先"误报为 0）。
                changed_queued.add(child_path)
                if child_path not in queued:
                    queue.append(child_path)
                    queued.add(child_path)
            elif directory_content_changed:
                # 本目录的直接清单哈希变了（例如同级新增/删除了条目），但该子目录
                # 自身 mtime 没有变化：不把它拉进本轮（否则一次增删会把所有子目录
                # 都拉进来，让受控增量膨胀成近全量重列），而是把它的"最近核对时间"
                # 归零，让下一轮的滚动抽样优先挑中它。子树变化不会被永久漏掉。
                directories[child_path]["last_verified_at"] = 0

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
            current_evidence = to_source_evidence(SourceEntry(
                root_id=root_id,
                scan_id=actual_scan_id,
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
            files[relative_file] = current_evidence
            pending.append(current_evidence)
            if len(pending) >= effective_batch_size:
                if on_evidence_batch is not None:
                    on_evidence_batch(pending)
                pending = []

        if on_progress is not None:
            # 增量扫描无法在枚举远端前知道最终媒体数；这里持续发送心跳，
            # 让 durable scan 的读取阶段可见，最终总数在扫描返回后由统一
            # 持久化边界确定，避免把基线数量伪装成远端总量。
            on_progress(processed_count=len(files), total_count=0)
        # 本次已核对：记住状态、mtime 与清单哈希；unknown → verified。
        directories[relative_dir]["verification_state"] = "verified"
        directories[relative_dir]["listing_hash"] = listing_hash
        directories[relative_dir]["last_verified_at"] = timestamp

    if first_root_entries is None:
        raise RuntimeError("OpenList 根目录未被核对")
    if pending and on_evidence_batch is not None:
        on_evidence_batch(pending)
    next_state["remote_verified"] = True
    verified_directories = sum(
        1 for entry in directories.values()
        if str(entry.get("verification_state") or "") == "verified"
    )
    stats = {
        "requested_directories": len(processed),
        "rolling_verified": sum(1 for path in rolling if path in processed),
        # TXT 基线只能给出"存在过这些目录"，远端时间事实要逐轮核对补齐：
        # 这两个数字让用户看到"还有多少目录没被远端核实过"。
        "verified_directories": verified_directories,
        "unknown_directories": len(directories) - verified_directories,
        # 只有本轮真正核对过的"变化目录"才算数：入队但被预算挡下的不算。
        "changed_directories": len([path for path in changed_queued if path in processed]),
        # 条目名非法被跳过的数量：这些文件不会入库，但必须可见（不静默丢弃）。
        "skipped_entries": skipped_counter[0],
        # 上游已不存在的"幽灵目录"（详见扫描循环里的跳过逻辑）：必须可见，
        # 否则用户会以为目录内容都进来了。
        "missing_directories": missing_directories[:20],
        "missing_directory_count": len(missing_directories),
    }
    return actual_scan_id, [files[path] for path in sorted(files, key=str.casefold)], next_state, stats
