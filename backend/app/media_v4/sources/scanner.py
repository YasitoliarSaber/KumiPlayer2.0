"""把本地目录或目录树文本转换为 SourceEvidence 输入。"""

from __future__ import annotations

import builtins
import ctypes
import hashlib
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from app.integrations.openlist.client import normalize_remote_path
from app.integrations.openlist.models import (
    OpenListError,
    OpenListNotFoundError,
    OpenListScanLimitExceeded,
)
from app.integrations.openlist.providers import (
    OpenListRouteConfig,
    derive_local_path,
    provider_for_remote,
)
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


class SourceScanCancelled(Exception):
    """用户在扫描读取过程中取消；后台任务应落为 cancelled 终态。"""


class SourceScanPaused(Exception):
    """本次巡检达到请求硬预算，进度已保留，可稍后继续。

    这不是成功也不是失败：frontier 与已交付证据都保留，但**不得**据此建立
    confirmed 基线——"部分进度可持久化"不等于"部分扫描算完整成功"。
    """


#: 目录列表遇到**瞬时上游错误**时的重试节奏（秒）。远端网盘驱动会偶发 5xx/超时：
#: 实测一次抖动就把已经扫到 763 个目录的完整扫描整轮打死。
LIST_RETRY_DELAYS = (1.0, 4.0, 10.0)

#: 可原地重试的失败类型。其余一律立即抛出：风控/限流重试只会加剧，
#: 本地准入拒绝、认证/权限/路径问题重试也没有意义。
RETRYABLE_LIST_KINDS = frozenset(
    {"server_error", "timeout", "network", "unknown", "page_consistency"}
)


def list_dir_with_retry(
    client,
    remote_path: str,
    *,
    page: int,
    per_page: int,
    should_cancel=None,
):
    """列目录；瞬时上游故障原地重试（取消检查放在等待之前）。

    完整扫描与增量核对共用本函数，避免两条路径的行为再次分叉。
    """

    attempt = 0
    while True:
        try:
            return client.list_dir(remote_path, page=page, per_page=per_page, refresh=False)
        except OpenListError as exc:
            if exc.kind not in RETRYABLE_LIST_KINDS or attempt >= len(LIST_RETRY_DELAYS):
                raise
            if should_cancel is not None and should_cancel():
                raise SourceScanCancelled() from exc
            time.sleep(LIST_RETRY_DELAYS[attempt])
            attempt += 1


# OpenList 全量扫描的默认目录请求预算：超过即暂停并保留断点，避免为了跑完
# 一个大库而持续打满上游请求（风控风险与耗时都不可控）。
DEFAULT_FULL_SCAN_DIRECTORY_BUDGET = 400

_UNICODE_TREE_LINE = re.compile(
    r"^(?P<prefix>(?:(?:│ {2,3})|(?: {3,4}))*)"
    r"(?:├──\s?|└──\s?|├─\s?|└─\s?)"
    r"(?P<name>.+)$"
)
_UNICODE_PREFIX_TOKEN = re.compile(r"(?:│ {2,3})|(?: {3,4})")
_PAN115_ROOT_LINE = re.compile(r"^\|[—-]{2}(?P<name>.+)$")
_PAN115_TREE_LINE = re.compile(r"^(?P<prefix>(?:\| )*)\|-(?P<name>.+)$")

_WINDOWS_DRIVE_TYPES = {
    0: "unknown",
    1: "invalid",
    2: "removable",
    3: "fixed",
    4: "remote",
    5: "cdrom",
    6: "ramdisk",
}
_VIRTUAL_FILESYSTEM_MARKERS = ("fuse", "winfsp", "webdav", "rclone", "cloud")


def _windows_volume_profile(path: str | Path) -> tuple[str, str]:
    """返回 Windows 卷类型与文件系统名，供本地扫描拒绝网盘映射。"""

    if os.name != "nt":
        return "unknown", ""
    anchor = Path(path).expanduser().anchor
    if not anchor:
        return "unknown", ""
    drive_type = _WINDOWS_DRIVE_TYPES.get(ctypes.windll.kernel32.GetDriveTypeW(anchor), "unknown")
    filesystem_name = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        anchor,
        None,
        0,
        None,
        None,
        None,
        filesystem_name,
        len(filesystem_name),
    )
    return drive_type, filesystem_name.value if ok else ""


