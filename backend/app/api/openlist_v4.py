"""V4 OpenList 连接、浏览与来源路由接口。

OpenList 在 V4 中只是 SourceEvidence 的一个来源适配器：本模块负责连接
设置和受控的单层目录浏览。
"""

from __future__ import annotations

import copy
import ipaddress
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.core.config import load_config, resolve_openlist_credentials, save_config
from app.core.credential_store import CredentialStoreError
from app.integrations.openlist.cache import connection_key, read_cache, write_cache
from app.integrations.openlist.client import (
    OpenListClient,
    clear_openlist_client_pool,
    get_openlist_client,
    normalize_openlist_server_url,
    normalize_remote_path,
    validate_server_url,
)
from app.integrations.openlist.connection import probe_openlist_connection
from app.integrations.openlist.governor import governor_connection_key
from app.integrations.openlist.models import OpenListError
from app.integrations.openlist.providers import (
    PROVIDER_OTHER,
    ROUTABLE_PROVIDERS,
    OpenListRouteConfig,
    derive_local_path,
    hint_provider_for_name,
    is_ancestor_or_self,
    new_route_id,
    normalize_route_prefix,
)
from app.media_v4.sources import health as source_health

router = APIRouter(prefix="/api/openlist", tags=["openlist"])

_DEFAULT_PER_PAGE = 100
_MAX_PER_PAGE = 100
_MAX_PREFETCH = 50
_MAX_DISCOVER_ENTRIES = 1000
_NOT_CONFIGURED = "尚未配置 OpenList 连接，请先到设置页完成配置"


class TestConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_url: str = ""
    remote_root: str = ""
    username: str = ""
    password: str = ""
    allow_insecure_http: bool = False


class SaveConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_url: str = ""
    remote_root: str = ""
    mount_root: str = ""
    username: str = ""
    password: str = ""
    allow_insecure_http: bool = False
    cache_ttl_minutes: int | None = None
    prefetch_limit: int | None = None
    skip_verification: bool = False


class PrefetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = []


class RouteItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str = ""
    label: str = ""
    remote_prefix: str = ""
    provider_id: str = PROVIDER_OTHER
    enabled: bool = True


class SaveRoutesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: list[RouteItem]


def _is_loopback_http(url: str) -> bool:
    try:
        host = (urlsplit(url or "").hostname or "").lower().rstrip(".").lstrip("[")
    except ValueError:
        return False
    if host.rstrip("]") in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host.rstrip("]")).is_loopback
    except ValueError:
        return False


def _credentials() -> tuple[str, str]:
    username, password, state = resolve_openlist_credentials()
    if state == "unavailable":
        raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，请稍后重试")
    if not username or not password:
        raise HTTPException(status_code=400, detail=_NOT_CONFIGURED)
    return username, password


def _client(config, username: str | None = None, password: str | None = None) -> OpenListClient:
    user, pwd = (
        (username, password)
        if username is not None and password is not None
        else _credentials()
    )
    if not config.openlist_server_url or not user or not pwd:
        raise HTTPException(status_code=400, detail=_NOT_CONFIGURED)
    try:
        return get_openlist_client(
            config.openlist_server_url,
            user,
            pwd,
            client_factory=OpenListClient,
        )
    except OpenListError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _remote_root(config) -> str:
    try:
        return normalize_remote_path(config.openlist_remote_root or "/")
    except OpenListError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _ensure_within_root(root: str, path: str) -> None:
    if not is_ancestor_or_self(root, path):
        raise HTTPException(status_code=400, detail="选择的远端目录不在映射根路径之下")


def _page_payload(
    client: OpenListClient,
    path: str,
    page: int,
    per_page: int,
    *,
    refresh: bool,
) -> dict:
    try:
        result = client.list_dir(path, page=page, per_page=per_page, refresh=refresh)
    except OpenListError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    entries = [
        {
            "name": item.name,
            "is_dir": item.is_dir,
            "size": item.size,
            "modified": item.modified,
            "remote_path": item.remote_path,
        }
        for item in result.entries
    ]
    total = int(result.total or 0)
    has_more = page * per_page < total if total else len(entries) == per_page
    return {"entries": entries, "total": total, "has_more": has_more}


