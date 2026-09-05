"""Assets API 端点

GET /api/assets?path=<path>  返回 mirror 目录下的图片/NFO 文件

安全要求：
- 只允许访问 mirror root 下的文件
- 拒绝 ../ 路径遍历
- 拒绝 mirror root 外的绝对路径
- 拒绝源盘真实视频路径
- 只允许特定扩展名
"""

import asyncio
import dataclasses
import hashlib
import mimetypes
import os
import threading
import time
import uuid
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.core.config import load_config
from app.core.paths import get_cache_dir, get_data_dir, get_mirror_root
from app.core.url_guard import validate_remote_asset_url
from app.media_v4.assets.thumbnails import (
    DEFAULT_THUMBNAIL_WIDTH,
    THUMBNAIL_WIDTHS,
    get_or_create_thumbnail,
    is_supported_source,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])
_REMOTE_FAILURES: dict[str, float] = {}
_REMOTE_FAILURE_TTL = 60.0
# 相同远程 URL 的并发请求共用同一把下载锁：详情页首屏几十个并发消费者
# 只允许产生一次上游下载，其余等待首个请求落盘后直接读缓存。
# 每个条目带等待者计数：只有最后一个离开且锁已释放的请求才能移除条目，
# 避免"旧等待者仍持有旧锁、新请求又建第二把锁"的重复下载竞态。
@dataclasses.dataclass
class _RemoteInflight:
    lock: asyncio.Lock
    waiters: int


_REMOTE_INFLIGHT_LOCKS: dict[str, _RemoteInflight] = {}
_REMOTE_INFLIGHT_GUARD = threading.Lock()


def _remote_inflight_entry(url: str) -> _RemoteInflight:
    with _REMOTE_INFLIGHT_GUARD:
        entry = _REMOTE_INFLIGHT_LOCKS.get(url)
        if entry is None:
            entry = _RemoteInflight(lock=asyncio.Lock(), waiters=0)
            _REMOTE_INFLIGHT_LOCKS[url] = entry
        entry.waiters += 1
        return entry


def _release_inflight_entry(url: str, entry: _RemoteInflight) -> None:
    with _REMOTE_INFLIGHT_GUARD:
        entry.waiters -= 1
        if entry.waiters <= 0 and not entry.lock.locked() and _REMOTE_INFLIGHT_LOCKS.get(url) is entry:
            _REMOTE_INFLIGHT_LOCKS.pop(url, None)

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


def _local_file_etag(path: Path) -> str:
    stat = path.stat()
    return f'"{hashlib.sha256(f"{path.name}-{stat.st_size}-{stat.st_mtime_ns}".encode()).hexdigest()[:32]}"'


def _local_file_response(file_path: Path, content_type: str, *, max_age: int, request_headers) -> Response:
    """本地图片响应：带稳定校验器，条件命中直接 304。"""

    headers = {
        "Cache-Control": f"public, max-age={max_age}",
        "ETag": _local_file_etag(file_path),
        "Last-Modified": _last_modified_header(file_path),
    }
    if _not_modified(request_headers, etag=headers["ETag"], last_modified=headers["Last-Modified"]):
        return Response(status_code=304, headers=headers)
    return FileResponse(path=str(file_path), media_type=content_type, headers=headers)


@router.get("")
def get_asset(
    request: Request,
    path: str = Query(..., description="文件路径（绝对路径或 mirror 相对路径）"),
):
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
    return _local_file_response(
        file_path,
        content_type,
        max_age=3600,
        request_headers=request.headers,
    )


def _validate_remote_asset_url(url: str):
    """兼容别名：共享校验实现位于 :mod:`app.core.url_guard`。"""
    return validate_remote_asset_url(url)


async def _get_remote_asset_client(app, proxy_url: str | None) -> httpx.AsyncClient:
    """复用 HTTP/2 连接，避免详情页每张图片都重新建立代理与 TLS 会话。"""

    state = app.state
    lock = getattr(state, "remote_asset_client_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        state.remote_asset_client_lock = lock

    async with lock:
        client = getattr(state, "remote_asset_client", None)
        active_proxy = getattr(state, "remote_asset_client_proxy", None)
        if client is not None and not client.is_closed and active_proxy == proxy_url:
            return client
        if client is not None and not client.is_closed:
            await client.aclose()

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            proxy=proxy_url,
            http2=True,
            limits=httpx.Limits(
                max_connections=24,
                max_keepalive_connections=12,
                keepalive_expiry=60.0,
            ),
        )
        state.remote_asset_client = client
        state.remote_asset_client_proxy = proxy_url
        return client


async def close_remote_asset_client(app) -> None:
    client = getattr(app.state, "remote_asset_client", None)
    if client is not None and not client.is_closed:
        await client.aclose()
    app.state.remote_asset_client = None
    app.state.remote_asset_client_proxy = None
    app.state.remote_asset_client_lock = None


def _write_cache_atomic(path: Path, payload: bytes) -> str:
    """原子发布图片与摘要，缓存命中无需重新读取整张图片计算 ETag。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    sidecar = path.with_suffix(f"{path.suffix}.etag")
    temporary_sidecar = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256(payload).hexdigest()
    published_sidecar = False
    try:
        temporary.write_bytes(payload)
        temporary_sidecar.write_text(f"{digest}\n", encoding="ascii")
        # 先发布摘要、再发布图片：任何看到缓存图片的请求都能同步看到
        # 对应的 ETag sidecar；首个下载期间则仍被 single-flight 锁保护。
        os.replace(temporary_sidecar, sidecar)
        published_sidecar = True
        os.replace(temporary, path)
    except OSError:
        if published_sidecar and not path.exists():
            sidecar.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
    return digest


def _etag_for_cache_file(path: Path) -> str:
    """缓存文件的稳定强校验器：优先读取下载时记录的摘要。"""

    sidecar = path.with_suffix(f"{path.suffix}.etag")
    if sidecar.exists():
        digest = sidecar.read_text(encoding="ascii", errors="ignore").strip()
        if digest:
            return f'"{digest}"'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        sidecar.write_text(digest, encoding="ascii")
    except OSError:
        pass
    return f'"{digest}"'


def _last_modified_header(path: Path) -> str:
    return formatdate(path.stat().st_mtime, usegmt=True)


def _not_modified(request_headers, *, etag: str, last_modified: str) -> bool:
    if_none_match = request_headers.get("if-none-match")
    if if_none_match:
        candidates = {value.strip() for value in if_none_match.split(",")}
        return etag in candidates or "*" in candidates
    if_modified_since = request_headers.get("if-modified-since")
    if if_modified_since:
        try:
            since = parsedate_to_datetime(if_modified_since).timestamp()
        except (TypeError, ValueError):
            return False
        try:
            modified = parsedate_to_datetime(last_modified).timestamp()
        except (TypeError, ValueError):
            return False
        return modified <= since
    return False


def _image_response(
    *,
    body: bytes | None,
    path: Path | None,
    media_type: str,
    etag: str,
    last_modified: str,
    request_headers,
) -> Response:
    """统一图片响应：条件请求 304，其余 200 并携带可复用缓存头。"""

    headers = {
        "Cache-Control": "public, max-age=86400",
        "ETag": etag,
        "Last-Modified": last_modified,
    }
    if _not_modified(request_headers, etag=etag, last_modified=last_modified):
        return Response(status_code=304, headers=headers)
    if body is not None:
        return Response(content=body, media_type=media_type, headers=headers)
    return FileResponse(path=str(path), media_type=media_type, headers=headers)


@router.get("/remote")
async def proxy_remote_asset(
    request: Request,
    url: str = Query(..., description="Trusted metadata image URL"),
):
    """Proxy trusted metadata artwork without allowing arbitrary outbound requests.

    缓存命中不再访问上游；相同 URL 的并发请求合并为一次上游下载；
    落盘原子完成，失败不残留半截文件。
    """

    try:
        _validate_remote_asset_url(url)
    except ValueError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error

    request_headers = request.headers
    cache_path = _remote_asset_cache_path(url)
    if cache_path.exists():
        return _cache_hit_response(cache_path, request_headers)

    failed_at = _REMOTE_FAILURES.get(url)
    if failed_at and time.monotonic() - failed_at < _REMOTE_FAILURE_TTL:
        raise HTTPException(status_code=502, detail="远程图片暂时不可用")

    entry = _remote_inflight_entry(url)
    try:
        async with entry.lock:
            # 等待期间可能已有同 URL 请求完成下载或写入失败熔断，
            # 直接复用其结果，不重复访问上游。
            if cache_path.exists():
                return _cache_hit_response(cache_path, request_headers)
            failed_at = _REMOTE_FAILURES.get(url)
            if failed_at and time.monotonic() - failed_at < _REMOTE_FAILURE_TTL:
                raise HTTPException(status_code=502, detail="远程图片暂时不可用")
            config = load_config()
            try:
                client = await _get_remote_asset_client(request.app, config.proxy_url or None)
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
            digest = _write_cache_atomic(cache_path, remote.content)
            _REMOTE_FAILURES.pop(url, None)
            return _image_response(
                body=remote.content,
                path=None,
                media_type=content_type.split(";", 1)[0],
                etag=f'"{digest}"',
                last_modified=_last_modified_header(cache_path),
                request_headers=request_headers,
            )
    finally:
        _release_inflight_entry(url, entry)


def _cache_hit_response(cache_path: Path, request_headers) -> Response:
    # 缓存命中走 FileResponse 流式回传，不把整张图片读进 Python 内存；
    # ETag 优先复用下载时记录的摘要 sidecar。
    media_type = mimetypes.guess_type(str(cache_path))[0] or "image/jpeg"
    return _image_response(
        body=None,
        path=cache_path,
        media_type=media_type,
        etag=_etag_for_cache_file(cache_path),
        last_modified=_last_modified_header(cache_path),
        request_headers=request_headers,
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
    request: Request,
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
            return _local_file_response(
                thumbnail,
                "image/webp",
                max_age=86400,
                request_headers=request.headers,
            )

    # 回退：返回原图
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return _local_file_response(
        file_path,
        content_type,
        max_age=3600,
        request_headers=request.headers,
    )