def _path_is_within(path: str | Path, root: str | Path) -> bool:
    candidate = os.path.normcase(os.path.abspath(os.fspath(Path(path).expanduser())))
    boundary = os.path.normcase(os.path.abspath(os.fspath(Path(root).expanduser())))
    try:
        return os.path.commonpath((candidate, boundary)) == boundary
    except ValueError:
        return False


def _ensure_physical_local_volume(
    path: str | Path,
    *,
    excluded_roots: Iterable[str | Path] = (),
) -> None:
    drive_type, filesystem_name = _windows_volume_profile(path)
    normalized_filesystem = filesystem_name.casefold()
    configured_cloud_path = any(
        str(root).strip() and _path_is_within(path, root)
        for root in excluded_roots
    )
    if (
        drive_type == "remote"
        or configured_cloud_path
        or any(marker in normalized_filesystem for marker in _VIRTUAL_FILESYSTEM_MARKERS)
    ):
        raise ValueError(
            "本地目录只支持本机物理磁盘；检测到网盘挂载或虚拟文件系统，"
            "请改用目录树 TXT 或 OpenList 导入"
        )


def _tree_node(line: str) -> tuple[int, str, bool] | None:
    """解析百度/Windows tree 与 115 ASCII tree 的节点层级。"""

    stripped = line.rstrip("\r\n")
    match = _UNICODE_TREE_LINE.match(stripped)
    if match:
        return (
            len(_UNICODE_PREFIX_TOKEN.findall(match.group("prefix"))),
            match.group("name").strip(),
            False,
        )
    match = _PAN115_ROOT_LINE.match(stripped)
    if match:
        return 0, match.group("name").strip(), True
    match = _PAN115_TREE_LINE.match(stripped)
    if match:
        return match.group("prefix").count("| "), match.group("name").strip(), True
    return None


VIDEO_SUFFIXES = frozenset({
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".rm",
    ".rmvb",
    ".ts",
    ".webm",
    ".wmv",
})

# 目录树文件上限，与旧版 media_presets 64 MB 边界一致。
MAX_TREE_FILE_BYTES = 64 * 1024 * 1024


def _emit_evidence_batch(callback: Callable | None, batch: list) -> None:
    if callback is not None and batch:
        callback(batch)


def _emit_scan_progress(
    callback: Callable | None,
    *,
    processed_count: int,
    total_count: int = 0,
) -> None:
    if callback is not None:
        callback(processed_count=processed_count, total_count=total_count)


class DirectoryTreeReadError(Exception):
    """目录树读取/解码失败；kind 供 API 映射为用户可读中文。"""

    def __init__(self, kind: str, message: str = "") -> None:
        self.kind = kind
        super().__init__(message or kind)


def _read_tree_bytes(path: Path) -> bytes:
    """单次二进制读取，不依赖 resolve/is_file/stat，兼容 WinFSP 等虚拟卷。"""

    try:
        with builtins.open(path, "rb") as handle:
            data = handle.read(MAX_TREE_FILE_BYTES + 1)
    except OSError as exc:
        raise DirectoryTreeReadError("unreadable", f"目录树文件无法读取: {exc}") from exc
    if not data:
        raise DirectoryTreeReadError("empty", "目录树文件为空")
    if len(data) > MAX_TREE_FILE_BYTES:
        raise DirectoryTreeReadError("too_large", "目录树文件过大，请拆分后重新导出")
    return data


def _nul_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    return sum(1 for byte in data if byte == 0) / len(data)


def _media_line_count(text: str) -> int:
    """统计可识别的目录树结构行与媒体路径行，用于候选编码打分。"""

    count = 0
    for line in text.splitlines():
        stripped = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#"):
            continue
        if _tree_node(stripped) is not None:
            count += 1
            continue
        value = stripped.strip().replace("\\", "/")
        if Path(value).suffix.casefold() in VIDEO_SUFFIXES:
            count += 2
    return count


def _looks_like_tree_text(text: str) -> bool:
    """轻量结构校验：无异常 NUL 且至少存在可识别的树节点或媒体路径行。"""

    if "\x00" in text:
        return False
    return _media_line_count(text) > 0


