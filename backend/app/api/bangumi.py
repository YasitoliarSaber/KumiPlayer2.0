"""Bangumi integration API."""

import hashlib
import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import DEFAULT_BANGUMI_USER_AGENT, load_config, save_config
from app.core.credential_store import SECURE_CREDENTIAL_STORE
from app.core.paths import get_cache_dir
from app.integrations.bangumi import (
    AUTH_INVALID,
    FORBIDDEN,
    NETWORK_UNAVAILABLE,
    PROXY_UNAVAILABLE,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMEOUT,
    BangumiAccountSnapshot,
    BangumiClient,
    BangumiError,
    clear_account_snapshot,
    load_account_snapshot,
    save_account_snapshot,
)

router = APIRouter(prefix="/api/integrations/bangumi", tags=["bangumi"])


class TokenRequest(BaseModel):
    access_token: str = Field(min_length=1)
    user_agent: str | None = None


class SubjectSearchRequest(BaseModel):
    keyword: str = Field(min_length=1)
    limit: int = 10
    offset: int = 0
    subject_types: list[int] = Field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _read_bangumi_credential() -> tuple[str, str]:
    """返回 ``(state, effective_token)``：``found`` 时 effective_token 一定是真实 token。

    - config 已 hydrate 出 token → found + token（测试走 config.json）；
    - config 为空但凭据存储启用 → **直接读安全存储拿真实 token**（不依赖
      可能陈旧的 config cache）：读失败 → unavailable + ""；无值 → not_found；
    - 否则 → not_found + ""。

    这样 Credential Manager 暂时不可读后恢复时，verify 无需重启进程即可
    重新使用真实凭据。
    """
    from app.core.config import _credential_storage_enabled
    from app.core.credential_store import CredentialStoreError

    config = load_config()
    if config.bangumi_access_token:
        return "found", config.bangumi_access_token
    if _credential_storage_enabled():
        try:
            token = SECURE_CREDENTIAL_STORE.read("bangumi_access_token")
        except CredentialStoreError:
            return "unavailable", ""
        return ("found", token) if token else ("not_found", "")
    return "not_found", ""


def _session_payload(snapshot: BangumiAccountSnapshot, *, credential_state: str) -> dict:
    """把本地 Credential 三态 + Account Snapshot 组装为 session 响应（0 远程请求）。"""
    user = snapshot.to_public_user()
    if credential_state == "not_found":
        return {
            "credential_state": "not_found",
            "credential_saved": False,
            "auth_status": "unknown",
            "connectivity": "unknown",
            "status": "signed_out",
            "user": None,
            "last_verified_at": snapshot.last_verified_at,
            "last_success_at": snapshot.last_success_at,
            "last_failure_at": snapshot.last_failure_at,
            "last_http_status": snapshot.last_http_status,
            "last_error_code": snapshot.last_error_code,
            "last_error_message": snapshot.last_error_message,
        }
    if credential_state == "unavailable":
        # 凭据存储暂时不可读：不判退出、不清快照、不触发任何删除
        return {
            "credential_state": "unavailable",
            "credential_saved": True,
            "auth_status": snapshot.auth_status,
            "connectivity": "unknown",
            "status": "unavailable",
            "user": user,
            "last_verified_at": snapshot.last_verified_at,
            "last_success_at": snapshot.last_success_at,
            "last_failure_at": snapshot.last_failure_at,
            "last_http_status": snapshot.last_http_status,
            "last_error_code": snapshot.last_error_code,
            "last_error_message": snapshot.last_error_message,
        }
    # found：本地恢复（auth_status/connectivity 来自上次验证快照）
    return {
        "credential_state": "found",
        "credential_saved": True,
        "auth_status": snapshot.auth_status,
        "connectivity": snapshot.connectivity,
        "status": "connected" if user else "available",
        "user": user,
        "last_verified_at": snapshot.last_verified_at,
        "last_success_at": snapshot.last_success_at,
        "last_failure_at": snapshot.last_failure_at,
        "last_http_status": snapshot.last_http_status,
        "last_error_code": snapshot.last_error_code,
        "last_error_message": snapshot.last_error_message,
    }


@router.post("/token")
def set_token(req: TokenRequest):
    """保存 Bangumi token 并验证它；验证成功才落盘并写账户快照。"""
    config = load_config()
    user_agent = DEFAULT_BANGUMI_USER_AGENT
    try:
        me = BangumiClient(access_token=req.access_token, user_agent=user_agent, timeout=12.0).get_me(purpose="manual_verify")
    except BangumiError as e:
        raise _http_error(e) from e
    config.bangumi_access_token = req.access_token
    config.bangumi_user_agent = DEFAULT_BANGUMI_USER_AGENT
    save_config(config)
    now = _now_iso()
    save_account_snapshot(BangumiAccountSnapshot(
        user_id=me.get("id"),
        username=str(me.get("username") or ""),
        nickname=str(me.get("nickname") or ""),
        avatar_url=_avatar_url(me.get("avatar")) or "",
        sign=me.get("sign", ""),
        auth_status="valid",
        connectivity="online",
        last_verified_at=now,
        last_success_at=now,
        last_http_status=200,
    ))
    return {"ok": True, "me": _public_me(me), "config": config.to_public_dict()}


@router.delete("/token")
def clear_token():
    """用户主动退出：清除 token 与本地账户快照（唯一允许清理凭据的路径）。

    ``cleared_keys`` 显式声明清除意图：空值语义默认是 KEEP，只有这里明确
    要求删除 ``bangumi_access_token``（REWORK：绝不隐式 CLEAR 其他凭据）。
    """
    config = load_config()
    config.bangumi_access_token = ""
    save_config(config, cleared_keys={"bangumi_access_token"})
    clear_account_snapshot()
    return {"ok": True}

@router.get("/me")
def get_me():
    try:
        me = BangumiClient(timeout=4.0).get_me(purpose="manual_verify")
    except BangumiError as e:
        raise _http_error(e) from e
    return _public_me(me)


@router.get("/session")
def get_session():
    """恢复本地 Bangumi 会话：只读 Credential + Account Snapshot，**0 个远程请求**。

    网络、429、5xx 或代理故障都不影响本接口。它只回答三件事：凭据是否存在
    （found / not_found / unavailable）、上次验证结果（快照）、最近一次连接
    状态；真正的远程验证走 POST /session/verify。
    """
    credential_state, _token = _read_bangumi_credential()
    snapshot = load_account_snapshot()
    return _session_payload(snapshot, credential_state=credential_state)


@router.post("/session/verify")
def verify_session():
    """显式远程验证：GET /v0/me → 按统一分类更新账户快照 → 返回 session。

    - 成功 → auth_status=valid、connectivity=online、用户资料刷新；
    - 401 → reauth_required（**保留凭据与快照**，UI 提示更新 Access Token）；
    - 403 / 429 / 5xx / timeout / proxy / 网络 → 对应 connectivity 分类，
      一律保留凭据，绝不自动删除、绝不伪装成退出登录。
    """
    credential_state, effective_token = _read_bangumi_credential()
    snapshot = load_account_snapshot()
    if credential_state != "found":
        return _session_payload(snapshot, credential_state=credential_state)

    now = _now_iso()
    try:
        me = BangumiClient(access_token=effective_token, timeout=8.0).get_me(purpose="session_verify")
    except BangumiError as error:
        snapshot.last_failure_at = now
        snapshot.last_http_status = error.status_code or None
        snapshot.last_error_code = error.error_code
        snapshot.last_error_message = str(error)[:500]
        if error.error_code == AUTH_INVALID:
            snapshot.auth_status = "reauth_required"
            snapshot.connectivity = "online"
        elif error.error_code == FORBIDDEN:
            snapshot.connectivity = "forbidden"
        elif error.error_code == RATE_LIMITED:
            snapshot.connectivity = "rate_limited"
        elif error.error_code == SERVER_ERROR:
            snapshot.connectivity = "server_error"
        elif error.error_code in (TIMEOUT, NETWORK_UNAVAILABLE, PROXY_UNAVAILABLE):
            snapshot.connectivity = "offline"
        else:
            snapshot.connectivity = "unknown"
        save_account_snapshot(snapshot)
        return _session_payload(snapshot, credential_state=credential_state)

    snapshot.user_id = me.get("id")
    snapshot.username = me.get("username") or ""
    snapshot.nickname = me.get("nickname") or ""
    snapshot.avatar_url = _avatar_url(me.get("avatar")) or ""
    snapshot.sign = me.get("sign", "") or ""
    snapshot.auth_status = "valid"
    snapshot.connectivity = "online"
    snapshot.last_verified_at = now
    snapshot.last_success_at = now
    snapshot.last_http_status = 200
    snapshot.last_error_code = ""
    snapshot.last_error_message = ""
    save_account_snapshot(snapshot)
    return _session_payload(snapshot, credential_state=credential_state)
@router.get("/avatar")
def get_cached_avatar(url: str):
    """Proxy and cache Bangumi avatar images for the sidebar account card."""
    return _cached_remote_image(url, "bangumi_avatars", "头像")


@router.get("/subject-image")
def get_cached_subject_image(url: str):
    """Proxy and cache Bangumi subject cover images for candidate cards."""
    return _cached_remote_image(url, "bangumi_subject_images", "条目图片")


def _cached_remote_image(url: str, cache_name: str, label: str):
    raw_url = unquote(url).strip()
    # SSRF 防护：仅放行 Bangumi 官方图片域（lain.bgm.tv），拒绝其他任意 URL。
    from app.core.url_guard import validate_bangumi_image_url

    try:
        validate_bangumi_image_url(raw_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label}地址不在受信任域名范围内") from exc
    cache_dir = get_cache_dir() / cache_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()[:24]
    suffix = Path(raw_url.split("?", 1)[0]).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        suffix = ".jpg"
    path = cache_dir / f"{digest}{suffix}"
    if not path.exists():
        try:
            # 拒绝跟随重定向：白名单域内的地址不应跳转到其他主机
            response = httpx.get(raw_url, timeout=10, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{label}下载失败: {exc}") from exc
        path.write_bytes(response.content)
    media_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return Response(
        content=path.read_bytes(),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "ETag": f'"{digest}"',
        },
    )


@router.post("/search")
def search_subjects(req: SubjectSearchRequest):
    try:
        payload = BangumiClient(timeout=12.0).search_subjects(req.keyword, req.limit, req.offset, req.subject_types or None)
    except BangumiError as e:
        raise _http_error(e) from e
    return _public_subject_search(payload)


def _public_me(me: dict) -> dict:
    avatar = _avatar_url(me.get("avatar"))
    return {
        "id": me.get("id"),
        "username": me.get("username"),
        "nickname": me.get("nickname"),
        "avatar": avatar,
        "sign": me.get("sign", ""),
    }


def _public_subject_search(payload):
    if isinstance(payload, list):
        return [_public_subject(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    result = dict(payload)
    for key in ("data", "items", "results"):
        if isinstance(result.get(key), list):
            result[key] = [_public_subject(item) for item in result[key]]
    return result


def _public_subject(subject):
    if not isinstance(subject, dict):
        return subject
    item = dict(subject)
    cover = _subject_cover_url(item.get("images") or item.get("image") or item.get("cover"))
    if cover:
        item["cover"] = cover
    return item


def _subject_cover_url(value) -> str | None:
    if isinstance(value, dict):
        raw = (
            value.get("grid")
            or value.get("common")
            or value.get("large")
            or value.get("medium")
            or value.get("small")
        )
    else:
        raw = value
    if not raw:
        return None
    return f"/api/integrations/bangumi/subject-image?url={quote(str(raw), safe='')}"


def _avatar_url(value) -> str | None:
    if isinstance(value, dict):
        raw = value.get("large") or value.get("medium") or value.get("small") or value.get("grid")
    else:
        raw = value
    if not raw:
        return None
    return f"/api/integrations/bangumi/avatar?url={quote(str(raw), safe='')}"


def _http_error(error: BangumiError) -> HTTPException:
    status = error.status_code or 502
    if status < 400:
        status = 502
    return HTTPException(status_code=status, detail=str(error))
