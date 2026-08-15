# -*- coding: utf-8 -*-
"""Bangumi integration API."""

import hashlib
import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import DEFAULT_BANGUMI_USER_AGENT, load_config, save_config
from app.core.credential_store import SECURE_CREDENTIAL_STORE
from app.core.paths import get_cache_dir
from app.integrations.bangumi import (
    AUTH_INVALID,
    EPISODE_COLLECTION_DONE,
    FORBIDDEN,
    NETWORK_UNAVAILABLE,
    PROXY_UNAVAILABLE,
    RATE_LIMITED,
    SERVER_ERROR,
    SUBJECT_COLLECTION_DOING,
    TIMEOUT,
    BangumiAccountSnapshot,
    BangumiClient,
    BangumiEpisodeSync,
    BangumiError,
    BangumiMatch,
    clear_account_snapshot,
    delete_match,
    get_match,
    list_local_episodes,
    load_account_snapshot,
    load_state,
    record_episode_sync,
    resolve_bangumi_episode_id,
    resolve_episode,
    resolve_work,
    save_account_snapshot,
    sync_bidirectional_progress,
    upsert_match,
)

router = APIRouter(prefix="/api/integrations/bangumi", tags=["bangumi"])


class TokenRequest(BaseModel):
    access_token: str = Field(min_length=1)
    user_agent: Optional[str] = None


class SubjectSearchRequest(BaseModel):
    keyword: str = Field(min_length=1)
    limit: int = 10
    offset: int = 0
    subject_types: list[int] = Field(default_factory=list)


class MatchRequest(BaseModel):
    subject_id: int
    season_number: Optional[int] = None
    subject_name: str = ""
    subject_name_cn: str = ""


class CollectionPatch(BaseModel):
    season_number: Optional[int] = None
    type: int = SUBJECT_COLLECTION_DOING


class EpisodeWatchedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_id: str = ""
    season_number: Optional[int] = None
    bangumi_episode_id: Optional[int] = None
    type: int = EPISODE_COLLECTION_DONE


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _read_credential_state() -> tuple[str, str]:
    """Bangumi token 三态：``found`` / ``not_found`` / ``unavailable``。

    优先使用 ``load_config`` 已 hydrate 的 token（生产走 Credential Manager、
    测试走 config.json）；token 为空且凭据存储启用时再查安全存储，区分
    「凭据不存在」与「凭据存储不可读」——两者绝不能都表现为退出登录。
    """
    from app.core.config import _credential_storage_enabled

    config = load_config()
    if config.bangumi_access_token:
        return "found", config.bangumi_access_token
    if _credential_storage_enabled():
        return SECURE_CREDENTIAL_STORE.read_state("bangumi_access_token"), ""
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
        me = BangumiClient(access_token=req.access_token, user_agent=user_agent, timeout=12.0).get_me()
    except BangumiError as e:
        raise _http_error(e)
    config.bangumi_access_token = req.access_token
    config.bangumi_user_agent = DEFAULT_BANGUMI_USER_AGENT
    save_config(config)
    now = _now_iso()
    save_account_snapshot(BangumiAccountSnapshot(
        user_id=me.get("id"),
        username=me.get("username"),
        nickname=me.get("nickname"),
        avatar_url=_avatar_url(me.get("avatar")),
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
    """用户主动退出：清除 token 与本地账户快照（唯一允许清理凭据的路径）。"""
    config = load_config()
    config.bangumi_access_token = ""
    save_config(config)
    clear_account_snapshot()
    return {"ok": True}

@router.get("/me")
def get_me():
    try:
        me = BangumiClient(timeout=4.0).get_me()
    except BangumiError as e:
        raise _http_error(e)
    return _public_me(me)


@router.get("/session")
def get_session():
    """恢复本地 Bangumi 会话：只读 Credential + Account Snapshot，**0 个远程请求**。

    网络、429、5xx 或代理故障都不影响本接口。它只回答三件事：凭据是否存在
    （found / not_found / unavailable）、上次验证结果（快照）、最近一次连接
    状态；真正的远程验证走 POST /session/verify。
    """
    credential_state, _token = _read_credential_state()
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
    credential_state, _token = _read_credential_state()
    snapshot = load_account_snapshot()
    if credential_state != "found":
        return _session_payload(snapshot, credential_state=credential_state)

    now = _now_iso()
    try:
        me = BangumiClient(timeout=8.0).get_me()
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
    if not raw_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail=f"{label}地址无效")
    cache_dir = get_cache_dir() / cache_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()[:24]
    suffix = Path(raw_url.split("?", 1)[0]).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        suffix = ".jpg"
    path = cache_dir / f"{digest}{suffix}"
    if not path.exists():
        try:
            response = httpx.get(raw_url, timeout=10, follow_redirects=True)
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
        raise _http_error(e)
    return _public_subject_search(payload)


@router.get("/matches/{work_id}")
def get_confirmed_match(work_id: str, season_number: Optional[int] = None):
    try:
        resolve_work(work_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    match = get_match(work_id, season_number)
    if match is None:
        raise HTTPException(status_code=404, detail="尚未确认 Bangumi 匹配")
    return _match_payload(match)


@router.post("/matches/{work_id}")
def confirm_match(work_id: str, req: MatchRequest):
    try:
        resolve_work(work_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    match = upsert_match(BangumiMatch(
        work_id=work_id,
        season_number=req.season_number,
        subject_id=req.subject_id,
        subject_name=req.subject_name,
        subject_name_cn=req.subject_name_cn,
    ))
    payload = _match_payload(match)

    # 双向同步：拉取网站进度 + 上传本地已完成但未同步的剧集
    sync_result = sync_bidirectional_progress(work_id, req.season_number)
    payload["sync"] = sync_result

    return payload


@router.delete("/matches/{work_id}")
def remove_match(work_id: str, season_number: Optional[int] = None):
    deleted = delete_match(work_id, season_number)
    return {"ok": deleted, "work_id": work_id, "season_number": season_number}


@router.patch("/collections/{work_id}")
def set_subject_collection(work_id: str, req: CollectionPatch):
    match = get_match(work_id, req.season_number)
    if match is None:
        raise HTTPException(status_code=409, detail="请先确认 Bangumi 条目匹配")
    try:
        payload = BangumiClient().set_collection(match.subject_id, req.type)
    except BangumiError as e:
        raise _http_error(e)
    return {"ok": True, "subject_id": match.subject_id, "type": req.type, "bangumi": payload}


@router.get("/collections/{work_id}")
def get_subject_collection(work_id: str, season_number: Optional[int] = None):
    match = get_match(work_id, season_number)
    if match is None:
        raise HTTPException(status_code=409, detail="请先确认 Bangumi 条目匹配")
    client = BangumiClient(timeout=6.0)
    try:
        me = client.get_me()
        username = me.get("username") or str(me.get("id") or "-")
        payload = client.get_collection(username, match.subject_id)
    except BangumiError as e:
        raise _http_error(e)
    return {
        "ok": True,
        "subject_id": match.subject_id,
        "match_season_number": match.season_number,
        "season_number": season_number,
        "bangumi": payload,
    }


@router.get("/episodes/{work_id}")
def get_episode_mapping(work_id: str, season_number: Optional[int] = None):
    try:
        episodes = list_local_episodes(work_id, season_number)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    match = get_match(work_id, season_number)
    state = load_state()
    synced = {item.local_episode_id: item for item in state.episode_sync if item.status == "succeeded"}
    return {
        "work_id": work_id,
        "season_number": season_number,
        "match": _match_payload(match) if match else None,
        "match_season_number": match.season_number if match else None,
        "episodes": [
            {
                "episode_id": episode.episode_id,
                "season_number": episode.season_number,
                "episode_number": episode.episode_number,
                "title": episode.title,
                "bangumi_episode_id": match.episode_map.get(episode.episode_id) if match else None,
                "synced": episode.episode_id in synced,
                "synced_at": synced[episode.episode_id].synced_at if episode.episode_id in synced else "",
            }
            for episode in episodes
        ],
    }


@router.put("/episodes/{episode_id}/watched")
def mark_episode_watched(episode_id: str, req: EpisodeWatchedRequest):
    try:
        work, episode = resolve_episode(episode_id, req.work_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    season_number = req.season_number
    if season_number is None:
        season_number = episode.season_number
    match = get_match(work.work_id, season_number)
    if match is None:
        season_label = f"当前季 (S{season_number})" if season_number is not None else "当前作品"
        raise HTTPException(status_code=409, detail=f"{season_label}尚未匹配 Bangumi 条目，请在详情页的 Bangumi 面板中搜索匹配")

    client = BangumiClient()
    try:
        bangumi_episode_id = resolve_bangumi_episode_id(client, match, episode, req.bangumi_episode_id)
        payload = client.set_episode_collection(bangumi_episode_id, req.type)
    except BangumiError as e:
        sync = BangumiEpisodeSync(
            local_episode_id=episode.episode_id,
            bangumi_episode_id=req.bangumi_episode_id or 0,
            work_id=work.work_id,
            season_number=season_number,
            subject_id=match.subject_id,
            type=req.type,
            status="failed",
            error=str(e),
        )
        record_episode_sync(sync)
        raise _http_error(e)
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e))

    sync = BangumiEpisodeSync(
        local_episode_id=episode.episode_id,
        bangumi_episode_id=bangumi_episode_id,
        work_id=work.work_id,
        season_number=season_number,
        subject_id=match.subject_id,
        type=req.type,
    )
    record_episode_sync(sync)
    return {
        "ok": True,
        "work_id": work.work_id,
        "episode_id": episode.episode_id,
        "season_number": season_number,
        "subject_id": match.subject_id,
        "bangumi_episode_id": bangumi_episode_id,
        "type": req.type,
        "bangumi": payload,
    }


def _match_payload(match: BangumiMatch) -> dict:
    return {
        "work_id": match.work_id,
        "season_number": match.season_number,
        "subject_id": match.subject_id,
        "subject_name": match.subject_name,
        "subject_name_cn": match.subject_name_cn,
        "confirmed_at": match.confirmed_at,
        "updated_at": match.updated_at,
        "episode_map": match.episode_map,
    }


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


def _subject_cover_url(value) -> Optional[str]:
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


def _avatar_url(value) -> Optional[str]:
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


class ProgressSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    season_number: Optional[int] = None


@router.post("/progress/{work_id}/sync")
def sync_progress(work_id: str, req: ProgressSyncRequest):
    """统一双向同步：拉取网站进度 + 上传本地进度。

    详情页应在 Bangumi 连接后调用此接口，等返回后再读取本地进度。
    """
    try:
        resolve_work(work_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    result = sync_bidirectional_progress(work_id, req.season_number)
    if result["status"] == "unmatched":
        raise HTTPException(status_code=409, detail="当前季度尚未匹配 Bangumi 条目")
    return result