def _decode_tree_bytes(data: bytes) -> str:
    """BOM 优先的严格解码；禁止 errors=ignore/replace 以免静默损坏文件名。

    候选顺序由文件字节决定：EF BB BF → utf-8-sig；FF FE / FE FF → utf-16；
    无 BOM → UTF-8 strict → GB18030 → 有明显 NUL 分布时的 UTF-16LE/BE。
    多个候选可解码时按可识别节点/媒体路径数量选择最像目录树的结果。
    """

    candidates: list[tuple[int, str]] = []
    if data.startswith(b"\xef\xbb\xbf"):
        candidates.append((0, "utf-8-sig"))
    elif data.startswith(b"\xff\xfe"):
        candidates.append((1, "utf-16"))
    elif data.startswith(b"\xfe\xff"):
        candidates.append((2, "utf-16"))
    else:
        candidates.append((3, "utf-8"))
        candidates.append((4, "gb18030"))
        if len(data) >= 2 and len(data) % 2 == 0:
            if _nul_ratio(data[1::2]) > 0.5:
                candidates.append((5, "utf-16-le"))
            elif _nul_ratio(data[0::2]) > 0.5:
                candidates.append((6, "utf-16-be"))

    best: str | None = None
    best_score = 0
    any_decoded = False
    for _priority, codec in candidates:
        try:
            text = data.decode(codec)
        except (UnicodeDecodeError, ValueError):
            continue
        any_decoded = True
        if not _looks_like_tree_text(text):
            continue
        score = _media_line_count(text)
        if score > best_score:
            best = text
            best_score = score
    if best is not None:
        return best
    if any_decoded:
        raise DirectoryTreeReadError(
            "not_a_tree",
            "文件内容不是可识别的目录树文本，请确认选择了正确的目录树 TXT",
        )
    raise DirectoryTreeReadError(
        "encoding",
        "无法识别目录树文件编码，请重新导出 UTF-8/UTF-16 TXT，或确认文件未损坏",
    )


def read_directory_tree_text(file_path: str | Path) -> str:
    """读取并解码目录树文本；与 parse_directory_tree_file 共用同一解码器。"""

    path = Path(file_path).expanduser()
    return _decode_tree_bytes(_read_tree_bytes(path))


def tree_media_relative_paths(text: str, *, provider: str = "") -> list[str]:
    """从解码后的目录树文本提取媒体相对路径；不写数据库、不做身份判断。"""

    return [
        path
        for path, _kind in _tree_relative_paths(text, provider=provider)
        if _kind == "video"
    ]


def detect_tree_provider(text: str) -> str:
    """按正文语法判断目录树来源：pan115 / baidu / unknown / ambiguous。

    两种格式的语法互斥：115 用 ``|——`` 与 ``|-``，百度/Windows tree 用 ``├──``
    配 ``│`` 缩进。用户选错来源标签时，解析规则仍应按正文实际格式工作。
    """

    pan115 = 0
    baidu = 0
    for line in text.splitlines():
        stripped = line.rstrip("\r\n")
        if _PAN115_ROOT_LINE.match(stripped) or _PAN115_TREE_LINE.match(stripped):
            pan115 += 1
        elif _UNICODE_TREE_LINE.match(stripped):
            baidu += 1
    if pan115 and baidu:
        return "ambiguous"
    if pan115:
        return "pan115"
    if baidu:
        return "baidu"
    return "unknown"


def _skip_shell_root(provider: str, detected: str) -> bool:
    """是否需要裁掉 115 导出的外壳层（``根目录``）。

    **正文语法优先**：目录树内容已经能唯一判定格式时，按正文实际格式解析，
    用户选错来源标签也不会解析错。只有正文无法判定（空文件、纯路径清单）
    时才退回来源标签。绝不因为“混入了一行 115 风格文本”就把整棵百度树裁掉
    一层——那种跨文件粘性状态正是旧实现的脆弱点。
    """

    if detected == "pan115":
        return True
    if detected == "baidu":
        return False
    mode = (provider or "").strip().casefold()
    return mode in {"pan115", "115"}


