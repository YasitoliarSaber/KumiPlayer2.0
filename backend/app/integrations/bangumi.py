"""Bangumi provider client and non-sensitive account snapshot storage.

V4 keeps this integration limited to credentials, session verification and
subject search.  Work/Season/Episode matching and watch synchronization are
owned by the V4 media graph and are not persisted here.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.atomic_json import write_json_atomic
from app.core.config import DEFAULT_BANGUMI_USER_AGENT, load_config
from app.core.credential_store import SECURE_CREDENTIAL_STORE, CredentialStoreError
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir

BANGUMI_BASE_URL = "https://api.bgm.tv"
SUBJECT_COLLECTION_WISH = 1
SUBJECT_COLLECTION_COLLECT = 2
SUBJECT_COLLECTION_DOING = 3
SUBJECT_COLLECTION_ON_HOLD = 4
SUBJECT_COLLECTION_DROPPED = 5
EPISODE_COLLECTION_WISH = 1
EPISODE_COLLECTION_DONE = 2
EPISODE_COLLECTION_DROPPED = 3


# ---- Bangumi 统一错误分类（CP2：外层 API/UI 不再自行猜状态码）----
AUTH_INVALID = "auth_invalid"                # 401：Token 需要更新
FORBIDDEN = "forbidden"                      # 403：无权限（保留凭据，不等于退出）
RATE_LIMITED = "rate_limited"                # 429：请求受限（保留凭据）
NETWORK_UNAVAILABLE = "network_unavailable"  # 连接/DNS 失败
PROXY_UNAVAILABLE = "proxy_unavailable"      # 代理不可连接
TIMEOUT = "timeout"                          # 请求超时
SERVER_ERROR = "server_error"                # 5xx
BAD_RESPONSE = "bad_response"                # 响应无法解析
UNKNOWN = "unknown"


class BangumiError(RuntimeError):
    """Raised when a Bangumi API request fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        payload: Any = None,
        error_code: str = UNKNOWN,
        retry_after: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.error_code = error_code
        # 429 时的 Retry-After 原始值（调用方据此停止立即重试）
        self.retry_after = retry_after

def resolve_bangumi_access_token() -> str:
    """统一运行时凭据解析：整个 Bangumi runtime 的真实 token 来源。

    语义（REWORK：CM 恢复无需重启即可恢复所有 authenticated 业务请求）：
    1. config cache 中有 token → 返回（测试走 config.json、生产已 hydrate）；
    2. config cache 为空且安全存储启用 → **直接读安全存储拿真实 token**
       （不依赖可能陈旧的 config cache，CM 恢复后立即生效）；
    3. 读取错误 → 返回 ""（不删除、不改写、不误判退出——与 not_found
       同值，调用方只关心有没有 token，session 层另有三态判定）。

    ``BangumiClient`` 默认 token 来源、/session/verify、收藏同步、已看状态
    写入等所有 authenticated 请求都消费本函数。
    """
    from app.core.config import _credential_storage_enabled

    config = load_config()
    if config.bangumi_access_token:
        return config.bangumi_access_token
    if _credential_storage_enabled():
        try:
            return SECURE_CREDENTIAL_STORE.read("bangumi_access_token")
        except CredentialStoreError:
            return ""
    return ""


