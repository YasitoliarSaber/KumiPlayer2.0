"""本地海报派生缩略图管线。

分类页卡片加载本地镜像的 w780 原图会带来过高的解码与内存开销。
本模块惰性生成有限尺寸档的 webp 缩略图，缓存于 data/cache/artwork_thumbnails/，
绝不修改镜像原图。

设计要点：
- 有限枚举尺寸档，不允许任意宽度，避免无限缓存膨胀。
- 缓存键包含规范化源路径、文件大小、mtime_ns、尺寸档和编码版本，
  源文件变化时自动失效。
- 惰性生成，使用临时文件 + 原子替换；同一缓存键进程内去重。
- 失败时调用方回退原图，不留下永久灰占位。
"""

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from PIL import Image

from app.core.paths import get_cache_dir

# 缩略图编码版本；改变编码参数（quality、format 等）时递增，使旧缓存自动失效。
_THUMBNAIL_ENCODING_VERSION = 1

# 允许的缩略图最大宽度档（像素）。有限枚举，禁止任意宽度。
THUMBNAIL_WIDTHS: tuple[int, ...] = (384, 512)

DEFAULT_THUMBNAIL_WIDTH = 384


# --------------------------------------------------------------------------- #
# 进程内去重：同一缓存键同时只生成一次。
# --------------------------------------------------------------------------- #

_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_GUARD = threading.Lock()


def _thumbnail_cache_dir() -> Path:
    directory = get_cache_dir() / "artwork_thumbnails"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cache_key(source: Path, width: int) -> str:
    """根据源文件身份和尺寸档计算缓存键。"""
    stat = source.stat()
    raw = (
        f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        f"|{width}|v{_THUMBNAIL_ENCODING_VERSION}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def cache_path(source: Path, width: int) -> Path:
    """返回缩略图的目标缓存路径（不一定已生成）。"""
    return _thumbnail_cache_dir() / f"{_cache_key(source, width)}.webp"


def _acquire_inflight(key: str) -> threading.Event | None:
    """返回 None 表示调用方负责生成；返回 Event 表示需要等待其他线程。"""
    with _INFLIGHT_GUARD:
        existing = _INFLIGHT.get(key)
        if existing is not None:
            return existing
        event = threading.Event()
        _INFLIGHT[key] = event
        return None


def _generate(source: Path, target: Path, width: int) -> bool:
    """生成缩略图到临时文件，原子替换到目标路径。失败返回 False。"""
    fd, temp_name = tempfile.mkstemp(
        prefix=".thumb.", suffix=".webp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as temp_file:
            with Image.open(source) as img:
                # CMYK / P 等模式需转换；RGB / RGBA / 灰度可直接保存为 webp
                if img.mode not in ("RGB", "RGBA", "L"):
                    img = img.convert("RGB")
                # thumbnail 不会放大，只会缩小，保持原比例
                img.thumbnail((width, width * 2), Image.LANCZOS)
                img.save(temp_file, "WEBP", quality=85, method=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, target)
        return True
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        return False


def get_or_create_thumbnail(source: Path, width: int) -> Path | None:
    """返回缩略图缓存路径；生成失败返回 None（调用方回退原图）。

    线程安全：同一缓存键的并发请求只生成一次，其余线程等待结果。
    """
    if width not in THUMBNAIL_WIDTHS:
        return None

    cache = cache_path(source, width)
    if cache.exists():
        return cache

    key = _cache_key(source, width)
    event = _acquire_inflight(key)
    if event is not None:
        # 等待其他线程生成完成
        event.wait(timeout=30.0)
        return cache if cache.exists() else None

    # 当前线程负责生成
    try:
        # double-check：等待期间可能已被其他路径生成
        if cache.exists():
            return cache
        if _generate(source, cache, width):
            return cache
        return None
    finally:
        # 唤醒等待者并清理
        with _INFLIGHT_GUARD:
            done_event = _INFLIGHT.pop(key, None)
        if done_event is not None:
            done_event.set()


def is_supported_source(source: Path) -> bool:
    """源文件扩展名是否支持生成缩略图。"""
    return source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