def _tree_relative_paths(text: str, *, provider: str = "") -> list[tuple[str, str]]:
    """提取视频相对路径；NFO 等 metadata 在 TXT 链路整体忽略。

    TXT 只是目录结构证据：先更新目录栈保持缩进层级，再按资源类型过滤，
    保证过滤不会破坏后续条目的层级。NFO 不读取、不入身份候选，也不把
    文件名 tvshow 变成作品。外壳根裁剪按 provider 决定，不做跨文件粘性判断。"""

    results: list[tuple[str, str]] = []
    stack: list[str] = []
    skip_tree_root = _skip_shell_root(provider, detect_tree_provider(text))
    for line in text.splitlines():
        node = _tree_node(line)
        if node is not None:
            depth, name, _node_skips_root = node
            if len(stack) > depth:
                stack = stack[:depth]
            while len(stack) < depth:
                stack.append("")
            if len(stack) == depth:
                stack.append(name)
            else:
                stack[depth] = name
            suffix = Path(name).suffix.casefold()
            if suffix not in VIDEO_SUFFIXES:
                continue
            parts = [part for part in stack[: depth + 1] if part]
            if skip_tree_root and parts:
                parts = parts[1:]
            relative = "/".join(parts)
        else:
            value = line.strip().replace("\\", "/")
            suffix = Path(value).suffix.casefold()
            if not value or value.startswith("#") or suffix not in VIDEO_SUFFIXES:
                continue
            relative = value.lstrip("/")
        if relative:
            results.append((relative, "video"))
    return results


def build_directory_tree_evidence(
    text: str,
    *,
    root_id: str,
    provider: str,
    source_root: str = "",
    source_route_id: str = "",
    scan_id: str | None = None,
    on_evidence_batch: Callable | None = None,
    on_progress: Callable | None = None,
    batch_size: int = 128,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[str, list]:
    """由已解码文本构建 SourceEvidence；避免为验证根再读一次文件。"""

    actual_scan_id = scan_id or ("scan_" + uuid.uuid4().hex)
    evidence = []
    pending: list = []
    entries = _tree_relative_paths(text, provider=provider)
    effective_batch_size = max(1, int(batch_size))
    for index, (relative, kind) in enumerate(entries, start=1):
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        locator = relative
        if source_root:
            locator_path = Path(source_root).expanduser()
            for part in PurePosixPath(relative).parts:
                locator_path /= part
            locator = str(locator_path)
        item = to_source_evidence(
            SourceEntry(
                root_id=root_id,
                scan_id=actual_scan_id,
                provider=provider,
                ingest_method="directory_tree",
                relative_path=relative,
                source_key=relative,
                source_locator=locator,
                playback_locator=locator,
                source_route_id=source_route_id,
                entry_kind=kind,
            )
        )
        evidence.append(item)
        pending.append(item)
        if len(pending) >= effective_batch_size:
            _emit_evidence_batch(on_evidence_batch, pending)
            pending = []
            _emit_scan_progress(
                on_progress,
                processed_count=index,
                total_count=len(entries),
            )
    _emit_evidence_batch(on_evidence_batch, pending)
    if pending:
        _emit_scan_progress(
            on_progress,
            processed_count=len(entries),
            total_count=len(entries),
        )
    return actual_scan_id, evidence


def _root_id(path: Path) -> str:
    return "root_" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


def tree_root_id(provider: str, source_root: str, tree_file: str | Path) -> str:
    """目录树更新文件名可变，但同一逻辑来源根必须得到稳定 root_id。"""

    stem = Path(tree_file).stem
    scope = re.sub(r"[_\s-]*(?:文件目录|目录树)(?:[_\s-]*\d{6,})?$", "", stem).strip()
    mapping_identity = source_root.replace("\\", "/").strip().casefold()
    scope_identity = (scope or stem).replace("\\", "/").strip().casefold()
    identity = "\x1f".join((mapping_identity, scope_identity))
    digest = hashlib.sha256(f"{provider.casefold()}\x1f{identity}".encode()).hexdigest()[:24]
    return "root_" + digest


def openlist_root_id(server_url: str, username: str, remote_root: str) -> str:
    identity = "\x1f".join((server_url.strip().casefold(), username.strip().casefold(), normalize_remote_path(remote_root)))
    return "root_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def scan_local_directory(
    root_path: str | Path,
    *,
    excluded_roots: Iterable[str | Path] = (),
    scan_id: str | None = None,
    should_cancel=None,
    on_evidence_batch: Callable | None = None,
    on_progress: Callable | None = None,
    batch_size: int = 128,
) -> tuple[str, str, list]:
    candidate = Path(root_path).expanduser()
    excluded = tuple(excluded_roots)
    _ensure_physical_local_volume(candidate, excluded_roots=excluded)
    root = candidate.resolve()
    _ensure_physical_local_volume(root, excluded_roots=excluded)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    root_id = _root_id(root)
    actual_scan_id = scan_id or ("scan_" + uuid.uuid4().hex)
    evidence: list = []
    pending: list = []
    effective_batch_size = max(1, int(batch_size))
    visited = 0
    for path in root.rglob("*"):
        visited += 1
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        if not path.is_file() or path.suffix.casefold() not in VIDEO_SUFFIXES:
            if visited % effective_batch_size == 0:
                _emit_scan_progress(
                    on_progress,
                    processed_count=len(evidence),
                    total_count=0,
                )
            continue
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        locator = str(path)
        item = to_source_evidence(
            SourceEntry(
                root_id=root_id,
                scan_id=actual_scan_id,
                provider="local",
                ingest_method="local_scan",
                relative_path=relative,
                source_key=relative,
                source_locator=locator,
                playback_locator=locator,
                size=stat.st_size,
                mtime=stat.st_mtime,
                fingerprint=f"stat:{stat.st_size}:{stat.st_mtime_ns}",
            )
        )
        evidence.append(item)
        pending.append(item)
        if len(pending) >= effective_batch_size:
            _emit_evidence_batch(on_evidence_batch, pending)
            pending = []
            _emit_scan_progress(
                on_progress,
                processed_count=len(evidence),
                total_count=0,
            )
        elif visited % effective_batch_size == 0:
            _emit_scan_progress(
                on_progress,
                processed_count=len(evidence),
                total_count=0,
            )
    _emit_evidence_batch(on_evidence_batch, pending)
    if pending or visited:
        _emit_scan_progress(
            on_progress,
            processed_count=len(evidence),
            total_count=len(evidence),
        )
    evidence.sort(key=lambda item: item.source_key.casefold())
    return root_id, actual_scan_id, evidence