@dataclass
class BangumiAccountSnapshot:
    """非敏感的本地账户快照（**严禁保存 Access Token**）。

    只缓存最近一次成功验证得到的用户资料与验证时间，供 /session 离线恢复
    账户卡；Token 永远只存在于 Windows Credential Manager。
    """

    user_id: int | None = None
    username: str = ""
    nickname: str = ""
    avatar_url: str = ""
    sign: str = ""
    auth_status: str = "unknown"  # unknown / valid / reauth_required
    connectivity: str = "unknown"  # unknown / online / offline / rate_limited / forbidden / server_error
    last_verified_at: str = ""
    last_success_at: str = ""
    last_failure_at: str = ""
    last_http_status: int | None = None
    last_error_code: str = ""
    last_error_message: str = ""

    def to_public_user(self) -> dict | None:
        """恢复账户卡的最小用户资料；无任何资料时返回 None。"""
        if self.user_id is None and not self.username and not self.nickname:
            return None
        return {
            "id": self.user_id,
            "username": self.username,
            "nickname": self.nickname,
            "avatar": self.avatar_url,
            "sign": self.sign,
        }


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
        # 显式 access_token 优先；否则统一运行时解析（config cache → 安全存储），
        # CM 暂时故障恢复后无需重启即可恢复普通 authenticated 业务请求
        self.access_token = access_token or resolve_bangumi_access_token()
        # User-Agent 是应用身份，不应携带用户姓名、昵称或其他个人配置。
        # 保留参数只为兼容旧调用方，但请求始终使用统一的公开应用标识。
        self.user_agent = DEFAULT_BANGUMI_USER_AGENT
        self.proxy_url = config.proxy_url or None
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_me(self, purpose: str = "") -> dict[str, Any]:
        return self._request("GET", "/v0/me", auth_required=True, purpose=purpose)
    def search_subjects(
        self,
        keyword: str,
        limit: int = 10,
        offset: int = 0,
        subject_types: list[int] | None = None,
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
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth_required: bool = False,
        purpose: str = "",
    ) -> dict[str, Any]:
        """统一请求入口：执行 + 诊断日志（脱敏，绝不含 Token）。

        ``purpose`` 是本地日志标记（不发给 Bangumi）：manual_verify /
        session_verify / subject_search / collection_read / collection_write /
        episode_sync / me_lookup 等；缺省时按 method+path 自动分类。
        """
        purpose = purpose or _classify_purpose(method, path)
        start = time.monotonic()
        try:
            result, status_code = self._raw_request(
                method, path, params=params, json=json, auth_required=auth_required
            )
        except BangumiError as error:
            _log_bangumi_request(
                method=method, path=path, purpose=purpose,
                status=error.status_code or 0, error_code=error.error_code,
                duration_ms=(time.monotonic() - start) * 1000,
                retry_after=error.retry_after,
                credential_present=bool(self.access_token),
                proxy_enabled=bool(self.proxy_url),
            )
            raise
        _log_bangumi_request(
            method=method, path=path, purpose=purpose, status=status_code, error_code="",
            duration_ms=(time.monotonic() - start) * 1000, retry_after="",
            credential_present=bool(self.access_token),
            proxy_enabled=bool(self.proxy_url),
        )
        return result

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth_required: bool = False,
    ) -> tuple[dict[str, Any], int]:
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
            raise BangumiError("Bangumi 请求超时", error_code=TIMEOUT) from e
        except httpx.ConnectError as e:
            if self.proxy_url:
                raise BangumiError(
                    f"Bangumi 代理不可连接（{self.proxy_url}），请启动 Clash 或检查代理端口。",
                    error_code=PROXY_UNAVAILABLE,
                ) from e
            raise BangumiError(
                "Bangumi 无法连接到服务，请检查网络连接。",
                error_code=NETWORK_UNAVAILABLE,
            ) from e
        except httpx.HTTPError as e:
            raise BangumiError(
                f"Bangumi 请求失败: {e}",
                error_code=NETWORK_UNAVAILABLE,
            ) from e

        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = response.text[:300]
            message = payload.get("description") if isinstance(payload, dict) else ""
            message = message or payload.get("message") if isinstance(payload, dict) else message
            status = response.status_code
            if status == 401:
                error_code = AUTH_INVALID
                if auth_required:
                    # 认证观察：401 → reauth_required（保留凭据，不删除）
                    _observe_auth("reauth", status)
            elif status == 403:
                error_code = FORBIDDEN
            elif status == 429:
                error_code = RATE_LIMITED
            elif 500 <= status < 600:
                error_code = SERVER_ERROR
            elif 400 <= status < 500:
                error_code = BAD_RESPONSE
            else:  # pragma: no cover - 理论不可达
                error_code = UNKNOWN
            raise BangumiError(
                message or f"Bangumi 返回 {status}",
                status,
                payload,
                error_code=error_code,
                retry_after=response.headers.get("Retry-After") or "",
            )

        if auth_required:
            # 认证观察：authenticated 请求成功 → valid/online（/v0/me 不再是唯一探针）
            _observe_auth("success", response.status_code)

        if response.status_code == 204 or not response.content:
            return {}, response.status_code
        try:
            return response.json(), response.status_code
        except ValueError as e:
            raise BangumiError(
                "Bangumi 响应格式无法解析",
                response.status_code,
                error_code=BAD_RESPONSE,
            ) from e

