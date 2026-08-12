"""Assets API 端点

GET /api/assets?path=<path>  返回 mirror 目录下的图片/NFO 文件

安全要求：
- 只允许访问 mirror root 下的文件
- 拒绝 ../ 路径遍历
- 拒绝 mirror root 外的绝对路径
- 拒绝源盘真实视频路径
- 只允许特定扩展名
"""

import hashlib
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from app.core.config import load_config
from app.core.paths import get_cache_dir, get_data_dir, get_mirror_root
from app.library.thumbnails import (
    DEFAULT_THUMBNAIL_WIDTH,
    THUMBNAIL_WIDTHS,
    get_or_create_thumbnail,
    is_supported_source,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])
_REMOTE_FAILURES: dict[str, float] = {}
_REMOTE_FAILURE_TTL = 60.0

# 允许的文件扩展名
_ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".svg",  # 图片
    ".nfo",  # NFO 元数据
}

# Content-Type 映射
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".nfo": "application/xml",
}


def _is_under_mirror_root(path: Path) -> bool:
    """检查路径是否在 mirror root 下"""
    mirror_root = get_mirror_root().resolve()
    try:
        path.resolve().relative_to(mirror_root)
        return True
    except ValueError:
        return False


def _is_allowed_local_asset(path: Path) -> bool:
    if _is_under_mirror_root(path):
        return True
    try:
        path.resolve().relative_to((get_data_dir() / "user_assets").resolve())
        return True
    except ValueError:
        return False


@router.get("")
def get_asset(path: str = Query(..., description="文件路径（绝对路径或 mirror 相对路径）")):
    """返回 mirror 目录下的文件

    支持两种路径格式：
    1. 绝对路径：如 D:/mirror/115/CLANNAD/poster.jpg
    2. 相对路径：如 115/CLANNAD/poster.jpg
    """
    if not path:
        raise HTTPException(status_code=400, detail="path 参数不能为空")

    # 检查路径遍历。只拒绝真正的 .. 路径组件，避免误伤正常文件名。
    raw_parts = Path(path).parts
    if any(part == ".." for part in raw_parts):
        raise HTTPException(status_code=403, detail="拒绝路径遍历")

    mirror_root = get_mirror_root()

    # 判断是绝对路径还是相对路径
    target_path = Path(path)
    if target_path.is_absolute():
        # 绝对路径：必须在 mirror root 下
        file_path = target_path.resolve()
    else:
        # 相对路径：拼接到 mirror root
        file_path = (mirror_root / path).resolve()

    # 安全检查：必须在 mirror root 下
    if not _is_allowed_local_asset(file_path):
        raise HTTPException(status_code=403, detail="路径不在允许的资源目录下")

    # 检查文件是否存在
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    # 检查是否为文件
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="路径不是文件")

    # 检查扩展名
    ext = file_path.suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=403, detail=f"不允许的文件类型: {ext}")

    # 返回文件
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",  # 缓存 1 小时
        },
    )


def _validate_remote_asset_url(url: str):
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("远程图片端口无效") from error
    if parsed.scheme != "https" or port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError("远程图片必须使用标准 HTTPS")
    trusted_path = (
        parsed.hostname == "image.tmdb.org" and parsed.path.startswith("/t/p/")
    ) or (
        parsed.hostname == "s4.anilist.co" and parsed.path.startswith("/file/anilistcdn/")
    )
    if not trusted_path:
        raise ValueError("远程图片地址不在受信任 CDN 范围内")
    return parsed


@router.get("/remote")
async def proxy_remote_asset(url: str = Query(..., description="Trusted metadata image URL")):
    """Proxy trusted metadata artwork without allowing arbitrary outbound requests."""
    try:
        _validate_remote_asset_url(url)
    except ValueError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error

    cache_path = _remote_asset_cache_path(url)
    if cache_path.exists():
        return FileResponse(
            path=str(cache_path),
            media_type=mimetypes.guess_type(str(cache_path))[0] or "image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    failed_at = _REMOTE_FAILURES.get(url)
    if failed_at and time.monotonic() - failed_at < _REMOTE_FAILURE_TTL:
        raise HTTPException(status_code=502, detail="远程图片暂时不可用")

    config = load_config()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            proxy=config.proxy_url or None,
        ) as client:
            remote = await client.get(url, headers={"Accept": "image/avif,image/webp,image/*"})
            remote.raise_for_status()
    except httpx.HTTPError as exc:
        _REMOTE_FAILURES[url] = time.monotonic()
        raise HTTPException(status_code=502, detail="远程图片加载失败") from exc

    content_type = remote.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        _REMOTE_FAILURES[url] = time.monotonic()
        raise HTTPException(status_code=502, detail="远端内容不是图片")
    if len(remote.content) > 12 * 1024 * 1024:
        _REMOTE_FAILURES[url] = time.monotonic()
        raise HTTPException(status_code=413, detail="图片过大")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_bytes(remote.content)
    except OSError:
        pass
    return Response(
        content=remote.content,
        media_type=content_type.split(";", 1)[0],
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _remote_asset_cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    return get_cache_dir() / "remote_assets" / f"{digest}{suffix}"


# --------------------------------------------------------------------------- #
# 缩略图端点
# --------------------------------------------------------------------------- #


def _resolve_local_asset_path(path: str) -> Path | None:
    """解析本地资源路径，复用 get_asset 的安全校验逻辑。

    返回 None 表示路径不合法或不安全。
    """
    if not path:
        return None

    raw_parts = Path(path).parts
    if any(part == ".." for part in raw_parts):
        return None

    mirror_root = get_mirror_root()
    target_path = Path(path)
    if target_path.is_absolute():
        file_path = target_path.resolve()
    else:
        file_path = (mirror_root / path).resolve()

    if not _is_allowed_local_asset(file_path):
        return None

    if not file_path.exists() or not file_path.is_file():
        return None

    return file_path


@router.get("/thumbnail")
def get_thumbnail(
    path: str = Query(..., description="文件路径（绝对路径或 mirror 相对路径）"),
    width: int = Query(
        DEFAULT_THUMBNAIL_WIDTH,
        description=f"缩略图宽度档（像素），允许值：{list(THUMBNAIL_WIDTHS)}",
    ),
):
    """返回本地图片的派生缩略图。

    生成失败时回退到原图，绝不留下灰占位。
    远程图片不走此端点（远程 URL 已在前端归一到合适尺寸档）。
    """
    file_path = _resolve_local_asset_path(path)
    if file_path is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    ext = file_path.suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=403, detail=f"不允许的文件类型: {ext}")

    # 尝试生成缩略图；不支持或失败时回退原图
    if is_supported_source(file_path) and width in THUMBNAIL_WIDTHS:
        thumbnail = get_or_create_thumbnail(file_path, width)
        if thumbnail is not None and thumbnail.exists():
            return FileResponse(
                path=str(thumbnail),
                media_type="image/webp",
                headers={
                    "Cache-Control": "public, max-age=86400",
                },
            )

    # 回退：返回原图
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )
