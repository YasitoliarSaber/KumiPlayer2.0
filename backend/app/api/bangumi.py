# -*- coding: utf-8 -*-
"""Bangumi integration API."""

from pathlib import Path
from typing import Optional
import hashlib
import mimetypes
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import DEFAULT_BANGUMI_USER_AGENT, load_config, save_config
from app.core.paths import get_cache_dir
from app.integrations.bangumi import (
    EPISODE_COLLECTION_DONE,
    SUBJECT_COLLECTION_DOING,
    BangumiClient,
    BangumiEpisodeSync,
    BangumiError,
    BangumiMatch,
    delete_match,
    get_match,
    list_local_episodes,
    load_state,
    record_episode_sync,
    resolve_bangumi_episode_id,
    resolve_episode,
    resolve_work,
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


@router.post("/token")
def set_token(req: TokenRequest):
    """Save Bangumi token and verify it by loading current user."""
    config = load_config()
    user_agent = DEFAULT_BANGUMI_USER_AGENT
    try:
        me = BangumiClient(access_token=req.access_token, user_agent=user_agent, timeout=12.0).get_me()
    except BangumiError as e:
        raise _http_error(e)
    config.bangumi_access_token = req.access_token
    config.bangumi_user_agent = DEFAULT_BANGUMI_USER_AGENT
    save_config(config)
    return {"ok": True, "me": _public_me(me), "config": config.to_public_dict()}


@router.delete("/token")
def clear_token():
    config = load_config()
    config.bangumi_access_token = ""
    save_config(config)
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
    """恢复已保存的登录状态，不把临时网络故障误报为退出登录。"""
    config = load_config()
    if not config.bangumi_access_token:
        return {
            "credential_saved": False,
            "status": "signed_out",
            "user": None,
            "message": "",
        }

    try:
        me = BangumiClient(timeout=4.0).get_me()
    except BangumiError as error:
        invalid = error.status_code in {401, 403}
        return {
            "credential_saved": True,
            "status": "invalid" if invalid else "unavailable",
            "user": None,
            "message": str(error),
        }

    return {
        "credential_saved": True,
        "status": "connected",
        "user": _public_me(me),
        "message": "",
    }


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