def _browse_response(
    path: str,
    root: str,
    entries: list[dict],
    *,
    page: int,
    per_page: int,
    total: int,
    has_more: bool,
    cache: dict,
) -> dict:
    parent = None if path == root or path == "/" else str(PurePosixPath(path).parent)
    return {
        "path": path,
        "parent_path": parent,
        "remote_root": root,
        "entries": entries,
        "page": page,
        "per_page": per_page,
        "total": total,
        "has_more": has_more,
        "truncated": False,
        "cache": cache,
    }


def _route_public(route: OpenListRouteConfig, config) -> dict:
    local_path = ""
    local_available = False
    mount_root = (config.openlist_mount_root or "").strip()
    if mount_root:
        try:
            local_path = derive_local_path(mount_root, _remote_root(config), route.remote_prefix)
            local_available = Path(local_path).expanduser().is_dir()
        except (OSError, ValueError, HTTPException):
            local_path = ""
    return {
        "route_id": route.route_id,
        "label": route.label,
        "remote_prefix": route.remote_prefix,
        "provider_id": route.provider_id,
        "enabled": route.enabled,
        "local_path": local_path,
        "local_available": local_available,
    }


def _configured_routes(config) -> list[OpenListRouteConfig]:
    return [
        item for item in (config.openlist_routes or [])
        if isinstance(item, OpenListRouteConfig)
    ]


@router.post("/test-connection")
def test_connection(req: TestConnectionRequest):
    config = load_config()
    server_url = (req.server_url or config.openlist_server_url).strip()
    saved_username, saved_password, _state = resolve_openlist_credentials()
    username = req.username.strip() or saved_username
    password = req.password or saved_password
    if req.username.strip() and req.username.strip() != saved_username and not req.password:
        return {
            "ok": False,
            "code": "invalid_configuration",
            "phase": "validation",
            "message": "请输入新 OpenList 账号对应的密码",
        }
    if not server_url or not username or not password:
        return {
            "ok": False,
            "code": "not_configured",
            "phase": "validation",
            "message": _NOT_CONFIGURED,
        }
    result = probe_openlist_connection(
        server_url=server_url,
        remote_root=req.remote_root or config.openlist_remote_root or "/",
        username=username,
        password=password,
        allow_insecure_http=req.allow_insecure_http,
    )
    payload = {
        "ok": result.ok,
        "code": result.code,
        "phase": result.phase,
        "message": result.message,
    }
    if not result.ok and result.code == "invalid_configuration" and "明文传输密码" in result.message:
        payload["insecure_http_required"] = True
    return payload


@router.post("/config")
def save_connection_config(req: SaveConfigRequest):
    ok, reason = validate_server_url(req.server_url)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    if (
        req.server_url.lower().startswith("http://")
        and not _is_loopback_http(req.server_url)
        and not req.allow_insecure_http
    ):
        raise HTTPException(status_code=400, detail="本地/局域网 HTTP 将以明文传输密码，请确认风险后保存")
    try:
        remote_root = normalize_remote_path(req.remote_root or "/")
    except OpenListError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    mount_root = req.mount_root.strip()
    if len(mount_root) == 2 and mount_root[1] == ":":
        mount_root += "\\"
    if not mount_root:
        raise HTTPException(status_code=400, detail="请填写 OpenList 对应的本地挂载根路径")

    config = load_config()
    old_username, old_password, credential_state = resolve_openlist_credentials()
    if credential_state == "unavailable":
        raise HTTPException(status_code=503, detail="本机凭据管理器暂时不可用，无法安全保存配置")
    username = req.username.strip() or old_username
    password = req.password or old_password
    if req.username.strip() and req.username.strip() != old_username and not req.password:
        raise HTTPException(status_code=400, detail="请输入新 OpenList 账号对应的密码")
    if not username or not password:
        raise HTTPException(status_code=400, detail=_NOT_CONFIGURED)
    server_url = normalize_openlist_server_url(req.server_url)
    old_server = normalize_openlist_server_url(config.openlist_server_url) if config.openlist_server_url else ""
    old_root = _remote_root(config)
    remote_changed = (
        server_url != old_server
        or remote_root != old_root
        or username != old_username
        or bool(req.password and req.password != old_password)
    )

    if remote_changed and not req.skip_verification:
        probe = probe_openlist_connection(
            server_url=server_url,
            remote_root=remote_root,
            username=username,
            password=password,
            allow_insecure_http=req.allow_insecure_http,
        )
        if not probe.ok:
            raise HTTPException(status_code=400, detail=probe.message)

    candidate = copy.deepcopy(config)
    candidate.openlist_server_url = server_url
    candidate.openlist_remote_root = remote_root
    candidate.openlist_mount_root = mount_root
    candidate.openlist_username = username
    candidate.openlist_password = password
    if req.cache_ttl_minutes is not None:
        candidate.openlist_cache_ttl_minutes = max(1, min(int(req.cache_ttl_minutes), 60 * 24 * 30))
    if req.prefetch_limit is not None:
        candidate.openlist_prefetch_limit = max(0, min(int(req.prefetch_limit), _MAX_PREFETCH))
    if remote_changed:
        candidate.openlist_routes = []
    try:
        save_config(candidate)
    except CredentialStoreError:
        raise HTTPException(status_code=500, detail="OpenList 配置保存失败，本机凭据服务异常") from None
    if remote_changed:
        clear_openlist_client_pool()
    message = "OpenList 连接配置已保存"
    if req.skip_verification and remote_changed:
        message += "（未验证，请稍后点击检查连接）"
    return {"ok": True, "message": message}


