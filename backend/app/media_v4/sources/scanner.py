"""把本地目录或目录树文本转换为 SourceEvidence 输入。"""

from __future__ import annotations

import hashlib
import re
import uuid
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
    identity = (source_root or scope or stem).replace("\\", "/").strip().casefold()
    digest = hashlib.sha256(f"{provider.casefold()}\x1f{identity}".encode()).hexdigest()[:24]
    return "root_" + digest


def openlist_root_id(server_url: str, username: str, remote_root: str) -> str:
    identity = "\x1f".join((server_url.strip().casefold(), username.strip().casefold(), normalize_remote_path(remote_root)))
    return "root_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def scan_local_directory(root_path: str | Path) -> tuple[str, str, list]:
    root = Path(root_path).expanduser().resolve()
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
) -> tuple[str, list]:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
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
