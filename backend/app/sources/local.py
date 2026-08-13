"""本地目录来源扫描器

递归扫描本地目录，生成 RawSnapshot / RawFile。
不读取 NFO / 图片作为媒体结构主真相。
"""

import hashlib
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.catalog.models import DirectoryPage, SourceNodeInput
from app.integrations.openlist.providers import compat_ingest, compat_provider
from app.raw.models import RawFile, RawSnapshot
from app.sources.base import SourceAdapter

# 已知文件扩展名（小写）
_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".rmvb", ".mov"}
_SUBTITLE_EXTS = {".ass", ".srt", ".ssa", ".vtt"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
_NFO_EXTS = {".nfo"}
_FONT_EXTS = {".ttf", ".ttc", ".otf"}
_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".exe"}
_AUDIO_EXTS = {".mp3", ".flac", ".mka", ".wav", ".aac", ".ogg", ".wma"}
_TEXT_EXTS = {".txt", ".log", ".cue", ".md", ".ini", ".cfg", ".conf"}

_ALL_KNOWN_EXTS = (
    _VIDEO_EXTS | _SUBTITLE_EXTS | _IMAGE_EXTS | _NFO_EXTS
    | _FONT_EXTS | _ARCHIVE_EXTS | _AUDIO_EXTS | _TEXT_EXTS
)

_SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

# 跳过的目录名
_SKIP_DIRS = {"__pycache__", ".git", ".svn", ".hg", "node_modules", ".idea", ".vscode"}
_FINGERPRINT_MAX_SIZE = 256 * 1024 * 1024
_FINGERPRINT_CHUNK_SIZE = 64 * 1024
_MOUNT_SCAN_MAX_ENTRIES = 20_000
_MOUNT_SCAN_MAX_DEPTH = 12
_MOUNT_SCAN_DIRECTORY_DELAY = 0.05
_MOUNT_SCAN_RETRY_DELAYS = (0.25, 0.75, 1.5)


def _get_resource_hint(ext: str) -> str:
    ext_lower = ext.lower()
    if ext_lower in _VIDEO_EXTS:
        return "video"
    if ext_lower in _SUBTITLE_EXTS:
        return "subtitle"
    if ext_lower in _IMAGE_EXTS:
        return "image"
    if ext_lower in _NFO_EXTS:
        return "nfo"
    if ext_lower in _FONT_EXTS:
        return "font"
    if ext_lower in _ARCHIVE_EXTS:
        return "archive"
    if ext_lower in _AUDIO_EXTS:
        return "audio"
    if ext_lower in _TEXT_EXTS:
        return "text"
    return "other"


def _is_system_file(name: str) -> bool:
    return name.lower() in _SYSTEM_FILES


def _should_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(".")


def _split_name_ext(name: str) -> tuple[str, str]:
    dot_idx = name.rfind(".")
    if dot_idx <= 0:
        return name, ""
    return name[:dot_idx], name[dot_idx:]


def _make_stable_id(source: str, relative_path: str) -> str:
    content = f"{source}:{relative_path}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _make_snapshot_id(source: str, root_path: str) -> str:
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    content = f"{source}:{root_path}:{now}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _content_fingerprint(path: Path, size: int) -> str:
    if size < 0 or size > _FINGERPRINT_MAX_SIZE:
        return ""
    try:
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with path.open("rb") as stream:
            digest.update(stream.read(_FINGERPRINT_CHUNK_SIZE))
            if size > _FINGERPRINT_CHUNK_SIZE:
                stream.seek(max(0, size - _FINGERPRINT_CHUNK_SIZE))
                digest.update(stream.read(_FINGERPRINT_CHUNK_SIZE))
        return digest.hexdigest()[:24]
    except OSError:
        return ""