@router.get("/browse")
def browse(path: str = "", page: int = 1, per_page: int = _DEFAULT_PER_PAGE, refresh: bool = False):
    if page < 1 or per_page < 1 or per_page > _MAX_PER_PAGE:
        raise HTTPException(status_code=400, detail="page 必须大于等于 1，per_page 必须在 1 到 100 之间")
    config = load_config()
    root = _remote_root(config)
    try:
        normalized = normalize_remote_path(path or root)
    except OpenListError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _ensure_within_root(root, normalized)
    username, password = _credentials()
    conn_key = connection_key(config.openlist_server_url, username, root)
    allowed, _health = source_health.peek_request_allowed(
        governor_connection_key(config.openlist_server_url, username)
    )
    cached = read_cache(conn_key, normalized, page=page, per_page=per_page)
    if not refresh and cached is not None and cached["fresh"]:
        return _browse_response(
            normalized,
            root,
            cached["entries"],
            page=cached["page"],
            per_page=cached["per_page"],
            total=cached["total"],
            has_more=cached["has_more"],
            cache={
                "cached": True,
                "status": "fresh",
                "refreshing": False,
                "refresh_failed": False,
                "fetched_at": cached["fetched_at"],
                "expires_at": cached["expires_at"],
            },
        )
    if not allowed:
        raise HTTPException(status_code=423, detail="远端网盘疑似触发访问保护，KumiPlayer 已暂停该来源的自动请求")
    try:
        payload = _page_payload(
            _client(config, username, password),
            normalized,
            page,
            per_page,
            refresh=refresh,
        )
    except HTTPException as exc:
        if cached is not None and not refresh:
            return _browse_response(
                normalized,
                root,
                cached["entries"],
                page=cached["page"],
                per_page=cached["per_page"],
                total=cached["total"],
                has_more=cached["has_more"],
                cache={
                    "cached": True,
                    "status": "stale",
                    "refreshing": False,
                    "refresh_failed": True,
                    "error": str(exc.detail),
                    "fetched_at": cached["fetched_at"],
                    "expires_at": cached["expires_at"],
                },
            )
        raise
    ttl = max(1, int(config.openlist_cache_ttl_minutes or 1440))
    write_cache(
        conn_key,
        normalized,
        payload["entries"],
        ttl,
        page=page,
        per_page=per_page,
        total=payload["total"],
        has_more=payload["has_more"],
    )
    result = _browse_response(
        normalized,
        root,
        payload["entries"],
        page=page,
        per_page=per_page,
        total=payload["total"],
        has_more=payload["has_more"],
        cache={
            "cached": False,
            "status": "none",
            "refreshing": False,
            "refresh_failed": False,
            "fetched_at": None,
            "expires_at": None,
        },
    )
    if refresh:
        result["refresh_requested"] = True
    return result


