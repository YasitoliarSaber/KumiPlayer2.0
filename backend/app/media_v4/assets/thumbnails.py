"""本地媒体海报的派生缩略图缓存。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from PIL import Image

from app.core.paths import get_cache_dir

_THUMBNAIL_ENCODING_VERSION = 1
THUMBNAIL_WIDTHS: tuple[int, ...] = (256, 384, 512, 768, 1280)
DEFAULT_THUMBNAIL_WIDTH = 384
_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_GUARD = threading.Lock()


def _thumbnail_cache_dir() -> Path:
    directory = get_cache_dir() / "artwork_thumbnails"
    return directory


def _cache_key(source: Path, width: int) -> str:
    stat = source.stat()
    raw = (
        f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        f"|{width}|v{_THUMBNAIL_ENCODING_VERSION}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def cache_path(source: Path, width: int) -> Path:
    return _thumbnail_cache_dir() / f"{_cache_key(source, width)}.webp"


def find_thumbnail(source: Path, width: int) -> Path | None:
    """浏览路径只查询已落盘的派生图，不建目录、不编码。"""
    if width not in THUMBNAIL_WIDTHS or not is_supported_source(source):
        return None
    try:
        target = cache_path(source, width)
        return target if target.is_file() else None
    except OSError:
        return None


def artwork_widths(source: Path) -> tuple[int, ...]:
    if source.stem == "poster":
        return (256, 384, 512)
    if source.stem == "fanart":
        return (512, 768, 1280)
    return (384,)


def prepare_artwork(source: Path) -> None:
    """下载任务发布原图后提前准备常用尺寸；派生缓存失败不影响原始产物。"""
    if not is_supported_source(source):
        return
    for width in artwork_widths(source):
        try:
            get_or_create_thumbnail(source, width)
        except OSError:
            continue


def _acquire_inflight(key: str) -> threading.Event | None:
    with _INFLIGHT_GUARD:
        existing = _INFLIGHT.get(key)
        if existing is not None:
            return existing
        _INFLIGHT[key] = threading.Event()
        return None


def _generate(source: Path, target: Path, width: int) -> bool:
    fd, temp_name = tempfile.mkstemp(prefix=".thumb.", suffix=".webp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as temp_file:
            with Image.open(source) as image:
                working = image if image.mode in ("RGB", "RGBA", "L") else image.convert("RGB")
                working.thumbnail((width, width * 2), Image.Resampling.LANCZOS)
                working.save(temp_file, "WEBP", quality=85, method=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, target)
        return True
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        return False


def get_or_create_thumbnail(source: Path, width: int) -> Path | None:
    if width not in THUMBNAIL_WIDTHS:
        return None
    cache = cache_path(source, width)
    if cache.exists():
        return cache
    key = _cache_key(source, width)
    event = _acquire_inflight(key)
    if event is not None:
        event.wait(timeout=30.0)
        return cache if cache.exists() else None
    try:
        if cache.exists():
            return cache
        cache.parent.mkdir(parents=True, exist_ok=True)
        return cache if _generate(source, cache, width) else None
    finally:
        with _INFLIGHT_GUARD:
            done_event = _INFLIGHT.pop(key, None)
        if done_event is not None:
            done_event.set()


def is_supported_source(source: Path) -> bool:
    return source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
