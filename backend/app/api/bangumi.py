"""Bangumi integration API."""

import hashlib
import json
import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.media_v4 import get_database
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


class MatchRequest(BaseModel):
    subject_id: int = Field(gt=0)
    season_number: int | None = Field(default=None, ge=0)
    subject_name: str = ""
    subject_name_cn: str = ""


class CollectionPatch(BaseModel):
    season_number: int | None = Field(default=None, ge=0)
    type: int = Field(default=SUBJECT_COLLECTION_DOING, ge=1, le=5)


class EpisodeWatchedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_id: str = Field(min_length=1)
    season_number: int | None = Field(default=None, ge=0)
    bangumi_episode_id: int | None = Field(default=None, gt=0)
    type: int = Field(default=EPISODE_COLLECTION_DONE, ge=1, le=3)


class ProgressSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    season_number: int | None = Field(default=None, ge=0)


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


def _season_key(season_number: int | None) -> int:
    return int(season_number or 0)


def _ensure_work_and_season(work_id: str, season_number: int | None, *, database=None) -> tuple[dict, int]:
    """只允许活动 V4 Work/Season 使用 Bangumi；绝不回退 Legacy 媒体库。"""

    requested_season = _season_key(season_number)
    with (database or get_database()).connect() as conn:
        work = conn.execute(
            """
            SELECT w.work_id, w.work_type
            FROM works w
            WHERE w.work_id = ?
              AND EXISTS (
                  SELECT 1 FROM revision_bindings rb
                  JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                  JOIN source_roots sr ON sr.root_id = ir.root_id
                  WHERE rb.work_id = w.work_id AND ir.status = 'confirmed' AND sr.retired_at = ''
              )
            """,
            (work_id,),
        ).fetchone()
        if work is None:
            raise HTTPException(status_code=404, detail="作品不存在或当前来源已退役")
        if requested_season and not conn.execute(
            "SELECT 1 FROM seasons WHERE work_id = ? AND local_season_number = ? LIMIT 1",
            (work_id, requested_season),
        ).fetchone():
            raise HTTPException(status_code=409, detail="当前作品不存在所选季度")
    return dict(work), requested_season