def _classify_purpose(method: str, path: str) -> str:
    """按 method+path 推断请求用途（本地日志标记，不发给 Bangumi）。"""
    if path == "/v0/me":
        return "me_lookup"
    if path.startswith("/v0/search"):
        return "subject_search"
    if path.startswith("/v0/episodes"):
        return "episode_sync"
    if "/collections/" in path:
        if "/episodes" in path:
            return "episode_sync"
        return "collection_write" if method in ("POST", "PATCH", "PUT", "DELETE") else "collection_read"
    return "unknown"


def _get_request_logger() -> logging.Logger:
    """Bangumi 请求诊断日志（data/logs/bangumi_requests.log，惰性初始化）。

    handler 按当前数据目录绑定：数据目录切换（测试隔离/安装版换库）时重建，
    避免日志写入已失效的旧目录。
    """
    logger = logging.getLogger("kumiplayer.bangumi")
    log_path = (get_data_dir() / "logs" / "bangumi_requests.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    existing = [
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
        and Path(getattr(handler, "baseFilename", "") or "").resolve() == log_path
    ]
    if not existing:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _log_bangumi_request(
    *,
    method: str,
    path: str,
    purpose: str,
    status: int,
    error_code: str,
    duration_ms: float,
    retry_after: str,
    credential_present: bool,
    proxy_enabled: bool,
) -> None:
    """请求诊断日志（脱敏红线：绝不含 Authorization / Token / 凭据前缀）。"""
    _get_request_logger().info(
        "%s %s purpose=%s status=%s error=%s duration_ms=%d retry_after=%s credential_present=%s proxy_enabled=%s",
        method,
        path,
        purpose,
        status,
        error_code or "ok",
        int(duration_ms),
        retry_after or "-",
        "true" if credential_present else "false",
        "true" if proxy_enabled else "false",
    )


def _observe_auth(kind: str, status_code: int) -> None:
    """认证观察（低频，仅 auth_required 请求）：把成功/401 结果反馈到账户快照。

    - success：auth_status=valid、connectivity=online、last_success_at 更新；
    - reauth：auth_status=reauth_required（**保留凭据与快照**，绝不删除）。

    ``/v0/me`` 不再是唯一“生命探针”——任何 authenticated 请求的成功都是
    认证证据；401 也是精确的失效信号。
    """
    snapshot = load_account_snapshot()
    now = _now()
    if kind == "success":
        snapshot.auth_status = "valid"
        snapshot.connectivity = "online"
        snapshot.last_success_at = now
        snapshot.last_error_code = ""
        snapshot.last_error_message = ""
    else:
        snapshot.auth_status = "reauth_required"
        snapshot.connectivity = "online"
        snapshot.last_failure_at = now
        snapshot.last_error_code = AUTH_INVALID
    snapshot.last_http_status = status_code
    save_account_snapshot(snapshot)
def get_account_snapshot_path() -> Path:
    return get_data_dir() / "bangumi_account.json"


def load_account_snapshot() -> BangumiAccountSnapshot:
    """读取本地账户快照；缺失或损坏时返回空快照（不抛错、不删文件）。"""
    path = get_account_snapshot_path()
    if not path.exists():
        return BangumiAccountSnapshot()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return BangumiAccountSnapshot()
    return BangumiAccountSnapshot(
        user_id=data.get("user_id"),
        username=data.get("username", ""),
        nickname=data.get("nickname", ""),
        avatar_url=data.get("avatar_url", ""),
        sign=data.get("sign", ""),
        auth_status=data.get("auth_status", "unknown"),
        connectivity=data.get("connectivity", "unknown"),
        last_verified_at=data.get("last_verified_at", ""),
        last_success_at=data.get("last_success_at", ""),
        last_failure_at=data.get("last_failure_at", ""),
        last_http_status=data.get("last_http_status"),
        last_error_code=data.get("last_error_code", ""),
        last_error_message=data.get("last_error_message", ""),
    )


def save_account_snapshot(snapshot: BangumiAccountSnapshot) -> None:
    """原子写账户快照（DATA_WRITE_LOCK + 原子换位）。"""
    with DATA_WRITE_LOCK:
        write_json_atomic(get_account_snapshot_path(), asdict(snapshot))


def clear_account_snapshot() -> None:
    """用户主动退出/替换凭据时清理账户快照。"""
    with DATA_WRITE_LOCK:
        path = get_account_snapshot_path()
        if path.exists():
            path.unlink()

def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()
