"""把本地目录或目录树文本转换为 SourceEvidence 输入。"""

from __future__ import annotations

import builtins
import ctypes
import hashlib
import os
import re
import uuid
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from app.integrations.openlist.client import normalize_remote_path
from app.integrations.openlist.models import OpenListScanLimitExceeded
from app.integrations.openlist.providers import (
    OpenListRouteConfig,
    derive_local_path,
    provider_for_remote,
)
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

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
    ".ts",
    ".webm",
    ".wmv",
})

# 目录树文件上限，与旧版 media_presets 64 MB 边界一致。
MAX_TREE_FILE_BYTES = 64 * 1024 * 1024


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


def tree_media_relative_paths(text: str) -> list[str]:
    """从解码后的目录树文本提取媒体相对路径；不写数据库、不做身份判断。"""

    relative_paths: list[str] = []
    stack: list[str] = []
    skip_tree_root = False
    for line in text.splitlines():
        node = _tree_node(line)
        if node is not None:
            depth, name, node_skips_root = node
            skip_tree_root = skip_tree_root or node_skips_root
            if len(stack) > depth:
                stack = stack[:depth]
            while len(stack) < depth:
                stack.append("")
            if len(stack) == depth:
                stack.append(name)
            else:
                stack[depth] = name
            if Path(name).suffix.casefold() not in VIDEO_SUFFIXES:
                continue
            parts = [part for part in stack[: depth + 1] if part]
            if skip_tree_root and parts:
                parts = parts[1:]
            relative = "/".join(parts)
        else:
            value = line.strip().replace("\\", "/")
            if not value or value.startswith("#") or Path(value).suffix.casefold() not in VIDEO_SUFFIXES:
                continue
            relative = value.lstrip("/")
        if relative:
            relative_paths.append(relative)
    return relative_paths


def build_directory_tree_evidence(
    text: str,
    *,
    root_id: str,
    provider: str,
    source_root: str = "",
    source_route_id: str = "",
    scan_id: str | None = None,
) -> tuple[str, list]:
    """由已解码文本构建 SourceEvidence；避免为验证根再读一次文件。"""

    actual_scan_id = scan_id or ("scan_" + uuid.uuid4().hex)
    evidence = []
    for relative in tree_media_relative_paths(text):
        locator = relative
        if source_root:
            locator_path = Path(source_root).expanduser()
            for part in PurePosixPath(relative).parts:
                locator_path /= part
            locator = str(locator_path)
        evidence.append(
            to_source_evidence(
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
                )
            )
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
) -> tuple[str, str, list]:
    candidate = Path(root_path).expanduser()
    excluded = tuple(excluded_roots)
    _ensure_physical_local_volume(candidate, excluded_roots=excluded)
    root = candidate.resolve()
    _ensure_physical_local_volume(root, excluded_roots=excluded)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    root_id = _root_id(root)
    scan_id = "scan_" + uuid.uuid4().hex
    evidence = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.suffix.casefold() not in VIDEO_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        locator = str(path)
        evidence.append(
            to_source_evidence(
                SourceEntry(
                    root_id=root_id,
                    scan_id=scan_id,
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
        )
    return root_id, scan_id, evidence


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
    default_provider: str = "unknown",
    routes: list[OpenListRouteConfig] | None = None,
    max_entries: int = 20_000,
    max_depth: int = 32,
    directory_observations: dict[str, float | None] | None = None,
) -> tuple[str, list]:
    """递归枚举 OpenList，并只输出统一的不可变 SourceEvidence。"""

    selected_root = normalize_remote_path(remote_root)
    mapping_root = normalize_remote_path(mapping_root)
    scan_id = "scan_" + uuid.uuid4().hex
    evidence = []
    queue: list[tuple[str, int]] = [(selected_root, 0)]
    seen_directories: set[str] = set()
    observed_entries = 0
    route_configs = routes or []
    if directory_observations is not None:
        directory_observations[""] = None

    while queue:
        directory, depth = queue.pop(0)
        if directory in seen_directories:
            continue
        if depth > max_depth:
            raise OpenListScanLimitExceeded("OpenList 目录层级超过安全上限，请选择更精确的目录")
        seen_directories.add(directory)
        page = 1
        while True:
            result = client.list_dir(directory, page=page, per_page=100, refresh=False)
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
                evidence.append(
                    to_source_evidence(
                        SourceEntry(
                            root_id=root_id,
                            scan_id=scan_id,
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
                )
            total = int(result.total or 0)
            if len(result.entries) < 100 or (total and page * 100 >= total):
                break
            page += 1

    return scan_id, evidence