def _local_episodes(work_id: str, season_number: int | None, *, database=None) -> list[dict]:
    clauses = ["e.work_id = ?", "e.episode_kind != 'auxiliary'"]
    params: list[object] = [work_id]
    if _season_key(season_number):
        clauses.append("s.local_season_number = ?")
        params.append(_season_key(season_number))
    with (database or get_database()).connect() as conn:
        rows = conn.execute(
            """
            SELECT e.episode_id, e.local_episode_number, e.display_title,
                   s.local_season_number
            FROM episodes e
            JOIN seasons s ON s.season_id = e.season_id
            WHERE """ + " AND ".join(clauses) + " ORDER BY s.local_season_number, e.local_episode_number, e.episode_id",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _match_payload(row: dict) -> dict:
    try:
        episode_map = json.loads(row.get("episode_map_json") or "{}")
    except (TypeError, ValueError):
        episode_map = {}
    return {
        "work_id": row["work_id"],
        "season_number": int(row["season_number"]) or None,
        "subject_id": int(row["subject_id"]),
        "subject_name": row.get("subject_name") or "",
        "subject_name_cn": row.get("subject_name_cn") or "",
        "confirmed_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
        "episode_map": {str(key): int(value) for key, value in episode_map.items() if str(value).isdigit()},
    }


def _get_match(work_id: str, season_number: int | None, *, database=None) -> dict | None:
    with (database or get_database()).connect() as conn:
        row = conn.execute(
            "SELECT * FROM bangumi_matches WHERE work_id = ? AND season_number = ?",
            (work_id, _season_key(season_number)),
        ).fetchone()
    return dict(row) if row else None


def _remote_episode_numbers(client: BangumiClient, subject_id: int) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in client.list_subject_episodes(subject_id):
        raw_number = item.get("ep")
        raw_episode_id = item.get("id")
        if raw_number is None or raw_episode_id is None:
            continue
        try:
            number = int(raw_number)
            episode_id = int(raw_episode_id)
        except (TypeError, ValueError):
            continue
        if number > 0 and episode_id > 0 and int(item.get("type") or 0) == 0:
            result[number] = episode_id
    return result


def _episode_map_for_match(
    work_id: str,
    season_number: int | None,
    client: BangumiClient,
    subject_id: int,
    *,
    database=None,
) -> dict[str, int]:
    remote_numbers = _remote_episode_numbers(client, subject_id)
    return {
        str(item["episode_id"]): remote_numbers[int(item["local_episode_number"])]
        for item in _local_episodes(work_id, season_number, database=database)
        if item.get("local_episode_number") is not None
        and int(item["local_episode_number"]) in remote_numbers
    }


def _save_match(
    work_id: str,
    season_number: int | None,
    subject_id: int,
    subject_name: str,
    subject_name_cn: str,
    episode_map: dict[str, int],
    *,
    database=None,
) -> dict:
    now = _now_iso()
    with (database or get_database()).connect() as conn:
        conn.execute(
            """
            INSERT INTO bangumi_matches(
                work_id, season_number, subject_id, subject_name, subject_name_cn,
                episode_map_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id, season_number) DO UPDATE SET
                subject_id = excluded.subject_id,
                subject_name = excluded.subject_name,
                subject_name_cn = excluded.subject_name_cn,
                episode_map_json = excluded.episode_map_json,
                updated_at = excluded.updated_at
            """,
            (
                work_id, _season_key(season_number), subject_id, subject_name, subject_name_cn,
                json.dumps(episode_map, ensure_ascii=False, sort_keys=True), now, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM bangumi_matches WHERE work_id = ? AND season_number = ?",
            (work_id, _season_key(season_number)),
        ).fetchone()
    return dict(row)


@router.get("/matches/{work_id}")
def get_confirmed_match(work_id: str, season_number: int | None = None):
    _ensure_work_and_season(work_id, season_number)
    match = _get_match(work_id, season_number)
    if match is None:
        raise HTTPException(status_code=404, detail="尚未确认 Bangumi 匹配")
    return _match_payload(match)


@router.post("/matches/{work_id}")
def confirm_match(work_id: str, req: MatchRequest):
    _ensure_work_and_season(work_id, req.season_number)
    try:
        episode_map = _episode_map_for_match(work_id, req.season_number, BangumiClient(timeout=10.0), req.subject_id)
    except BangumiError:
        # 条目匹配是用户明确操作；网络暂不可用时保存其选择，稍后同步会补全剧集映射。
        episode_map = {}
    return _match_payload(_save_match(
        work_id, req.season_number, req.subject_id, req.subject_name, req.subject_name_cn, episode_map,
    ))


@router.delete("/matches/{work_id}")
def remove_match(work_id: str, season_number: int | None = None):
    _ensure_work_and_season(work_id, season_number)
    with get_database().connect() as conn:
        deleted = conn.execute(
            "DELETE FROM bangumi_matches WHERE work_id = ? AND season_number = ?",
            (work_id, _season_key(season_number)),
        ).rowcount
    return {"ok": bool(deleted), "work_id": work_id, "season_number": season_number}


@router.get("/episodes/{work_id}")
def get_episode_mapping(work_id: str, season_number: int | None = None):
    _ensure_work_and_season(work_id, season_number)
    match = _get_match(work_id, season_number)
    match_payload = _match_payload(match) if match else None
    episode_map = (match_payload or {}).get("episode_map") or {}
    subject_id = int((match_payload or {}).get("subject_id") or 0)
    with get_database().connect() as conn:
        synced_rows = conn.execute(
            "SELECT episode_id, synced_at FROM bangumi_episode_sync WHERE subject_id = ? AND status = 'succeeded'",
            (subject_id,),
        ).fetchall() if subject_id else []
    synced = {str(row["episode_id"]): str(row["synced_at"]) for row in synced_rows}
    episodes = _local_episodes(work_id, season_number)
    return {
        "work_id": work_id,
        "season_number": season_number,
        "match": match_payload,
        "match_season_number": (match_payload or {}).get("season_number"),
        "episodes": [{
            "episode_id": item["episode_id"],
            "season_number": item["local_season_number"],
            "episode_number": item["local_episode_number"],
            "title": item["display_title"] or "",
            "bangumi_episode_id": episode_map.get(str(item["episode_id"])),
            "synced": str(item["episode_id"]) in synced,
            "synced_at": synced.get(str(item["episode_id"]), ""),
        } for item in episodes],
    }


@router.patch("/collections/{work_id}")
def set_subject_collection(work_id: str, req: CollectionPatch):
    _ensure_work_and_season(work_id, req.season_number)
    match = _get_match(work_id, req.season_number)
    if match is None:
        raise HTTPException(status_code=409, detail="请先确认 Bangumi 条目匹配")
    try:
        payload = BangumiClient(timeout=10.0).set_collection(int(match["subject_id"]), req.type)
    except BangumiError as exc:
        raise _http_error(exc) from exc
    return {"ok": True, "subject_id": int(match["subject_id"]), "type": req.type, "bangumi": payload}


@router.get("/collections/{work_id}")
def get_subject_collection(work_id: str, season_number: int | None = None):
    _ensure_work_and_season(work_id, season_number)
    match = _get_match(work_id, season_number)
    if match is None:
        raise HTTPException(status_code=409, detail="请先确认 Bangumi 条目匹配")
    client = BangumiClient(timeout=6.0)
    try:
        me = client.get_me(purpose="collection_read")
        username = me.get("username") or str(me.get("id") or "-")
        payload = client.get_collection(username, int(match["subject_id"]))
    except BangumiError as exc:
        raise _http_error(exc) from exc
    return {
        "ok": True,
        "subject_id": int(match["subject_id"]),
        "match_season_number": int(match["season_number"]) or None,
        "season_number": season_number,
        "bangumi": payload,
    }


def _record_episode_sync(
    episode_id: str,
    subject_id: int,
    bangumi_episode_id: int,
    *,
    status: str,
    error: str = "",
    database=None,
) -> None:
    with (database or get_database()).connect() as conn:
        conn.execute(
            """
            INSERT INTO bangumi_episode_sync(
                episode_id, subject_id, bangumi_episode_id, status, synced_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(episode_id, subject_id) DO UPDATE SET
                bangumi_episode_id = excluded.bangumi_episode_id,
                status = excluded.status,
                synced_at = excluded.synced_at,
                last_error = excluded.last_error
            """,
            (episode_id, subject_id, bangumi_episode_id, status, _now_iso(), error[:500]),
        )


def _refresh_episode_map(
    work_id: str,
    season_number: int | None,
    match: dict,
    client: BangumiClient,
    *,
    database=None,
) -> dict:
    episode_map = _episode_map_for_match(
        work_id, season_number, client, int(match["subject_id"]), database=database,
    )
    return _save_match(
        work_id,
        season_number,
        int(match["subject_id"]),
        str(match.get("subject_name") or ""),
        str(match.get("subject_name_cn") or ""),
        episode_map,
        database=database,
    )


@router.put("/episodes/{episode_id}/watched")
def mark_episode_watched(episode_id: str, req: EpisodeWatchedRequest):
    return sync_completed_episode(get_database(), episode_id, req)


def sync_completed_episode(database, episode_id: str, req: EpisodeWatchedRequest):
    """同步一个播放完成事件到 Bangumi，显式使用调用方的 V4 数据库。"""

    _ensure_work_and_season(req.work_id, req.season_number, database=database)
    episode = next((item for item in _local_episodes(
        req.work_id, req.season_number, database=database,
    ) if item["episode_id"] == episode_id), None)
    if episode is None:
        raise HTTPException(status_code=404, detail="剧集不属于当前作品或季度")
    match = _get_match(req.work_id, req.season_number, database=database)
    if match is None:
        raise HTTPException(status_code=409, detail="请先确认 Bangumi 条目匹配")
    match_payload = _match_payload(match)
    bangumi_episode_id = req.bangumi_episode_id or match_payload["episode_map"].get(episode_id)
    client = BangumiClient(timeout=10.0)
    try:
        if not bangumi_episode_id:
            match_payload = _match_payload(_refresh_episode_map(
                req.work_id, req.season_number, match, client, database=database,
            ))
            bangumi_episode_id = match_payload["episode_map"].get(episode_id)
        if not bangumi_episode_id:
            raise HTTPException(status_code=409, detail="Bangumi 条目中未找到对应剧集")
        payload = client.set_episode_collection(int(bangumi_episode_id), req.type)
    except BangumiError as exc:
        _record_episode_sync(
            episode_id,
            int(match["subject_id"]),
            int(bangumi_episode_id or 0),
            status="failed",
            error=str(exc),
            database=database,
        )
        raise _http_error(exc) from exc
    _record_episode_sync(
        episode_id,
        int(match["subject_id"]),
        int(bangumi_episode_id),
        status="succeeded",
        database=database,
    )
    return {
        "ok": True,
        "work_id": req.work_id,
        "episode_id": episode_id,
        "season_number": req.season_number,
        "subject_id": int(match["subject_id"]),
        "bangumi_episode_id": int(bangumi_episode_id),
        "type": req.type,
        "bangumi": payload,
    }


def _remote_watched_episode_ids(payload: dict) -> set[int]:
    watched: set[int] = set()
    for item in payload.get("data") or []:
        if int(item.get("type") or 0) != EPISODE_COLLECTION_DONE:
            continue
        episode = item.get("episode") or {}
        try:
            watched.add(int(episode.get("id") or item.get("episode_id")))
        except (TypeError, ValueError):
            continue
    return watched


def _active_asset_for_episode(conn, work_id: str, episode_id: str) -> str | None:
    """返回仍属于活动 confirmed revision 的真实 Asset，绝不为远端状态伪造播放身份。"""

    row = conn.execute(
        """
        SELECT ea.asset_id
        FROM episode_assets ea
        JOIN revision_bindings rb
          ON rb.episode_id = ea.episode_id
         AND rb.asset_id = ea.asset_id
         AND rb.work_id = ?
        JOIN import_revisions ir ON ir.revision_id = rb.revision_id AND ir.status = 'confirmed'
        JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
        WHERE ea.episode_id = ?
        ORDER BY ea.edition_id, ea.asset_id
        LIMIT 1
        """,
        (work_id, episode_id),
    ).fetchone()
    return str(row["asset_id"]) if row is not None else None


def _pull_remote_completion(
    work_id: str,
    local_episodes: list[dict],
    episode_map: dict[str, int],
    remote_watched: set[int],
) -> tuple[int, list[tuple[str, int]]]:
    """把远端已看映射为现有 V4 Asset 的 completed 进度，不写播放历史。"""

    pulled = 0
    synchronized: list[tuple[str, int]] = []
    database = get_database()
    with database.connect() as conn:
        for item in local_episodes:
            episode_id = str(item["episode_id"])
            remote_id = episode_map.get(episode_id)
            if remote_id is None or int(remote_id) not in remote_watched:
                continue
            asset_id = _active_asset_for_episode(conn, work_id, episode_id)
            if asset_id is None:
                continue
            existing = conn.execute(
                "SELECT completed FROM playback_progress WHERE episode_id = ? AND asset_id = ?",
                (episode_id, asset_id),
            ).fetchone()
            if existing is None or not bool(existing["completed"]):
                conn.execute(
                    """
                    INSERT INTO playback_progress(
                        episode_id, asset_id, work_id, position, duration, completed, updated_at
                    ) VALUES (?, ?, ?, 0, 0, 1, ?)
                    ON CONFLICT(episode_id, asset_id) DO UPDATE SET
                        work_id = excluded.work_id,
                        completed = 1,
                        updated_at = excluded.updated_at
                    """,
                    (episode_id, asset_id, work_id, _now_iso()),
                )
                pulled += 1
            synchronized.append((episode_id, int(remote_id)))
    return pulled, synchronized


@router.post("/progress/{work_id}/sync")
def sync_progress(work_id: str, req: ProgressSyncRequest):
    _ensure_work_and_season(work_id, req.season_number)
    match = _get_match(work_id, req.season_number)
    if match is None:
        raise HTTPException(status_code=409, detail="当前季度尚未匹配 Bangumi 条目")
    client = BangumiClient(timeout=15.0)
    try:
        match = _refresh_episode_map(work_id, req.season_number, match, client)
        match_payload = _match_payload(match)
        subject_id = int(match_payload["subject_id"])
        episode_map = match_payload["episode_map"]
        remote_watched = _remote_watched_episode_ids(client.get_episode_collection(subject_id))
    except BangumiError as exc:
        raise _http_error(exc) from exc

    local_episodes = _local_episodes(work_id, req.season_number)
    with get_database().connect() as conn:
        completed_rows = conn.execute(
            "SELECT DISTINCT episode_id FROM playback_progress WHERE work_id = ? AND completed = 1",
            (work_id,),
        ).fetchall()
        completed_ids = {str(row["episode_id"]) for row in completed_rows}

    pulled, pulled_episodes = _pull_remote_completion(
        work_id,
        local_episodes,
        episode_map,
        remote_watched,
    )

    to_push = [
        int(episode_map[item["episode_id"]])
        for item in local_episodes
        if item["episode_id"] in completed_ids
        and item["episode_id"] in episode_map
        and int(episode_map[item["episode_id"]]) not in remote_watched
    ]
    try:
        if to_push:
            client.batch_set_episode_collection(subject_id, to_push, EPISODE_COLLECTION_DONE)
    except BangumiError as exc:
        for item in local_episodes:
            remote_id = episode_map.get(item["episode_id"])
            if remote_id in to_push:
                _record_episode_sync(item["episode_id"], subject_id, int(remote_id), status="failed", error=str(exc))
        raise _http_error(exc) from exc

    pushed_ids = set(to_push)
    for item in local_episodes:
        remote_id = episode_map.get(item["episode_id"])
        if remote_id and (int(remote_id) in remote_watched or int(remote_id) in pushed_ids):
            _record_episode_sync(item["episode_id"], subject_id, int(remote_id), status="succeeded")
    for episode_id, remote_id in pulled_episodes:
        _record_episode_sync(episode_id, subject_id, remote_id, status="succeeded")

    return {
        "ok": True,
        "status": "synchronized",
        "work_id": work_id,
        "season_number": req.season_number,
        "subject_id": subject_id,
        "remote_done_before": len(remote_watched),
        "local_done_before": len(completed_ids),
        "pulled": pulled,
        "pushed": len(to_push),
        "pending": 0,
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