@router.post("/prefetch")
def prefetch(req: PrefetchRequest):
    config = load_config()
    limit = max(0, min(int(config.openlist_prefetch_limit or 12), _MAX_PREFETCH))
    if limit <= 0 or not req.paths:
        return {"prefetched": 0, "skipped": len(req.paths), "busy": False, "cancelled": True}
    root = _remote_root(config)
    username, password = _credentials()
    conn_key = connection_key(config.openlist_server_url, username, root)
    paths: list[str] = []
    for raw in req.paths[:limit]:
        try:
            path = normalize_remote_path(raw)
        except OpenListError:
            continue
        if is_ancestor_or_self(root, path) and path not in paths:
            paths.append(path)
    prefetched = 0
    skipped = 0
    client = None
    ttl = max(1, int(config.openlist_cache_ttl_minutes or 1440))
    for path in paths:
        cached = read_cache(conn_key, path, page=1, per_page=_DEFAULT_PER_PAGE)
        if cached is not None and cached["fresh"]:
            skipped += 1
            continue
        try:
            client = client or _client(config, username, password)
            payload = _page_payload(client, path, 1, _DEFAULT_PER_PAGE, refresh=False)
            write_cache(
                conn_key,
                path,
                payload["entries"],
                ttl,
                page=1,
                per_page=_DEFAULT_PER_PAGE,
                total=payload["total"],
                has_more=payload["has_more"],
            )
            prefetched += 1
        except Exception:
            skipped += 1
    return {"prefetched": prefetched, "skipped": skipped, "busy": False, "cancelled": False}


@router.get("/routes")
def get_routes():
    config = load_config()
    return {"routes": [_route_public(route, config) for route in _configured_routes(config)]}


@router.post("/routes/discover")
def discover_routes():
    config = load_config()
    root = _remote_root(config)
    client = _client(config)
    entries: list[dict[str, Any]] = []
    page = 1
    while len(entries) < _MAX_DISCOVER_ENTRIES:
        try:
            result = client.list_dir(root, page=page, per_page=_DEFAULT_PER_PAGE, refresh=False)
        except OpenListError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        entries.extend(
            {"name": item.name, "is_dir": item.is_dir, "remote_path": item.remote_path}
            for item in result.entries
        )
        if (
            not result.entries
            or len(result.entries) < _DEFAULT_PER_PAGE
            or (result.total and len(entries) >= result.total)
        ):
            break
        page += 1
    existing = {route.remote_prefix: route for route in _configured_routes(config)}
    items = []
    for entry in entries[:_MAX_DISCOVER_ENTRIES]:
        if not entry["is_dir"]:
            continue
        prefix = normalize_route_prefix(entry["remote_path"])
        known = existing.get(prefix)
        items.append(
            {
                "name": entry["name"],
                "remote_prefix": prefix,
                "hint_provider": hint_provider_for_name(entry["name"]),
                "current_provider": known.provider_id if known else "",
                "current_label": known.label if known else "",
            }
        )
    return {"remote_root": root, "items": items}


@router.put("/routes")
def save_routes(req: SaveRoutesRequest):
    config = load_config()
    root = _remote_root(config)
    routes: list[OpenListRouteConfig] = []
    seen: set[str] = set()
    for item in req.routes:
        try:
            prefix = normalize_route_prefix(item.remote_prefix)
        except (ValueError, OpenListError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not is_ancestor_or_self(root, prefix):
            raise HTTPException(status_code=400, detail=f"路由前缀 {prefix} 不在远端总根之下")
        if prefix in seen:
            raise HTTPException(status_code=400, detail=f"远端前缀重复配置：{prefix}")
        provider_id = item.provider_id or PROVIDER_OTHER
        if provider_id not in ROUTABLE_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"未知内容提供商：{provider_id}")
        seen.add(prefix)
        routes.append(
            OpenListRouteConfig(
                route_id=item.route_id or new_route_id(),
                label=item.label.strip() or PurePosixPath(prefix).name or "未命名来源",
                remote_prefix=prefix,
                provider_id=provider_id,
                enabled=bool(item.enabled),
            )
        )
    candidate = copy.deepcopy(config)
    candidate.openlist_routes = routes
    save_config(candidate)
    return {"routes": [_route_public(route, candidate) for route in routes]}


@router.get("/telemetry/today")
def openlist_telemetry_today():
    from app.integrations.openlist.telemetry import daily_summary

    config = load_config()
    username, _password, state = resolve_openlist_credentials()
    if state != "found" or not config.openlist_server_url or not username:
        return {
            "fs_list": 0,
            "login": 0,
            "total": 0,
            "disclaimer": (
                "这是 KumiPlayer → OpenList 的请求次数（访问风险参考值）；"
                "OpenList 可能命中自身缓存，因此实际网盘上游请求可能更少。"
            ),
        }
    return daily_summary(governor_connection_key(config.openlist_server_url, username))
