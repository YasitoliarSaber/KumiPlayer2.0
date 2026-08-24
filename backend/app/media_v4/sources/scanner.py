"""把本地目录或目录树文本转换为 SourceEvidence 输入。"""

from __future__ import annotations

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
    # 底层卷支持 resolve/stat。
    path = Path(file_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gb18030")
    scan_id = "scan_" + uuid.uuid4().hex
    evidence = []
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
        if not relative:
            continue
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
                    scan_id=scan_id,
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
    return scan_id, evidence


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