class LocalScanner(SourceAdapter):
    """本地目录来源扫描器"""

    @property
    def source_id(self) -> str:
        return "local"

    @property
    def mirror_namespace(self) -> str:
        return "local"

    def parse(self, input_path: str, source_root: str = "") -> RawSnapshot:
        """扫描本地目录

        参数:
            input_path: 本地目录路径（用作 root_path）
            source_root: 不使用，保留接口兼容

        返回:
            RawSnapshot
        """
        return self.scan(input_path, source_root=source_root)

    def scan(
        self,
        input_path: str,
        source_root: str = "",
        logical_source: str = "local",
        include_root: bool = False,
        metadata_only: bool = False,
        should_cancel: Callable[[], bool] | None = None,
        max_entries: int | None = None,
        max_depth: int | None = None,
        directory_delay: float | None = None,
        retry_delays: tuple[float, ...] | None = None,
    ) -> RawSnapshot:
        """扫描文件系统；网盘挂载使用 metadata_only 禁止读取媒体内容。

        挂载扫描默认限制条目数和目录深度，并对目录枚举做轻量节流与退避。
        本地导入不启用这些保守限制，避免改变原有导入能力。
        """
        root_path = Path(input_path)
        if not source_root:
            source_root = str(root_path)

        effective_max_entries = max_entries if max_entries is not None else (
            _MOUNT_SCAN_MAX_ENTRIES if metadata_only else None
        )
        effective_max_depth = max_depth if max_depth is not None else (
            _MOUNT_SCAN_MAX_DEPTH if metadata_only else None
        )
        effective_directory_delay = directory_delay if directory_delay is not None else (
            _MOUNT_SCAN_DIRECTORY_DELAY if metadata_only else 0
        )
        effective_retry_delays = retry_delays if retry_delays is not None else (
            _MOUNT_SCAN_RETRY_DELAYS if metadata_only else ()
        )

        snapshot_id = _make_snapshot_id(logical_source, str(root_path))
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()

        files: list[RawFile] = []

        for file_path, stat in self._walk(
            root_path,
            should_cancel=should_cancel,
            max_entries=effective_max_entries,
            max_depth=effective_max_depth,
            directory_delay=max(0, effective_directory_delay),
            retry_delays=effective_retry_delays,
        ):

            relative_path = str(file_path.relative_to(root_path)).replace("\\", "/")
            if include_root:
                relative_path = f"{root_path.name}/{relative_path}"
            parts = relative_path.split("/")
            name = parts[-1]

            if _is_system_file(name):
                continue

            stem, ext = _split_name_ext(name)

            virtual_root = parts[0] if len(parts) > 1 else ""
            parent_path = "/".join(parts[:-1]) if len(parts) > 1 else ""
            resource_hint = _get_resource_hint(ext)
            real_path = str(file_path)
            file_id = _make_stable_id(logical_source, relative_path)

            raw_file = RawFile(
                id=file_id,
                snapshot_id=snapshot_id,
                source=logical_source,
                source_root=source_root,
                virtual_root=virtual_root,
                source_path_parts=parts,
                relative_path=relative_path,
                real_path=real_path,
                name=name,
                stem=stem,
                ext=ext,
                depth=len(parts),
                parent_path=parent_path,
                is_file=True,
                resource_hint=resource_hint,
                size=stat.st_size,
                mtime=stat.st_mtime,
                content_fingerprint=(
                    "" if metadata_only else _content_fingerprint(file_path, stat.st_size)
                ),
            )
            files.append(raw_file)

        file_count = len(files)
        video_count = sum(1 for f in files if f.resource_hint == "video")

        return RawSnapshot(
            snapshot_id=snapshot_id,
            source=logical_source,
            provider_id=compat_provider(logical_source),
            ingest_method=compat_ingest(logical_source),
            source_root=source_root,
            created_at=now,
            input_file=str(root_path),
            file_count=file_count,
            video_count=video_count,
            files=files,
        )

    def build_real_path(self, relative_path: str, source_root: str) -> str:
        """拼接真实路径"""
        return str(Path(source_root) / relative_path)

    @property
    def capabilities(self) -> dict:
        return {"paginated": True}

    def enumerate_directory(self, remote_path: str, page: int = 1, per_page: int = 100) -> DirectoryPage:
        """分页枚举本地目录的直接成员（不递归）。"""
        from app.catalog.models import DirectoryPage

        # Catalog 为了和 OpenList 共用路径层级，Windows 驱动器根使用
        # ``/C:/...`` 的虚拟 POSIX 定位；这里只在真正访问磁盘前还原。
        disk_path = remote_path[1:] if len(remote_path) > 3 and remote_path[0] == "/" and remote_path[2] == ":" else remote_path
        root = Path(disk_path).expanduser()
        try:
            entries = [item for item in os.scandir(root)]
        except OSError:
            return DirectoryPage(entries=[], total=0)
        start = (max(1, int(page)) - 1) * per_page
        chunk = entries[start:start + per_page]
        nodes = [
            SourceNodeInput(
                name=item.name,
                remote_path=(Path(remote_path) / item.name).as_posix(),
                parent_path=Path(remote_path).as_posix(),
                kind="dir" if item.is_dir() else "file",
                size=item.stat().st_size if item.is_file() else None,
                mtime=item.stat().st_mtime if item.is_file() else None,
            )
            for item in chunk
        ]
        return DirectoryPage(entries=nodes, total=len(entries))

    def _walk(
        self,
        root: Path,
        *,
        should_cancel: Callable[[], bool] | None,
        max_entries: int | None,
        max_depth: int | None,
        directory_delay: float,
        retry_delays: tuple[float, ...],
    ) -> Iterator[tuple[Path, os.stat_result]]:
        """以单次元数据读取递归遍历，且不跟随链接或重解析点。"""
        visited_entries = 0

        def check_cancel() -> None:
            if should_cancel and should_cancel():
                raise RuntimeError("任务已停止")

        def cooperative_sleep(seconds: float) -> None:
            remaining = max(0.0, seconds)
            while remaining > 0:
                check_cancel()
                interval = min(0.1, remaining)
                time.sleep(interval)
                remaining -= interval

        def list_directory(directory: Path):
            nonlocal visited_entries
            delays = (0.0, *retry_delays)
            for attempt, delay in enumerate(delays):
                if delay:
                    cooperative_sleep(delay)
                check_cancel()
                try:
                    records = []
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            check_cancel()
                            visited_entries += 1
                            if max_entries is not None and visited_entries > max_entries:
                                raise ValueError(
                                    f"扫描超过 {max_entries} 个目录条目的安全上限；"
                                    "请改用目录树导入，或选择更精确的作品目录"
                                )
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    records.append((entry.name, Path(entry.path), None))
                                elif entry.is_file(follow_symlinks=False):
                                    records.append((
                                        entry.name,
                                        Path(entry.path),
                                        entry.stat(follow_symlinks=False),
                                    ))
                            except OSError:
                                continue
                    return sorted(records, key=lambda item: item[0].casefold())
                except PermissionError:
                    return []
                except ValueError:
                    raise
                except OSError:
                    if attempt == len(delays) - 1:
                        raise
            return []

        def walk(directory: Path, depth: int):
            check_cancel()
            if max_depth is not None and depth > max_depth:
                raise ValueError(
                    f"扫描目录深度超过 {max_depth} 层的安全上限；"
                    "请改用目录树导入，或选择更精确的作品目录"
                )
            if directory_delay:
                cooperative_sleep(directory_delay)
            for name, path, stat in list_directory(directory):
                if stat is None:
                    if _should_skip_dir(name):
                        continue
                    yield from walk(path, depth + 1)
                else:
                    yield path, stat

        yield from walk(root, 0)
