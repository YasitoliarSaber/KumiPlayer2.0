# -*- coding: utf-8 -*-
"""Bangumi API client and local match store.

The frontend must confirm a subject match before sync.  Local matches are keyed
by work_id + season_number because Bangumi uses separate subject IDs for many
different seasons of the same series.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx

from app.core.config import DEFAULT_BANGUMI_USER_AGENT, load_config
from app.core.paths import get_cache_dir
from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.library.models import EpisodeIndex, WorkIndex
from app.library.store import load_library_index

BANGUMI_BASE_URL = "https://api.bgm.tv"
SUBJECT_COLLECTION_WISH = 1
SUBJECT_COLLECTION_COLLECT = 2
SUBJECT_COLLECTION_DOING = 3
SUBJECT_COLLECTION_ON_HOLD = 4
SUBJECT_COLLECTION_DROPPED = 5
EPISODE_COLLECTION_WISH = 1
EPISODE_COLLECTION_DONE = 2
EPISODE_COLLECTION_DROPPED = 3


class BangumiError(RuntimeError):
    """Raised when a Bangumi API request fails."""

    def __init__(self, message: str, status_code: int = 0, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class BangumiMatch:
    """Confirmed local-to-Bangumi subject mapping."""

    work_id: str
    subject_id: int
    season_number: Optional[int] = None
    subject_name: str = ""
    subject_name_cn: str = ""
    confirmed_at: str = ""
    updated_at: str = ""
    episode_map: dict[str, int] = field(default_factory=dict)


@dataclass
class BangumiEpisodeSync:
    """Local record of a Bangumi episode sync attempt."""

    local_episode_id: str
    bangumi_episode_id: int
    work_id: str
    season_number: Optional[int]
    subject_id: int
    type: int = EPISODE_COLLECTION_DONE
    status: str = "succeeded"
    error: str = ""
    synced_at: str = ""


@dataclass
class BangumiState:
    version: int = 1
    matches: list[BangumiMatch] = field(default_factory=list)
    episode_sync: list[BangumiEpisodeSync] = field(default_factory=list)


class BangumiClient:
    """Small official v0 API client.

    Bangumi requires bearer auth for account writes and a custom User-Agent for
    non-browser clients.
    """

    def __init__(
        self,
        access_token: str = "",
        user_agent: str = "",
        base_url: str = BANGUMI_BASE_URL,
        timeout: float = 15.0,
    ):
        config = load_config()
        self.access_token = access_token or config.bangumi_access_token
        # User-Agent 是应用身份，不应携带用户姓名、昵称或其他个人配置。
        # 保留参数只为兼容旧调用方，但请求始终使用统一的公开应用标识。
        self.user_agent = DEFAULT_BANGUMI_USER_AGENT
        self.proxy_url = config.proxy_url or None
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/v0/me", auth_required=True)

    def search_subjects(
        self,
        keyword: str,
        limit: int = 10,
        offset: int = 0,
        subject_types: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        if not keyword.strip():
            raise BangumiError("搜索关键词不能为空", status_code=400)
        body = {"keyword": keyword.strip(), "sort": "match"}
        if subject_types:
            body["filter"] = {"type": subject_types}
        return self._request(
            "POST",
            "/v0/search/subjects",
            params={"limit": limit, "offset": offset},
            json=body,
        )

    def get_collection(self, username: str, subject_id: int) -> dict[str, Any]:
        return self._request("GET", f"/v0/users/{username}/collections/{subject_id}", auth_required=True)

    def set_collection(self, subject_id: int, collection_type: int) -> dict[str, Any]:
        body = {"type": collection_type}
        return self._request("POST", f"/v0/users/-/collections/{subject_id}", json=body, auth_required=True)

    def list_subject_episodes(self, subject_id: int, episode_type: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v0/episodes",
            params={"subject_id": subject_id, "type": episode_type, "limit": limit, "offset": 0},
        )
        if isinstance(payload, dict):
            return list(payload.get("data") or [])
        return []

    def set_episode_collection(self, episode_id: int, collection_type: int = EPISODE_COLLECTION_DONE) -> dict[str, Any]:
        body = {"type": collection_type}
        return self._request(
            "PUT",
            f"/v0/users/-/collections/-/episodes/{episode_id}",
            json=body,
            auth_required=True,
        )

    def get_episode_collection(
        self, subject_id: int, limit: int = 1000, offset: int = 0
    ) -> dict[str, Any]:
        """获取用户在某个条目下所有章节的收藏状态。

        返回形如
        ``{"data": [{"episode": {"id": 123}, "type": 2}], "total": N}``。
        必须分页读取，不能只取默认第一页。
        """
        payload = self._request(
            "GET",
            f"/v0/users/-/collections/{subject_id}/episodes",
            params={"limit": limit, "offset": offset, "subject_id": subject_id},
            auth_required=True,
        )
        if isinstance(payload, dict):
            return payload
        return {"data": [], "total": 0}

    def batch_set_episode_collection(
        self,
        subject_id: int,
        episode_ids: list[int],
        collection_type: int = EPISODE_COLLECTION_DONE,
    ) -> dict[str, Any]:
        """批量设置章节收藏状态。

        Bangumi 官方文档规定批量更新章节的状态使用 PATCH 方法。
        ``type=2`` 表示"看过"。
        请求体：``{"episode_id": [1, 2, 3], "type": 2}``
        """
        if not episode_ids:
            return {"ok": True, "updated": 0}
        body = {"episode_id": episode_ids, "type": collection_type}
        return self._request(
            "PATCH",
            f"/v0/users/-/collections/{subject_id}/episodes",
            json=body,
            auth_required=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        auth_required: bool = False,
    ) -> dict[str, Any]:
        if auth_required and not self.access_token:
            raise BangumiError("未配置 Bangumi access token", status_code=401)

        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            client_kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self.proxy_url:
                client_kwargs["proxy"] = self.proxy_url
            with httpx.Client(**client_kwargs) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=json,
                    headers=headers,
                )
        except httpx.TimeoutException as e:
            raise BangumiError("Bangumi 请求超时") from e
        except httpx.ConnectError as e:
            if self.proxy_url:
                raise BangumiError(
                    f"Bangumi 代理不可连接（{self.proxy_url}），请启动 Clash 或检查代理端口。"
                ) from e
            raise BangumiError("Bangumi 无法连接到服务，请检查网络连接。") from e
        except httpx.HTTPError as e:
            raise BangumiError(f"Bangumi 请求失败: {e}") from e

        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = response.text[:300]
            message = payload.get("description") if isinstance(payload, dict) else ""
            message = message or payload.get("message") if isinstance(payload, dict) else message
            raise BangumiError(message or f"Bangumi 返回 {response.status_code}", response.status_code, payload)

        if response.status_code == 204 or not response.content:
            return {}
        return response.json()


def get_state_path() -> Path:
    return get_cache_dir() / "bangumi_state.json"


def load_state() -> BangumiState:
    path = get_state_path()
    if not path.exists():
        return BangumiState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return BangumiState()

    matches = []
    for item in data.get("matches", []):
        matches.append(BangumiMatch(
            work_id=item.get("work_id", ""),
            season_number=item.get("season_number"),
            subject_id=int(item.get("subject_id") or 0),
            subject_name=item.get("subject_name", ""),
            subject_name_cn=item.get("subject_name_cn", ""),
            confirmed_at=item.get("confirmed_at", ""),
            updated_at=item.get("updated_at", ""),
            episode_map={str(k): int(v) for k, v in (item.get("episode_map") or {}).items()},
        ))

    sync_items = []
    for item in data.get("episode_sync", []):
        sync_items.append(BangumiEpisodeSync(
            local_episode_id=item.get("local_episode_id", ""),
            bangumi_episode_id=int(item.get("bangumi_episode_id") or 0),
            work_id=item.get("work_id", ""),
            season_number=item.get("season_number"),
            subject_id=int(item.get("subject_id") or 0),
            type=int(item.get("type") or EPISODE_COLLECTION_DONE),
            status=item.get("status", "succeeded"),
            error=item.get("error", ""),
            synced_at=item.get("synced_at", ""),
        ))

    return BangumiState(version=data.get("version", 1), matches=matches, episode_sync=sync_items)


def save_state(state: BangumiState) -> None:
    with DATA_WRITE_LOCK:
        payload = {
            "version": state.version,
            "matches": [asdict(match) for match in state.matches],
            "episode_sync": [asdict(sync) for sync in state.episode_sync[-500:]],
        }
        write_json_atomic(get_state_path(), payload)


def get_match(work_id: str, season_number: Optional[int]) -> Optional[BangumiMatch]:
    """查找当前季的已确认 Bangumi 条目。

    旧版允许季级请求回退到 ``season_number=None`` 的整作映射，但 Bangumi
    往往会把续作拆为不同条目；这个回退会将后续季错误同步到首季。保留旧
    映射的读取能力，仅在调用方没有季上下文时才使用它。
    """
    state = load_state()
    for match in state.matches:
        if match.work_id == work_id and match.season_number == season_number:
            return match
    return None


def upsert_match(match: BangumiMatch) -> BangumiMatch:
    with DATA_WRITE_LOCK:
        state = load_state()
        now = _now()
        if not match.confirmed_at:
            match.confirmed_at = now
        match.updated_at = now
        for idx, existing in enumerate(state.matches):
            if existing.work_id == match.work_id and existing.season_number == match.season_number:
                match.episode_map = existing.episode_map | match.episode_map
                state.matches[idx] = match
                save_state(state)
                return match
        state.matches.append(match)
        save_state(state)
        return match


def delete_match(work_id: str, season_number: Optional[int]) -> bool:
    with DATA_WRITE_LOCK:
        state = load_state()
        before = len(state.matches)
        state.matches = [m for m in state.matches if not (m.work_id == work_id and m.season_number == season_number)]
        if len(state.matches) == before:
            return False
        save_state(state)
        return True


def record_episode_sync(sync: BangumiEpisodeSync) -> None:
    with DATA_WRITE_LOCK:
        state = load_state()
        sync.synced_at = sync.synced_at or _now()
        state.episode_sync.append(sync)
        for match in state.matches:
            if match.work_id == sync.work_id and match.season_number == sync.season_number:
                match.episode_map[sync.local_episode_id] = sync.bangumi_episode_id
                match.updated_at = sync.synced_at
                break
        save_state(state)


def resolve_work(work_id: str) -> WorkIndex:
    index = load_library_index()
    if index is None:
        raise ValueError("LibraryIndex 不存在，请先执行 POST /api/library/rescan")
    for work in index.works:
        if work.work_id == work_id:
            return work
    raise LookupError(f"作品不存在: {work_id}")


def resolve_episode(episode_id: str, work_id: str = "") -> tuple[WorkIndex, EpisodeIndex]:
    index = load_library_index()
    if index is None:
        raise ValueError("LibraryIndex 不存在，请先执行 POST /api/library/rescan")
    for work in index.works:
        if work_id and work.work_id != work_id:
            continue
        for episode in work.episodes:
            if episode.episode_id == episode_id:
                return work, episode
    raise LookupError(f"剧集不存在: {episode_id}")


def list_local_episodes(work_id: str, season_number: Optional[int]) -> list[EpisodeIndex]:
    work = resolve_work(work_id)
    episodes = work.episodes
    if season_number is not None:
        episodes = [episode for episode in episodes if episode.season_number == season_number]
    return sorted(episodes, key=lambda item: (item.season_number or 0, item.episode_number or 0, item.title))


def resolve_bangumi_episode_id(
    client: BangumiClient,
    match: BangumiMatch,
    episode: EpisodeIndex,
    explicit_episode_id: Optional[int] = None,
) -> int:
    if explicit_episode_id:
        return explicit_episode_id
    mapped = match.episode_map.get(episode.episode_id)
    if mapped:
        return mapped

    candidates = client.list_subject_episodes(
        match.subject_id,
        episode_type=_bangumi_episode_type(episode),
    )
    by_number = _episode_number_candidates(candidates)
    if episode.episode_number in by_number:
        return by_number[episode.episode_number]
    raise LookupError("未能按集数自动匹配 Bangumi 章节，请前端让用户手动指定")


def _bangumi_episode_type(episode: EpisodeIndex) -> int:
    """Map local structural groups to Bangumi's episode types.

    ``season`` is Bangumi main story (0); local OVA/OAD/SP entries are queried
    from Bangumi's special list (1).  Auxiliary OP/ED/PV assets are never
    eligible for syncing because they do not enter the playable library index.
    """
    return 1 if (episode.group_type or "").lower() == "special" else 0


def _episode_number_candidates(items: list[dict[str, Any]]) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in items:
        ep_number = _coerce_int(item.get("ep"))
        item_id = _coerce_int(item.get("id"))
        if ep_number and item_id:
            result.setdefault(ep_number, item_id)
    return result


def _coerce_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def sync_bidirectional_progress(
    work_id: str,
    season_number: Optional[int],
    *,
    client: Optional[BangumiClient] = None,
) -> dict[str, Any]:
    """双向合并 Bangumi 与 KumiPlayer 的观看进度。

    同步算法使用集合式合并，而非简单最大值：

    .. code-block::

        remote_done = Bangumi 中 type=2 的剧集 Bangumi ID 集合
        local_done = 本地当前季度所有 completed=True 的 episode_id 集合
        target_done = remote_done ∪ local_done（按 episode_id 逐集映射）

    然后：
    - ``remote_done - local_done``：批量写入本地（pull）。
    - ``local_done - remote_done``：批量上传 Bangumi（push）。

    返回同步摘要。
    """
    match = get_match(work_id, season_number)
    if match is None:
        return {
            "ok": False,
            "status": "unmatched",
            "work_id": work_id,
            "season_number": season_number,
        }

    bg_client = client or BangumiClient()

    # ---- 1. 获取远端已看剧集 ----
    remote_bangumi_ids: set[int] = set()
    try:
        offset = 0
        limit = 500
        while True:
            page = bg_client.get_episode_collection(match.subject_id, limit=limit, offset=offset)
            items = page.get("data") or []
            if not items:
                break
            for item in items:
                episode_payload = item.get("episode")
                if isinstance(episode_payload, dict):
                    ep_id = _coerce_int(episode_payload.get("id"))
                    episode_type = _coerce_int(episode_payload.get("type"))
                else:
                    # 兼容旧测试夹具和可能存在的旧缓存结构。
                    ep_id = _coerce_int(item.get("episode_id"))
                    episode_type = 0
                ep_type = _coerce_int(item.get("type"))
                if ep_id and episode_type == 0 and ep_type == EPISODE_COLLECTION_DONE:
                    remote_bangumi_ids.add(ep_id)
            if len(items) < limit:
                break
            offset += limit
        remote_available = True
    except BangumiError:
        remote_available = False
        remote_bangumi_ids = set()

    # ---- 2. 获取本地已看和本地剧集列表 ----
    from app.playback.progress import list_progress

    local_episodes = list_local_episodes(work_id, season_number)
    local_progress = list_progress(work_id)
    current_episode_ids = {episode.episode_id for episode in local_episodes}
    local_completed_ids: set[str] = {
        item.episode_id
        for item in local_progress
        if (
            item.episode_id in current_episode_ids
            and item.completed
            and not item.manually_unwatched
        )
    }

    # 如果远端不可达，只能返回当前本地状态
    if not remote_available:
        return {
            "ok": True,
            "status": "offline",
            "work_id": work_id,
            "season_number": season_number,
            "subject_id": match.subject_id,
            "remote_done_before": 0,
            "local_done_before": len(local_completed_ids),
            "pulled": 0,
            "pushed": 0,
            "pending": len(local_completed_ids),
        }

    # ---- 3. 建立本地 episode_id ↔ Bangumi episode_id 的双向映射 ----
    local_to_bangumi: dict[str, int] = {}
    inferred_candidates = [
        episode
        for episode in local_episodes
        if (
            not match.episode_map.get(episode.episode_id)
            and (episode.group_type or "").lower() in ("", "season")
            and episode.episode_number
        )
    ]
    by_number: dict[int, int] = {}
    if inferred_candidates:
        candidates = bg_client.list_subject_episodes(
            match.subject_id,
            episode_type=0,
            limit=1000,
        )
        by_number = _episode_number_candidates(candidates)

    for ep in local_episodes:
        mapped = match.episode_map.get(ep.episode_id)
        if mapped:
            local_to_bangumi[ep.episode_id] = mapped
            continue
        # 按本篇集数推断（仅 season 类型）
        if (ep.group_type or "").lower() in ("", "season") and ep.episode_number:
            bangumi_id = by_number.get(ep.episode_number)
            if bangumi_id:
                local_to_bangumi[ep.episode_id] = bangumi_id
                # 缓存映射
                match.episode_map[ep.episode_id] = bangumi_id

    # bangumi_id -> local_id
    bangumi_to_local: dict[int, str] = {v: k for k, v in local_to_bangumi.items()}

    # ---- 4. 计算集合 ----
    remote_local_ids: set[str] = set()
    for bg_id in remote_bangumi_ids:
        local_id = bangumi_to_local.get(bg_id)
        if local_id:
            remote_local_ids.add(local_id)

    # 需拉取到本地的：远端有、本地没有完成
    to_pull = remote_local_ids - local_completed_ids
    manual_unwatched_ids = {
        item.episode_id
        for item in local_progress
        if item.episode_id in current_episode_ids and item.manually_unwatched
    }
    to_pull = to_pull - manual_unwatched_ids

    # 需推送到远端的：本地完成了、远端没有
    local_remote_completed_ids: set[int] = set()
    for local_id in local_completed_ids:
        bg_id = local_to_bangumi.get(local_id)
        if bg_id:
            local_remote_completed_ids.add(bg_id)
    to_push = local_remote_completed_ids - remote_bangumi_ids

    # ---- 5. 执行拉取（写入本地） ----
    pulled = 0
    if to_pull:
        from app.playback.progress import import_remote_progress

        pulled = import_remote_progress(work_id, to_pull, season_number)

    # 远端已经存在的本地完成记录，也应结束“待同步”状态。
    already_remote_local_ids = remote_local_ids & local_completed_ids
    _mark_bangumi_synced(work_id, already_remote_local_ids)

    # ---- 6. 执行推送（上传 Bangumi） ----
    pushed = 0
    push_failed = False
    if to_push:
        try:
            result = bg_client.batch_set_episode_collection(
                match.subject_id,
                sorted(to_push),
                EPISODE_COLLECTION_DONE,
            )
            pushed = result.get("updated", len(to_push))
            pushed_local_ids = {
                local_id
                for local_id, bangumi_id in local_to_bangumi.items()
                if local_id in local_completed_ids and bangumi_id in to_push
            }
            _mark_bangumi_synced(work_id, pushed_local_ids)
        except BangumiError:
            push_failed = True

    # ---- 7. 返回摘要 ----
    if push_failed:
        status = "partial"
    elif pulled > 0 or pushed > 0:
        status = "synced"
    else:
        status = "synced"

    upsert_match(match)

    return {
        "ok": True,
        "status": status,
        "work_id": work_id,
        "season_number": season_number,
        "subject_id": match.subject_id,
        "remote_done_before": len(remote_bangumi_ids),
        "local_done_before": len(local_completed_ids),
        "pulled": pulled,
        "pushed": pushed,
        "pending": len(to_push) if push_failed else 0,
    }


def _mark_bangumi_synced(work_id: str, episode_ids: set[str]) -> None:
    """批量更新进度记录的 bangumi_synced 状态。"""
    from app.playback.progress import load_progress, progress_path
    from app.core.atomic_json import write_json_atomic
    from app.core.data_lock import DATA_WRITE_LOCK

    if not episode_ids:
        return
    with DATA_WRITE_LOCK:
        items = load_progress()
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        for item in items:
            if item.work_id == work_id and item.episode_id in episode_ids and item.completed:
                item.bangumi_synced = True
                item.bangumi_error = ""
                item.updated_at = now
        write_json_atomic(progress_path(), [asdict(item) for item in items])


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()
