"""把本地目录或目录树文本转换为 SourceEvidence 输入。"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

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


def _root_id(path: Path) -> str:
    return "root_" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


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
    for line in text.splitlines():
        value = line.strip().replace("\\", "/")
        if not value or value.startswith("#") or Path(value).suffix.casefold() not in VIDEO_SUFFIXES:
            continue
        relative = value.lstrip("/")
        locator = value
        if source_root and not Path(value).is_absolute():
            locator = str(Path(source_root).expanduser() / Path(value))
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