def parse_directory_tree_file(
    file_path: str | Path,
    *,
    root_id: str,
    provider: str,
    source_root: str = "",
    source_route_id: str = "",
) -> tuple[str, list]:
    # 目录树可能位于 WebDAV/CloudDrive 等虚拟盘。此类盘符可以正常打开文件，
    # 但不一定实现 Windows 最终路径解析；导入合同只要求 TXT 可读，不要求
    # 底层卷支持 resolve/stat。文本解码由 read_directory_tree_text 统一负责。
    text = read_directory_tree_text(file_path)
    return build_directory_tree_evidence(
        text,
        root_id=root_id,
        provider=provider,
        source_root=source_root,
        source_route_id=source_route_id,
    )


def scan_openlist_directory(
    client,
    *,
    remote_root: str,
    mapping_root: str,
    mount_root: str,
    root_id: str,
    scan_id: str | None = None,
    default_provider: str = "unknown",
    routes: list[OpenListRouteConfig] | None = None,
    max_entries: int = 20_000,
    max_depth: int = 32,
    directory_observations: dict[str, float | None] | None = None,
    directory_budget: int | None = None,
    scan_stats: dict | None = None,
    frontier_next: Callable[[], dict | None] | None = None,
    frontier_mark: Callable[..., None] | None = None,
    frontier_add: Callable[..., int] | None = None,
    should_cancel=None,
    on_evidence_batch: Callable | None = None,
    on_progress: Callable | None = None,
    batch_size: int = 128,
) -> tuple[str, list]:
    """递归枚举 OpenList，并只输出统一的不可变 SourceEvidence。

    传入 ``frontier_next`` / ``frontier_mark`` / ``frontier_add`` 时，遍历由
    目录级 frontier 驱动：每列完一页写回 ``next_page``，目录完成置 ``completed``，
    因此进程重启、风控中断或用户取消后都能从断点继续，而不是从根目录重扫。
    不传时保持原有的内存队列行为。
    """

    selected_root = normalize_remote_path(remote_root)
    mapping_root = normalize_remote_path(mapping_root)
    actual_scan_id = scan_id or ("scan_" + uuid.uuid4().hex)
    evidence: list = []
    frontier_driven = (
        frontier_next is not None and frontier_mark is not None and frontier_add is not None
    )
    queue: deque[tuple[str, int]] | None = None
    if frontier_driven and frontier_add is not None:
        frontier_add(remote_paths=[selected_root], depth=0)
    else:
        queue = deque([(selected_root, 0)])
    seen_directories: set[str] = set()
    observed_entries = 0
    skipped_entries = 0
    listed_directories = 0
    #: 上游已不存在的"幽灵目录"（父目录仍列出，fs/list 返回 object not found）
    missing_directories: list[str] = []
    budget_exhausted = False
    # 单页请求条目数：终止判据与请求必须用同一个值，否则"短页=末页"的误判会
    # 静默丢掉后续条目（详见下方终止判据处的说明）。
    per_page = 100
    route_configs = routes or []
    pending: list = []
    effective_batch_size = max(1, int(batch_size))
    if directory_observations is not None:
        directory_observations[""] = None

    while True:
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        if directory_budget is not None and listed_directories >= max(1, int(directory_budget)):
            # 达到硬预算：干净停下（不抛异常），由调用方把这次扫描收口为
            # paused/resumable。未列目录仍留在 frontier 里，续扫接着跑。
            budget_exhausted = True
            break
        if frontier_driven and frontier_next is not None:
            frontier_item = frontier_next()
            if frontier_item is None:
                break
            directory = str(frontier_item.get("remote_path") or "")
            depth = int(frontier_item.get("depth") or 0)
            page = max(1, int(frontier_item.get("next_page") or 1))
        else:
            if not queue:
                break
            directory, depth = queue.popleft()
            if directory in seen_directories:
                continue
            seen_directories.add(directory)
            page = 1
        if depth > max_depth:
            raise OpenListScanLimitExceeded("OpenList 目录层级超过安全上限，请选择更精确的目录")
        listed_directories += 1
        directory_entries = 0
        start_page = page
        while True:
            if should_cancel is not None and should_cancel():
                if frontier_driven and frontier_mark is not None:
                    frontier_mark(
                        remote_path=directory, status="scanning", next_page=page,
                    )
                raise SourceScanCancelled()
            try:
                result = list_dir_with_retry(
                    client,
                    directory,
                    page=page,
                    per_page=per_page,
                    should_cancel=should_cancel,
                )
                # 条目名非法被客户端跳过的数量（例如 `Show: Extra 01.mkv`）：这些
                # 文件不会入库，但必须让用户看见，不能静默丢弃。
                skipped_entries += int(getattr(result, "skipped_entries", 0) or 0)
            except OpenListNotFoundError:
                # 幽灵目录：父目录列表里仍有它，但上游已经移动/改名/删除
                # （实测 /夸克网盘/动画/4k 京阿尼合集/冰菓 的上游响应是
                #  "failed get objs: failed get dir: object not found"，OpenList
                #  把它包成 code=500）。目录不存在，其子树不可能含文件：
                # 跳过并记录，绝不让一次路径问题打死整轮扫描——修复前正是它把
                # 已经跑完 763 个目录的完整扫描判成 failed。
                missing_directories.append(directory)
                if frontier_driven and frontier_mark is not None:
                    # 断点同样收口：续扫不必反复撞这个不存在的目录。
                    frontier_mark(remote_path=directory, status="completed", next_page=page)
                break
            except Exception:
                if frontier_driven and frontier_mark is not None:
                    # 保留当前页游标：恢复时从这一页继续，已完成目录不会被重列。
                    frontier_mark(
                        remote_path=directory, status="scanning", next_page=page,
                    )
                raise
            for item in result.entries:
                observed_entries += 1
                if observed_entries > max_entries:
                    raise OpenListScanLimitExceeded()
                remote_path = normalize_remote_path(item.remote_path)
                if item.is_dir:
                    if directory_observations is not None:
                        directory_observations[
                            PurePosixPath(remote_path).relative_to(PurePosixPath(selected_root)).as_posix()
                        ] = item.modified
                    if frontier_driven and frontier_add is not None:
                        frontier_add(remote_paths=[remote_path], depth=depth + 1)
                    else:
                        assert queue is not None
                        queue.append((remote_path, depth + 1))
                    continue
                if Path(item.name).suffix.casefold() not in VIDEO_SUFFIXES:
                    continue
                relative = PurePosixPath(remote_path).relative_to(PurePosixPath(selected_root)).as_posix()
                route_id, routed_provider = provider_for_remote(route_configs, remote_path)
                provider = routed_provider if route_id else default_provider
                playback_locator = ""
                if mount_root:
                    playback_locator = derive_local_path(mount_root, mapping_root, remote_path)
                item_evidence = to_source_evidence(
                    SourceEntry(
                        root_id=root_id,
                        scan_id=actual_scan_id,
                        provider=provider,
                        ingest_method="openlist_api",
                        relative_path=relative,
                        source_key=remote_path,
                        source_locator=remote_path,
                        playback_locator=playback_locator,
                        source_route_id=route_id,
                        size=item.size,
                        mtime=item.modified,
                    )
                )
                evidence.append(item_evidence)
                pending.append(item_evidence)
                if len(pending) >= effective_batch_size:
                    _emit_evidence_batch(on_evidence_batch, pending)
                    pending = []
                    _emit_scan_progress(
                        on_progress,
                        processed_count=len(evidence),
                        total_count=0,
                    )
            total = int(result.total or 0)
            _emit_scan_progress(
                on_progress,
                processed_count=len(evidence),
                total_count=0,
            )
            # 终止判据必须基于**实际请求的 per_page**与**服务端是否给出 total**。
            # 原先写死 `len(entries) < 100`：当驱动把每页截得比请求值短、而 total
            # 又缺失（浏览链路的 test_openlist_pagination 已承认 total 可能缺失）时，
            # 短页会被误判成末页，该目录后续条目被静默丢弃且目录仍被标记 completed。
            # 空页才是确定的末页；有 total 时再按页数封顶。
            if not result.entries:
                break
            # 终止判据必须基于**实际收到的条目数**，而不是 `page * per_page`：
            # 驱动可能把每页截得比请求值短（见上方注释），此时按请求值推算会在
            # 第一页就判定"已到末页"，后续条目被静默丢弃且目录仍标 completed。
            # 续扫时（从第 N 页开始）本会话看不到前 N-1 页的条目，frontier 的
            # 前提是"前 N-1 页都是满页"，因此折算 (start_page-1)*per_page 补齐。
            directory_entries += len(result.entries)
            delivered_estimate = directory_entries + (start_page - 1) * per_page
            if total and delivered_estimate >= total:
                break
            page += 1
            if frontier_driven and frontier_mark is not None:
                # 断点边界必须先交付已收集证据：否则恢复时从下一页开始，
                # 上一页的证据会永久丢失（目录状态已推进）。
                _emit_evidence_batch(on_evidence_batch, pending)
                pending = []
                frontier_mark(remote_path=directory, status="scanning", next_page=page)
        if frontier_driven and frontier_mark is not None:
            # 目录完成同样是一个断点边界：先把证据交付，再置 completed，
            # 保证"标记完成的目录"其证据一定已经落库。
            _emit_evidence_batch(on_evidence_batch, pending)
            pending = []
            frontier_mark(remote_path=directory, status="completed", next_page=page)

    _emit_evidence_batch(on_evidence_batch, pending)
    if pending:
        _emit_scan_progress(
            on_progress,
            processed_count=len(evidence),
            total_count=len(evidence),
        )
    evidence.sort(key=lambda item: item.source_key.casefold())
    if scan_stats is not None:
        scan_stats["budget_exhausted"] = budget_exhausted
        scan_stats["directories_listed"] = listed_directories
        scan_stats["evidence_count"] = len(evidence)
        scan_stats["skipped_entries"] = skipped_entries
        # 上游已不存在的目录必须可见：否则用户会以为目录内容都进来了。
        scan_stats["missing_directories"] = missing_directories[:20]
        scan_stats["missing_directory_count"] = len(missing_directories)
    return actual_scan_id, evidence
