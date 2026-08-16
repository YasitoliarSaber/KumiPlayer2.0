"""OpenList 连接 API 端点。

POST   /api/openlist/test-connection       连接测试（ping + login + 目录可读）
POST   /api/openlist/config                保存连接配置（密码入凭据管理器，单实例）
GET    /api/openlist/browse                单层目录浏览（懒加载 + 本地持久缓存 + SWR）
POST   /api/openlist/prefetch              有界预取当前层少量直接子目录（单并发、不递归）
GET    /api/openlist/routes                读取提供商路由（含推导本地路径预览）
POST   /api/openlist/routes/discover       从远端总根读取直接子目录并给出提供商建议（不自动保存）
PUT    /api/openlist/routes                保存提供商路由（校验前缀合法/不重复/归属 provider）
POST   /api/openlist/presets/{id}/rescan   按预设保存的远端定位增量更新（Source Catalog 增量链路）

安全边界：
- 前端绝不直连 OpenList；所有外部 HTTP 只由本模块发起；
- 密码 / Token / Authorization 头 / 服务端原始错误不进入日志、任务结果
  与前端响应（OpenListClient 已做归一化）；
- 本地浏览缓存只存白名单字段，禁止缓存凭据 / Token / 直链 / 内部 path；
- 导入路径必须位于配置的映射根路径之下，并归属启用的提供商路由（或 other）；
- 普通浏览绝不递归扫描，预取有预算、单并发、可取消且不递归后代；
"""
import copy
import ipaddress
import threading
from functools import wraps
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.catalog import source_health
from app.core.config import load_config, openlist_credential_state, save_config
from app.core.credential_store import CredentialStoreError
from app.integrations.openlist.cache import (
    connection_key,
    read_cache,
    write_cache,
)
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
    provider_for_remote,
)
from app.media_presets.store import get_preset

router = APIRouter(prefix="/api/openlist", tags=["openlist"])


def _admitted_import_endpoint(fn):
    """在实际同步路由线程内持有 admission，覆盖根/批次/入队完整链路。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from app.catalog import maintenance_guard

        try:
            with maintenance_guard.admission():
                return fn(*args, **kwargs)
        except maintenance_guard.MaintenanceAdmissionDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    return wrapper

# 单层浏览的安全上限（超出截断并标记，不阻塞导入扫描）
_BROWSE_MAX_ENTRIES = 1000
# 每个目录的浏览分页大小
_BROWSE_PER_PAGE = 100
# 批量导入的目录数上限
_BATCH_MAX_PATHS = 20
# 预取数量上限
_PREFETCH_MAX = 50

_NOT_CONFIGURED_MESSAGE = "尚未配置 OpenList 连接，请先到设置页完成配置"


def _is_loopback_http(url: str) -> bool:
    """http 地址是否为本地回环（localhost / 127.x / ::1）。

    回环地址的明文传输不经过网络，无需风险确认。
    """
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

# 后台刷新去重与有界执行（禁止每个 stale 路径直接创建线程）
_refresh_guard = threading.Lock()
_refresh_inflight: dict[tuple[str, str], bool] = {}
_refresh_active = 0
_REFRESH_MAX_ACTIVE = 8
_refresh_executor = None  # 惰性初始化 ThreadPoolExecutor(max_workers=4)
_refresh_executor_lock = threading.Lock()
# 预取 generation：新导航/新一代预取使旧 generation 停止启动未处理路径
_prefetch_generation_guard = threading.Lock()
_prefetch_generation = 0
# 预取全局单并发
_prefetch_guard = threading.Lock()


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
class BatchImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_paths: list[str]
    import_family: str = "anime"
    import_scope: str = ""


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


# ============================================================
# 内部工具
# ============================================================

def _client_from_config(
    config=None,
    *,
    server_url: str = "",
    username: str | None = None,
    password: str | None = None,
) -> OpenListClient:
    """从配置（或显式覆盖）构建客户端。

    凭据在正式环境由 ``load_config`` 从 Windows Credential Manager 水合。
    ``username`` / ``password`` 为 None 时回落到配置中的值。
    """
    config = config or load_config()
    url = server_url or config.openlist_server_url
    user = username if username is not None else config.openlist_username
    pwd = password if password is not None else config.openlist_password
    if not url or not user or not pwd:
        raise HTTPException(status_code=400, detail=_NOT_CONFIGURED_MESSAGE)
    try:
        return get_openlist_client(url, user, pwd, client_factory=OpenListClient)
    except OpenListError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _routes_from_config(config) -> list[OpenListRouteConfig]:
    routes = config.openlist_routes or []
    return [item for item in routes if isinstance(item, OpenListRouteConfig)]


def _ensure_within_remote_root(remote_root: str, path: str) -> None:
    """浏览/导入范围限制在远端总根路径内。"""
    if remote_root != "/" and path != remote_root and not path.startswith(remote_root + "/"):
        raise HTTPException(status_code=400, detail="选择的远端目录不在映射根路径之下，无法映射到本地挂载")


def _local_root_for_remote(config, remote_path: str) -> Path:
    """由远端总根 + 选中远端目录推导本地来源根（连接级挂载根统一推导）。"""
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    mount_root = (config.openlist_mount_root or "").strip()
    if not mount_root:
        raise HTTPException(status_code=400, detail=_NOT_CONFIGURED_MESSAGE)
    _ensure_within_remote_root(remote_root, remote_path)
    return Path(derive_local_path(mount_root, remote_root, remote_path))


def _require_route_for_import(config, remote_path: str) -> tuple[str, str]:
    """导入前必须归属启用的提供商路由（provider 可为 other）。

    返回 (route_id, provider_id)。未归类时拒绝导入（浏览不受限）。
    """
    routes = _routes_from_config(config)
    route_id, provider_id = provider_for_remote(routes, remote_path)
    if not route_id and provider_id == PROVIDER_OTHER:
        raise HTTPException(
            status_code=400,
            detail="该目录尚未归类到内容提供商，请先在设置页配置来源目录路由（或归类为其他远程来源）",
        )
    return route_id, provider_id


def _fetch_dir_page(
    client: OpenListClient,
    path: str,
    page: int,
    per_page: int,
    *,
    refresh: bool = False,
) -> dict:
    """只请求当前页（绝不偷偷拉后续页），返回分页载荷（白名单字段）。

    - total > 0 时 has_more = page*per_page < total；
    - total 未知（0）时 has_more = len(entries) == per_page（满页推断），
      且不把本页数量冒充 total。
    """
    try:
        dir_page = client.list_dir(path, page=page, per_page=per_page, refresh=refresh)
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
        for item in dir_page.entries
    ]
    total = int(getattr(dir_page, "total", 0) or 0)
    if total > 0:
        has_more = page * per_page < total
    else:
        has_more = len(entries) == per_page
    return {
        "entries": entries,
        "page": int(page),
        "per_page": int(per_page),
        "total": total,
        "has_more": has_more,
    }


def _collect_immediate_children_bounded(
    client: OpenListClient,
    path: str,
    *,
    refresh: bool = False,
    limit: int = _BROWSE_MAX_ENTRIES,
) -> tuple[list[dict], bool]:
    """有界收集单层全部直接子项（provider route discovery 专用），不递归。

    与 UI browse 的逐页拉取（_fetch_dir_page）分离，不共用含糊 helper；
    结果不超过 limit 项，超限裁剪并标记 truncated；强制刷新只在第一页传
    refresh=true（避免分页过程中反复刷新）。
    """
    entries: list[dict] = []
    page = 1
    last_total = 0
    truncated = False
    while True:
        try:
            dir_page = client.list_dir(
                path,
                page=page,
                per_page=_BROWSE_PER_PAGE,
                refresh=refresh and page == 1,
            )
        except OpenListError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        entries.extend(
            {
                "name": item.name,
                "is_dir": item.is_dir,
                "size": item.size,
                "modified": item.modified,
                "remote_path": item.remote_path,
            }
            for item in dir_page.entries
        )
        last_total = dir_page.total or 0
        if len(entries) >= limit:
            entries = entries[:limit]
            truncated = True
            break
        if not dir_page.entries:
            break
        if last_total and len(entries) >= last_total:
            break
        if len(dir_page.entries) < _BROWSE_PER_PAGE:
            break
        page += 1
    return entries, truncated


def _fetch_dir_entries(client: OpenListClient, path: str, *, refresh: bool = False) -> tuple[list[dict], bool]:
    """兼容别名：等价于有界收集器（旧测试/旧调用保留引用，UI browse 不使用）。"""
    return _collect_immediate_children_bounded(client, path, refresh=refresh)


def _get_refresh_executor():
    """有界 SWR 刷新线程池：最多 4 个并发网络线程，队列受 _REFRESH_MAX_ACTIVE 限制。"""
    global _refresh_executor
    with _refresh_executor_lock:
        if _refresh_executor is None:
            from concurrent.futures import ThreadPoolExecutor

            _refresh_executor = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="openlist-swr",
            )
        return _refresh_executor


def _schedule_background_refresh(
    conn_key: str,
    remote_path: str,
    server_url: str,
    username: str,
    password: str,
    ttl_minutes: int,
    *,
    page: int = 1,
    per_page: int = _BROWSE_PER_PAGE,
) -> None:
    """过期缓存的后台刷新：refresh=false 重新读取「当前页」并更新；失败保留旧缓存。

    按「连接 + 路径 + 页码」去重；有界线程池执行，超过上限时跳过本次（下次访问再刷新）。
    只刷新请求的页码，不刷新整个目录。
    """
    global _refresh_active
    key = (conn_key, remote_path, int(page), int(per_page))
    with _refresh_guard:
        if _refresh_inflight.get(key):
            return
        if _refresh_active >= _REFRESH_MAX_ACTIVE:
            return
        _refresh_inflight[key] = True
        _refresh_active += 1

    def worker() -> None:
        global _refresh_active
        try:
            # 模块 1 冷却拦截：冷却中保留旧缓存，不发任何请求
            allowed, _health = source_health.peek_request_allowed(
                governor_connection_key(server_url, username)
            )
            if not allowed:
                return
            client = get_openlist_client(
                server_url, username, password, client_factory=OpenListClient,
            )
            page_payload = _fetch_dir_page(
                client, remote_path, int(page), int(per_page), refresh=False
            )
            write_cache(
                conn_key, remote_path, page_payload["entries"], ttl_minutes,
                page=page, per_page=per_page,
                total=page_payload["total"], has_more=page_payload["has_more"],
            )
        except Exception:
            # 后台刷新失败：保留最后一次有效缓存，前端显示非阻塞提示
            pass
        finally:
            with _refresh_guard:
                _refresh_inflight.pop(key, None)
                _refresh_active -= 1

    try:
        _get_refresh_executor().submit(worker)
    except RuntimeError:
        # 解释器关闭等极端情况：直接以同步方式降级完成
        worker()


# ============================================================
# 连接端点
# ============================================================

@router.post("/test-connection")
def test_connection(req: TestConnectionRequest):
    """连接测试：对**当前候选**执行 Fresh Probe（只读，不保存任何配置）。

    凭据解析语义固定：
    - username / password 都为空 → KEEP SAVED（使用已保存凭据）；
    - username 与已保存账号相同、password 为空 → 使用已保存密码；
    - 显式填写新 password → 使用新密码；
    - 修改 username 但 password 为空 → 拒绝（禁止把旧账号密码套给新账号）。

    探测使用全新 client（不进入 production pool），draft remote_root 会真正
    参与 ``/api/fs/list`` 请求；响应只含固定 ``code`` / ``phase`` / 安全消息，
    用户名、密码、Token、Authorization 与服务端原始错误一律不回传前端。
    """
    config = load_config()
    server_url = (req.server_url or config.openlist_server_url).strip()
    # username 统一 strip，与 save 端点语义一致（审计 L2：带首尾空格的
    # username 不得被误判为「新账号」而要求密码）
    req_username = req.username.strip() if req.username else ""
    username = req_username if req_username else config.openlist_username
    password = req.password if req.password else config.openlist_password

    # 修改 username 但未提供新密码：禁止把旧账号密码套给新账号
    if req_username and req_username != config.openlist_username and not req.password:
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
            "message": _NOT_CONFIGURED_MESSAGE,
        }

    # draft remote_root 纳入测试契约：页面当前显示什么，就真正读取什么
    remote_root = (req.remote_root or config.openlist_remote_root or "/").strip()

    result = probe_openlist_connection(
        server_url=server_url,
        remote_root=remote_root,
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
    if (
        not result.ok
        and result.code == "invalid_configuration"
        and "明文传输密码" in result.message
    ):
        payload["insecure_http_required"] = True
    return payload

@router.post("/config")
def save_connection_config(req: SaveConfigRequest):
    """保存 OpenList 连接配置（validate-before-commit，preflight + 补偿回滚）。

    Remote-affecting（首次配置 / server_url / username / 显式 password /
    remote_root 变化）：
        candidate → fresh probe → success → atomic commit
    Local-only（mount_root / cache TTL / prefetch 等）：
        不要求 OpenList 在线，直接保存。

    任何 probe / 凭据读写 / 配置写失败都保持旧状态（0 mutation）；路由与
    runtime client pool 只在 probe + durable save 全部成功之后才更新。
    用户名/密码进入 Windows Credential Manager；保存后不返回任何凭据。
    """
    ok, reason = validate_server_url(req.server_url)
    if not ok:
        if "公网 HTTP" in reason:
            reason = "公网 HTTP 明文传输不安全，已拒绝保存；请改用 HTTPS"
        raise HTTPException(status_code=400, detail=reason)
    if req.server_url.startswith("http://") and not _is_loopback_http(req.server_url) and not req.allow_insecure_http:
        raise HTTPException(
            status_code=400,
            detail="本地/局域网 HTTP 将以明文传输密码，请确认风险后保存",
        )
    remote_root = normalize_remote_path(req.remote_root)
    mount_root = req.mount_root.strip()
    if len(mount_root) == 2 and mount_root[0].isalpha() and mount_root[1] == ":":
        mount_root += "\\"
    if not mount_root:
        raise HTTPException(status_code=400, detail="请填写 OpenList 对应的本地挂载根路径")

    config = load_config()
    old_server = normalize_openlist_server_url(config.openlist_server_url) if config.openlist_server_url else ""
    old_username = config.openlist_username or ""
    old_password = config.openlist_password or ""
    old_remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    new_server = normalize_openlist_server_url(req.server_url)
    username = req.username.strip()
    password = req.password or ""

    # 更换 username 但未提供新密码：禁止把旧账号密码套给新账号
    if username and username != old_username and not password:
        raise HTTPException(status_code=400, detail="请输入新 OpenList 账号对应的密码")

    # 凭据存储暂时不可读：不能安全提交（防止通用持久化把空字段解释成 delete）
    if openlist_credential_state() == "unavailable":
        raise HTTPException(
            status_code=503,
            detail="本机凭据管理器暂时不可用，无法安全保存 OpenList 配置，请稍后重试",
        )

    # 有效凭据解析（KEEP SAVED 语义）
    effective_username = username or old_username
    effective_password = password or old_password

    # Remote-affecting 判定（首次配置也算 remote-affecting；显式 password
    # 只有与已保存值不同才算更新，相同内容重复保存保持幂等）
    password_changed = bool(password) and password != old_password
    remote_affecting = (
        not config.openlist_server_url
        or new_server != old_server
        or effective_username != old_username
        or password_changed
        or remote_root != old_remote_root
    )

    # candidate 构建（禁止原地修改 cached AppConfig）
    candidate = copy.deepcopy(config)
    candidate.openlist_server_url = new_server
    candidate.openlist_remote_root = remote_root
    candidate.openlist_mount_root = mount_root
    if effective_username:
        candidate.openlist_username = effective_username
    if password:
        candidate.openlist_password = password
    if req.cache_ttl_minutes is not None:
        candidate.openlist_cache_ttl_minutes = max(1, min(int(req.cache_ttl_minutes), 60 * 24 * 30))
    if req.prefetch_limit is not None:
        candidate.openlist_prefetch_limit = max(0, min(int(req.prefetch_limit), _PREFETCH_MAX))

    # validate-before-commit：remote-affecting 必须先真实 probe 成功
    if remote_affecting:
        if not effective_username or not effective_password:
            raise HTTPException(status_code=400, detail=_NOT_CONFIGURED_MESSAGE)
        probe = probe_openlist_connection(
            server_url=new_server,
            remote_root=remote_root,
            username=effective_username,
            password=effective_password,
            allow_insecure_http=req.allow_insecure_http,
        )
        if not probe.ok:
            message = probe.message
            if probe.code == "invalid_configuration" and "明文传输密码" in message:
                message += "（请确认风险后重试）"
            raise HTTPException(status_code=400, detail=message)

    # connection_changed：身份（server/username/remote_root）任一变化
    connection_changed = (
        new_server != old_server
        or effective_username != old_username
        or remote_root != old_remote_root
    )

    # probe + durable save 全部成功之后才能清 route（失败时 routes unchanged）。
    # routes 清空并入 candidate 一次性原子提交，避免「新身份 + 旧路由」的
    # 二次写窗口与冗余的第二次凭据往返（审计 M3）。
    old_routes_existed = bool(config.openlist_routes)
    if connection_changed and old_routes_existed:
        candidate.openlist_routes = []

    # atomic commit：凭据写入失败 / JSON 写失败都会补偿回滚（config._persist_config_payload）
    try:
        save_config(candidate)
    except CredentialStoreError:
        # 区分两类失败：
        # - 凭据存储读失败中止（0 mutation，无需恢复，只是暂时不可用）；
        # - 凭据写入失败且补偿回滚失败（本机凭据可能不一致）。
        # 这里统一按写入失败处理，但消息不再误导「凭据已损坏」。
        raise HTTPException(
            status_code=500,
            detail="OpenList 配置保存失败，本机凭据服务异常，请稍后重试",
        ) from None
    except OSError:
        raise HTTPException(status_code=500, detail="OpenList 配置保存失败，请稍后重试") from None

    # durable save 成功之后才更新 runtime 状态
    if connection_changed or password_changed:
        # 密码不参与匿名会话键；用户重新填写密码时也必须丢弃旧内存 Token，
        # 避免新配置仍携带旧会话继续请求。
        clear_openlist_client_pool()

    message = "OpenList 连接配置已保存"
    if connection_changed and old_routes_existed:
        message = "OpenList 连接已保存；连接身份已变化，旧来源目录路由已清空，请重新发现并确认"
    return {"ok": True, "message": message}


# ============================================================
# 懒加载浏览与缓存
# ============================================================

@router.get("/browse")
def browse(path: str = "", page: int = 1, per_page: int = _BROWSE_PER_PAGE, refresh: bool = False):
    """单层目录浏览（真分页）：一次请求只拉一页，绝不递归扫描。

    - page >= 1，1 <= per_page <= 100（默认 100）；
    - total > 0 → has_more = page*per_page < total；total=0（未知）→
      has_more = 本页是否满页（len(entries) == per_page）；
    - 普通浏览（refresh=false）：优先本地分页缓存；缓存新鲜直接返回（不触碰上游）；
      缓存过期返回 stale 数据并后台只刷新当前页；无缓存才请求 OpenList；
    - 显式强制刷新（refresh=true）：只刷新请求的页码并更新缓存，不递归后代；
      失败时保留最后一次有效缓存。
    """
    if page < 1:
        raise HTTPException(status_code=400, detail="page 必须大于等于 1")
    if per_page < 1 or per_page > 100:
        raise HTTPException(status_code=400, detail="per_page 必须在 1 到 100 之间")

    config = load_config()
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    path = normalize_remote_path(path) if path else remote_root
    _ensure_within_remote_root(remote_root, path)

    parent_path = None
    if remote_root != "/" and path != remote_root:
        parent_path = str(PurePosixPath(path).parent)

    ttl_minutes = max(1, int(config.openlist_cache_ttl_minutes or 1440))
    conn_key = connection_key(
        config.openlist_server_url,
        config.openlist_username,
        remote_root,
    )

    # 模块 1 冷却拦截：发请求前检查连接健康（与 OpenListClient 上报同一连接键）。
    # 冷却中：fresh 缓存直接返回（标注 health）；无 fresh 缓存则拒绝请求，
    # 不发起任何网络请求。
    allowed, _health = source_health.peek_request_allowed(
        governor_connection_key(config.openlist_server_url, config.openlist_username)
    )
    if not allowed:
        if not refresh:
            cached = read_cache(conn_key, path, page=page, per_page=per_page)
            if cached is not None and cached["fresh"]:
                return _browse_payload(
                    path, parent_path, remote_root, cached["entries"],
                    page=cached["page"], per_page=cached["per_page"],
                    total=cached["total"], has_more=cached["has_more"],
                    cache={"cached": True, "status": "fresh", "refreshing": False,
                           "refresh_failed": False, "health": "cooling_down",
                           "fetched_at": cached["fetched_at"],
                           "expires_at": cached["expires_at"]},
                )
        raise HTTPException(
            status_code=423,
            detail="远端网盘疑似触发访问保护，KumiPlayer 已暂停该来源的自动请求",
        )

    if not refresh:
        cached = read_cache(conn_key, path, page=page, per_page=per_page)
        if cached is not None:
            if cached["fresh"]:
                # 缓存命中：不构造客户端、不触碰上游
                return _browse_payload(
                    path, parent_path, remote_root, cached["entries"],
                    page=cached["page"], per_page=cached["per_page"],
                    total=cached["total"], has_more=cached["has_more"],
                    cache={"cached": True, "status": "fresh", "refreshing": False,
                           "refresh_failed": False, "fetched_at": cached["fetched_at"],
                           "expires_at": cached["expires_at"]},
                )
            # stale-while-revalidate：先显示缓存，后台只刷新当前页
            _schedule_background_refresh(
                conn_key, path,
                config.openlist_server_url,
                config.openlist_username,
                config.openlist_password,
                ttl_minutes,
                page=page, per_page=per_page,
            )
            return _browse_payload(
                path, parent_path, remote_root, cached["entries"],
                page=cached["page"], per_page=cached["per_page"],
                total=cached["total"], has_more=cached["has_more"],
                cache={"cached": True, "status": "stale", "refreshing": True,
                       "refresh_failed": False, "fetched_at": cached["fetched_at"],
                       "expires_at": cached["expires_at"]},
            )

    client = _client_from_config(config)  # 需要实际请求时才构造客户端
    try:
        page_payload = _fetch_dir_page(client, path, page, per_page, refresh=refresh)
    except HTTPException as exc:
        if refresh:
            cached = read_cache(conn_key, path, page=page, per_page=per_page)
            if cached is not None:
                # 显式强制刷新失败：保留旧缓存，非阻塞提示
                return _browse_payload(
                    path, parent_path, remote_root, cached["entries"],
                    page=cached["page"], per_page=cached["per_page"],
                    total=cached["total"], has_more=cached["has_more"],
                    cache={"cached": True, "status": "stale", "refreshing": False,
                           "refresh_failed": True, "error": str(exc.detail),
                           "fetched_at": cached["fetched_at"],
                           "expires_at": cached["expires_at"]},
                )
        raise exc

    write_cache(
        conn_key, path, page_payload["entries"], ttl_minutes,
        page=page, per_page=per_page,
        total=page_payload["total"], has_more=page_payload["has_more"],
    )
    payload = _browse_payload(
        path, parent_path, remote_root, page_payload["entries"],
        page=page, per_page=per_page,
        total=page_payload["total"], has_more=page_payload["has_more"],
        cache={"cached": False, "status": "none", "refreshing": False,
               "refresh_failed": False, "fetched_at": None, "expires_at": None},
    )
    if refresh:
        payload["refresh_requested"] = True
    return payload


def _browse_payload(
    path: str,
    parent_path: str | None,
    remote_root: str,
    entries: list[dict],
    *,
    page: int = 1,
    per_page: int = _BROWSE_PER_PAGE,
    total: int = 0,
    has_more: bool | None = None,
    truncated: bool = False,
    cache: dict,
) -> dict:
    """分页浏览响应载荷。

    total > 0 → has_more = page*per_page < total；total 未知（0）→ 优先用显式
    has_more，否则按满页推断（len(entries) == per_page）。truncated 仅由有界
    收集器（route discovery）使用，不再代表浏览 1000 截断。
    """
    total = int(total or 0)
    if total > 0:
        has_more_value = page * per_page < total
    elif has_more is not None:
        has_more_value = bool(has_more)
    else:
        has_more_value = len(entries) == per_page
    return {
        "path": path,
        "parent_path": parent_path,
        "remote_root": remote_root,
        "entries": entries,
        "page": int(page),
        "per_page": int(per_page),
        "total": total,
        "has_more": has_more_value,
        "truncated": bool(truncated),
        "cache": cache,
    }


@router.post("/prefetch")
def prefetch(req: PrefetchRequest):
    """有界预取：单并发、只拉取指定路径的一层列表并写入本地缓存。

    - 每次调用递增 generation；worker 在每个路径前检查 generation，
      新一代预取（含空请求取消）会使旧 generation 停止启动未处理路径；
    - prefetch_limit=0 时真正关闭预取（不发请求）；
    - 绝不递归后代；已有未过期缓存的路径跳过；全局忙时直接跳过本次。
    """
    global _prefetch_generation
    config = load_config()
    with _prefetch_generation_guard:
        _prefetch_generation += 1
        my_generation = _prefetch_generation
    if not (config.openlist_server_url and config.openlist_username and config.openlist_password):
        return {"prefetched": 0, "skipped": 0, "busy": False, "cancelled": True}
    limit = max(0, min(int(config.openlist_prefetch_limit if config.openlist_prefetch_limit is not None else 12), _PREFETCH_MAX))
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    if limit <= 0 or not req.paths:
        # 空请求或预取关闭：仅递增 generation（使旧预取停止），不发请求
        return {"prefetched": 0, "skipped": 0, "busy": False, "cancelled": True}

    paths: list[str] = []
    for raw in req.paths[:limit]:
        try:
            path = normalize_remote_path(raw)
        except OpenListError:
            continue
        if not is_ancestor_or_self(remote_root, path):
            continue
        if path not in paths:
            paths.append(path)

    if not _prefetch_guard.acquire(blocking=False):
        return {"prefetched": 0, "skipped": len(paths), "busy": True}

    # 模块 1 冷却拦截：冷却中不发任何请求，直接返回空结果 + health 标注
    allowed, _health = source_health.peek_request_allowed(
        governor_connection_key(config.openlist_server_url, config.openlist_username)
    )
    if not allowed:
        _prefetch_guard.release()
        return {
            "prefetched": 0, "skipped": 0, "busy": False, "cancelled": False,
            "health": "cooling_down",
        }

    ttl_minutes = max(1, int(config.openlist_cache_ttl_minutes or 1440))
    conn_key = connection_key(
        config.openlist_server_url,
        config.openlist_username,
        remote_root,
    )
    try:
        prefetched = 0
        skipped = 0
        client: OpenListClient | None = None
        for path in paths:
            # 新一代预取已到来：停止启动未处理路径
            with _prefetch_generation_guard:
                if _prefetch_generation != my_generation:
                    break
            cached = read_cache(conn_key, path, page=1, per_page=_BROWSE_PER_PAGE)
            if cached is not None and cached["fresh"]:
                skipped += 1
                continue
            try:
                if client is None:
                    client = get_openlist_client(
                        config.openlist_server_url,
                        config.openlist_username,
                        config.openlist_password,
                        client_factory=OpenListClient,
                    )
                # 预取只拉当前层 page 1（有上限、不递归）；继续走 OpenListClient
                #（Module 1 governor + circuit breaker 自动覆盖），不得绕过。
                page_payload = _fetch_dir_page(client, path, 1, _BROWSE_PER_PAGE, refresh=False)
                write_cache(
                    conn_key, path, page_payload["entries"], ttl_minutes,
                    page=1, per_page=_BROWSE_PER_PAGE,
                    total=page_payload["total"], has_more=page_payload["has_more"],
                )
                prefetched += 1
            except Exception:
                # 预取失败静默：不影响浏览主链路
                skipped += 1
    finally:
        _prefetch_guard.release()
    return {"prefetched": prefetched, "skipped": skipped, "busy": False, "cancelled": False}


# ============================================================
# 提供商路由
# ============================================================

def _route_public(route: OpenListRouteConfig, config) -> dict:
    local_path = ""
    local_available = False
    try:
        mount_root = (config.openlist_mount_root or "").strip()
        remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
        if mount_root and is_ancestor_or_self(remote_root, route.remote_prefix):
            local_path = derive_local_path(mount_root, remote_root, route.remote_prefix)
            local_available = Path(local_path).expanduser().is_dir()
    except (ValueError, OSError):
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


@router.get("/routes")
def get_routes():
    """读取提供商路由（含自动推导的本地路径只读预览，不含任何凭据）。"""
    config = load_config()
    routes = _routes_from_config(config)
    return {"routes": [_route_public(route, config) for route in routes]}


@router.post("/routes/discover")
def discover_routes():
    """从远端总根读取直接子目录（仅一层），按目录名给出提供商建议。

    建议值绝不自动保存为事实；用户确认后通过 PUT /routes 保存。
    """
    config = load_config()
    client = _client_from_config(config)
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    try:
        # 有界收集器：读配置根目录全部直接子项（有数量上限），与 UI browse 逐页拉取分离
        entries, _truncated = _collect_immediate_children_bounded(client, remote_root, refresh=False)
    except HTTPException as exc:
        raise exc
    existing = {normalize_route_prefix(route.remote_prefix): route for route in _routes_from_config(config)}
    items = []
    for entry in entries:
        if not entry.get("is_dir"):
            continue
        name = str(entry.get("name") or "")
        prefix = normalize_route_prefix(entry.get("remote_path") or "")
        known = existing.get(prefix)
        items.append(
            {
                "name": name,
                "remote_prefix": prefix,
                "hint_provider": hint_provider_for_name(name),
                "current_provider": known.provider_id if known else "",
                "current_label": known.label if known else "",
            }
        )
    return {"remote_root": remote_root, "items": items}


@router.put("/routes")
def save_routes(req: SaveRoutesRequest):
    """保存提供商路由表。

    校验：前缀合法且不是连接根、provider 可路由、同一前缀不重复配置、
    路由位于远端总根之下。返回每条路由的本地路径只读预览与可访问性。
    """
    config = load_config()
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    if not (config.openlist_mount_root or "").strip():
        raise HTTPException(status_code=400, detail=_NOT_CONFIGURED_MESSAGE)

    seen: dict[str, str] = {}
    routes: list[OpenListRouteConfig] = []
    for item in req.routes:
        try:
            prefix = normalize_route_prefix(item.remote_prefix)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except OpenListError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not is_ancestor_or_self(remote_root, prefix):
            raise HTTPException(status_code=400, detail=f"路由前缀 {prefix} 不在远端总根之下")
        if prefix in seen:
            raise HTTPException(status_code=400, detail=f"远端前缀重复配置：{prefix}")
        seen[prefix] = item.route_id
        provider_id = (item.provider_id or PROVIDER_OTHER).strip()
        if provider_id not in ROUTABLE_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"未知内容提供商：{provider_id}")
        label = (item.label or "").strip() or PurePosixPath(prefix).name or "未命名来源"
        route = OpenListRouteConfig(
            route_id=item.route_id or new_route_id(),
            label=label,
            remote_prefix=prefix,
            provider_id=provider_id,
            enabled=bool(item.enabled),
        )
        routes.append(route)

    config.openlist_routes = routes
    save_config(config)
    return {"routes": [_route_public(route, config) for route in routes]}


@router.post("/presets/{preset_id}/rescan")
@_admitted_import_endpoint
def rescan_openlist_preset(preset_id: str):
    """按预设保存的远端定位更新（Source Catalog 增量链路）（模块4：preset rescan → Source Catalog 增量链路）。

    预设远端定位 → 来源根覆盖解析（exact 复用 / 既有祖先覆盖复用 /
    新父覆盖既有后代时事务化归并 / 缺失时一次性注册）→
    bump generation → enqueue durable discovery scan
    （reuse 系列 incremental；promote_parent 后 full）。
    返回 durable job_id 与 resolution，前端既有 /api/tasks polling 继续可用。
    """
    from app.catalog import lifecycle
    from app.catalog import store as catalog_store
    from app.db.database import init_db
    from app.pipeline import orchestrator

    init_db()
    config = load_config()
    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="媒体库预设不存在")
    if preset.source != "openlist":
        raise HTTPException(status_code=400, detail="该媒体库不是 OpenList 来源")
    if not preset.remote_locator:
        raise HTTPException(status_code=400, detail="该媒体库缺少 OpenList 远端定位，请重新导入")
    _client_from_config(config)  # 未配置时快速失败

    source_id = _openlist_source_id(config)
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    normalized = normalize_remote_path(preset.remote_locator)
    _ensure_within_remote_root(remote_root, normalized)
    _ensure_openlist_source(catalog_store, source_id)
    _require_route_for_import(config, normalized)

    resolution = lifecycle.resolve_root_for_import(source_id, normalized)
    scan_mode = "incremental"
    if resolution.action == "create":
        # 一次性注册（参考 import-batch：路由归属校验 + overlap 安全失败）
        try:
            root = catalog_store.create_source_root(
                source_id=source_id,
                remote_locator=normalized,
                local_locator=derive_local_path(config.openlist_mount_root, remote_root, normalized),
                import_family=(preset.import_family or "anime").strip(),
                import_scope=(preset.import_scope or "").strip(),
                scan_policy="standard",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"预设远端定位与既有来源根重叠，拒绝扩大扫描范围：{exc}",
            ) from None
    elif resolution.action == "promote_parent":
        # 新父目录覆盖既有后代：事务化归并（unit/revision 保留），随后 full 扫描
        resolution = lifecycle.promote_parent_root(
            source_id,
            normalized,
            local_locator=derive_local_path(config.openlist_mount_root, remote_root, normalized),
            import_family=(preset.import_family or "anime").strip(),
            import_scope=(preset.import_scope or "").strip(),
            child_root_ids=resolution.covered_root_ids,
        )
        root = catalog_store.get_source_root(resolution.canonical_root_id)
        if root is None:
            raise HTTPException(status_code=409, detail="来源根归并失败，请重试")
        scan_mode = "full"
    else:
        # reuse_exact / reuse_ancestor：复用既有 root，不创建新 root
        root = catalog_store.get_source_root(resolution.canonical_root_id)
        if root is None:
            raise HTTPException(status_code=409, detail="来源根解析失败，请重新导入")
        if resolution.action == "reuse_ancestor":
            # 更新既有祖先根的 family/scope 为本次预设语义（不改变 locator）
            catalog_store.update_root_metadata(
                root.root_id,
                import_family=(preset.import_family or "anime").strip(),
                import_scope=(preset.import_scope or "").strip(),
            )

    root_id = root.root_id
    generation = catalog_store.bump_generation(root_id)
    job_id = orchestrator.enqueue_scan(root_id, generation, source_id, scan_mode=scan_mode)
    return {
        "task_id": job_id,
        "root_id": root_id,
        "generation": generation,
        "execution_mode": "durable",
        "resolution": lifecycle.resolution_api_label(resolution.action),
        "requested_locator": normalized,
        "canonical_locator": resolution.canonical_locator,
        "scan_mode": scan_mode,
        "covered_root_ids": list(resolution.covered_root_ids),
    }


# ============================================================
# Durable batch API（v2：import-batch → discovery job → SQLite revision）
# ============================================================

def _openlist_source_id(config) -> str:
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    identity = connection_key(
        config.openlist_server_url,
        config.openlist_username,
        remote_root,
    )
    return f"openlist-{identity}"


#: OpenList 来源能力声明（模块4：Source Catalog 主链路）。
#: native_delta=False：OpenList 无原生增量接口；依赖目录验证 + 滚动对账收敛。
_OPENLIST_CAPABILITIES = {
    "native_delta": False,
    "directory_verification": True,
    "rolling_reconciliation": True,
}


def _ensure_openlist_source(catalog_store, source_id: str) -> None:
    """创建（或复用）OpenList 来源记录并写入能力声明（幂等）。"""
    catalog_store.create_source(
        source_id=source_id,
        source_type="openlist",
        provider_id="openlist",
        ingest_method="openlist_api",
        connection_key=source_id,
        display_name="OpenList",
    )
    catalog_store.set_source_capabilities(source_id, _OPENLIST_CAPABILITIES)


def _batch_jobs(batch: dict) -> dict[str, object]:
    """按 root_id 找到其 discovery job，避免把 job_id 伪造为业务事实。

    优先匹配本批次的 generation（import-batch 时写入 batch root 的 generation），
    避免历史 job 残留干扰批次状态。
    """
    from app.jobs import store as job_store

    by_root: dict[str, object] = {}
    for root in batch.get("roots", []):
        jobs = job_store.list_discovery_jobs_for_root(root["root_id"])
        if not jobs:
            continue
        target_gen = root.get("generation")
        picked = next(
            (
                j for j in jobs
                if target_gen is not None
                and int(j.payload.get("generation") or 0) == int(target_gen)
            ),
            None,
        )
        by_root[root["root_id"]] = picked or jobs[0]
    return by_root


def _refresh_batch_status(batch: dict, *, persist: bool = True) -> dict:
    from app.pipeline.batch_status import refresh_batch_status

    return refresh_batch_status(batch, persist=persist)


def _validate_batch_paths(config, remote_paths: list[str]) -> list[str]:
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    if not remote_paths:
        raise HTTPException(status_code=400, detail="请先选择至少一个目录")
    if len(remote_paths) > _BATCH_MAX_PATHS:
        raise HTTPException(status_code=400, detail=f"一次最多导入 {_BATCH_MAX_PATHS} 个目录")
    normalized: list[str] = []
    for raw in remote_paths:
        path = normalize_remote_path(raw)
        _ensure_within_remote_root(remote_root, path)
        _require_route_for_import(config, path)
        if path not in normalized:
            normalized.append(path)
    ordered = sorted(normalized)
    for index, path in enumerate(ordered):
        for other in ordered[index + 1:]:
            if is_ancestor_or_self(path, other):
                raise HTTPException(
                    status_code=400,
                    detail=f"选择目录存在父子重叠：{path} 与 {other} 只能保留其中一个",
                )
    return normalized


@router.post("/import-batch")
@_admitted_import_endpoint
def create_openlist_import_batch(req: BatchImportRequest):
    """一次创建多个 OpenList source roots，并为每个 root 入队 durable discovery job。"""
    from app.catalog import store as catalog_store
    from app.db.database import init_db

    init_db()
    from app.integrations.openlist.providers import provider_for_remote
    from app.pipeline import orchestrator

    config = load_config()
    normalized = _validate_batch_paths(config, req.remote_paths)
    remote_root = normalize_remote_path(config.openlist_remote_root) if config.openlist_remote_root else "/"
    source_id = _openlist_source_id(config)
    _ensure_openlist_source(catalog_store, source_id)
    family = (req.import_family or "anime").strip()
    scope = (req.import_scope or "").strip()
    roots = []
    for remote_locator in normalized:
        route_id, provider_id = provider_for_remote(_routes_from_config(config), remote_locator)
        roots.append({
            "remote_locator": remote_locator,
            "local_locator": derive_local_path(config.openlist_mount_root, remote_root, remote_locator),
            "import_family": family,
            "import_scope": scope,
            "provider_id": provider_id,
            "source_route_id": route_id,
        })
    batch = None
    created_job_ids: list[str] = []
    preset_by_root: dict[str, dict] = {}
    # provider/route 是请求级事实（batch roots 不投影这些字段）
    provider_by_locator = {
        normalize_remote_path(str(r["remote_locator"])): (r["provider_id"], r["source_route_id"])
        for r in roots
    }
    try:
        batch = catalog_store.create_import_batch(
            source_id=source_id,
            roots=roots,
            import_family=family,
        )
        for root in batch["roots"]:
            generation = catalog_store.bump_generation(root["root_id"])
            # promote_parent 归并后必须 full 扫描（重新验证整棵已知目录树）
            scan_mode = (
                "full"
                if root.get("resolution") == "promoted_to_parent"
                else "incremental"
            )
            job_id = orchestrator.enqueue_scan(
                root["root_id"], generation, source_id, scan_mode=scan_mode,
            )
            created_job_ids.append(job_id)
            catalog_store.update_import_batch_root(
                batch["batch_id"], root["root_id"], status="queued", generation=generation,
            )
            root["generation"] = generation
            root["job_id"] = job_id
            # OpenList 来源卡：每个 canonical SourceRoot 同步一张长期媒体库
            # 入口卡（复用/创建），作为“我选中的这个 OpenList 媒体库根”的
            # 持久身份；不按 MediaUnit 建卡。
            from app.media_presets.service import sync_openlist_source_preset

            provider_id, source_route_id = provider_by_locator.get(
                normalize_remote_path(str(root["remote_locator"] or "")), ("", "")
            )
            preset, _created = sync_openlist_source_preset(
                catalog_root_id=root["root_id"],
                remote_locator=root["remote_locator"],
                local_locator=root.get("local_locator") or "",
                provider_id=provider_id,
                source_route_id=source_route_id,
                import_family=family,
                import_scope=scope,
            )
            preset_by_root[root["root_id"]] = {
                "preset_id": preset.preset_id,
                "name": preset.name,
                "remote_locator": preset.remote_locator,
                "catalog_root_id": preset.catalog_root_id,
                "created": _created,
            }
    except (ValueError, OSError) as exc:
        from app.db.database import get_connection
        from app.jobs import store as job_store

        for job_id in created_job_ids:
            job_store.cancel_job(job_id)
        if batch is not None:
            conn = get_connection()
            conn.execute("DELETE FROM import_batches WHERE batch_id = ?", (batch["batch_id"],))
            conn.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    payload = _refresh_batch_status(batch)
    payload["presets"] = list(preset_by_root.values())
    return payload


@router.get("/import-batches/{batch_id}")
def get_openlist_import_batch(batch_id: str):
    from app.catalog import store as catalog_store
    from app.db.database import init_db

    init_db()
    batch = catalog_store.get_import_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    return _refresh_batch_status(batch, persist=False)


@router.post("/import-batches/{batch_id}/full-validate")
@_admitted_import_endpoint
def full_validate_openlist_import_batch(batch_id: str):
    from app.catalog import store as catalog_store
    from app.db.database import init_db

    init_db()
    from app.jobs import store as job_store
    from app.pipeline import orchestrator

    batch = catalog_store.get_import_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    source_id = ""
    for root in batch["roots"]:
        source_id = root.get("source_id") or source_id
    for root in batch["roots"]:
        for job in job_store.list_discovery_jobs_for_root(root["root_id"]):
            if job.status in {"queued", "running"}:
                job_store.cancel_job(job.job_id)
        generation = catalog_store.bump_generation(root["root_id"])
        job_id = orchestrator.enqueue_scan(
            root["root_id"], generation, source_id, scan_mode="full",
        )
        catalog_store.update_import_batch_root(
            batch_id, root["root_id"], status="queued", generation=generation,
        )
        root["job_id"] = job_id
    return _refresh_batch_status(catalog_store.get_import_batch(batch_id) or batch)


@router.post("/import-batches/{batch_id}/cancel")
def cancel_openlist_import_batch(batch_id: str):
    from app.catalog import store as catalog_store
    from app.db.database import init_db
    from app.jobs import store as job_store

    init_db()
    batch = catalog_store.get_import_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    for root in batch["roots"]:
        for job in job_store.list_discovery_jobs_for_root(root["root_id"]):
            if job.status in {"queued", "running"}:
                job_store.cancel_job(job.job_id)
    return _refresh_batch_status(catalog_store.get_import_batch(batch_id) or batch)


@router.post("/import-batches/{batch_id}/units/{unit_id}/retry")
def retry_openlist_import_unit(batch_id: str, unit_id: str):
    """重试批次中一个失败/待处理的识别单元（exact-stage retry，幂等）。

    - 单元必须属于该批次；
    - 按当前 durable stage 精确定位失败阶段并只重试该阶段，按当前
      revision/unit 精确归属（mirror 按 resource_key ``mirror:{revision}``、
      scrape 按 mirror result 链 / payload.revision_id、library 按 scrape
      result 链 / payload.unit_id），**绝不读取全局 ``scrape:global`` /
      ``library:global`` 的“最近一条”跨作品串线**；
    - 终态 failed/cancelled job 重新入队复用同一 job 行（attempt+1），不新建
      业务任务，也不依赖 orchestrator 的本地未提交 rerun 参数；
    - revision 仍是 draft（needs_review）→ 拒绝，走人工确认入口；
    - 不重新扫描整个 SourceRoot；
    - 重复点击 / 页面刷新 / 进程重启均幂等（同一 job 行重入队）。
    """
    import json

    from app.catalog import maintenance_guard
    from app.catalog import store as catalog_store
    from app.db.database import get_connection, init_db
    from app.jobs import store as job_store
    from app.pipeline import orchestrator

    init_db()
    batch = catalog_store.get_import_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    unit = get_connection().execute(
        "SELECT * FROM media_units WHERE unit_id = ? AND root_id IN "
        "(SELECT root_id FROM import_batch_roots WHERE batch_id = ?)",
        (unit_id, batch_id),
    ).fetchone()
    if unit is None:
        raise HTTPException(status_code=404, detail="识别单元不存在于该批次")
    revision_id = str(unit["current_revision_id"] or "")
    if not revision_id:
        raise HTTPException(status_code=409, detail="该识别单元还没有可执行的识别版本")
    revision = get_connection().execute(
        "SELECT * FROM import_revisions WHERE revision_id = ? AND unit_id = ?",
        (revision_id, unit_id),
    ).fetchone()
    if revision is None:
        raise HTTPException(status_code=409, detail="识别版本不存在，请重新扫描")
    if revision["status"] not in ("confirmed", "executed"):
        raise HTTPException(
            status_code=409,
            detail="识别结果仍待人工确认，请先处理识别结果再重试",
        )

    def _latest_stage_job(job_type: str, resource_key: str):
        return get_connection().execute(
            "SELECT * FROM jobs WHERE job_type = ? AND resource_key = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (job_type, resource_key),
        ).fetchone()

    def _job_result(row) -> dict:
        try:
            return (json.loads(row["payload"] or "{}").get("result") or {})
        except (TypeError, ValueError):
            return {}

    def _latest_scrape_job_for_revision():
        """本 revision 的 scrape job：mirror result 链优先，payload 精确兜底。"""
        mirror_row = _latest_stage_job("mirror_revision", f"mirror:{revision_id}")
        if mirror_row is not None:
            scrape_id = _job_result(mirror_row).get("scrape_job_id") or ""
            if scrape_id:
                row = get_connection().execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (scrape_id,)
                ).fetchone()
                if row is not None:
                    return row
        return get_connection().execute(
            "SELECT * FROM jobs WHERE job_type = 'scrape_revision' AND payload LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f'%"revision_id": "{revision_id}"%',),
        ).fetchone()

    def _latest_library_job_for_unit():
        """本 unit 的 library rebuild job：scrape result 链优先，payload 精确兜底。"""
        scrape_row = _latest_scrape_job_for_revision()
        if scrape_row is not None:
            lib_id = _job_result(scrape_row).get("library_rebuild_job") or ""
            if lib_id:
                row = get_connection().execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (lib_id,)
                ).fetchone()
                if row is not None:
                    return row
        return get_connection().execute(
            "SELECT * FROM jobs WHERE job_type = 'library_rebuild' AND payload LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f'%"unit_id": "{unit_id}"%',),
        ).fetchone()

    def _requeue_terminal(job_row) -> str:
        """把终态 failed/cancelled job 重新入队（同一行 attempt+1），返回 job_id。

        维护屏障激活时 rerun_terminal_job 拒绝写入 → 转 409（与其它任务写
        入口一致），旧 job 保持终态不被误恢复。
        """
        from app.catalog import maintenance_guard

        job_id = str(job_row["job_id"])
        if job_row["status"] in ("failed", "cancelled"):
            try:
                job_store.rerun_terminal_job(job_id)
            except maintenance_guard.MaintenanceAdmissionDenied as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
        return job_id
    retried: dict[str, str] = {}
    already_active: dict[str, str] = {}

    def _stage_active(stage: str, job_row) -> None:
        """已有 queued/running stage：记录为 already_active，不伪报 retried。"""
        already_active[stage] = str(job_row["job_id"])

    mirror_job = _latest_stage_job("mirror_revision", f"mirror:{revision_id}")
    if mirror_job is None:
        # 尚无 mirror job → 正常补建（真实入队，属于本次实际调度）
        try:
            retried["mirror"] = orchestrator.enqueue_mirror(revision_id, unit_id)
        except maintenance_guard.MaintenanceAdmissionDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    elif mirror_job["status"] in ("failed", "cancelled"):
        retried["mirror"] = _requeue_terminal(mirror_job)
    elif mirror_job["status"] in ("queued", "running"):
        _stage_active("mirror", mirror_job)
    else:
        # mirror succeeded → 检查 scrape 阶段
        scrape_job = _latest_scrape_job_for_revision()
        if scrape_job is None:
            # 链路尚未推进（或无需刮削）：没有可重试的失败阶段
            pass
        elif scrape_job["status"] in ("failed", "cancelled"):
            retried["scrape"] = _requeue_terminal(scrape_job)
        elif scrape_job["status"] in ("queued", "running"):
            _stage_active("scrape", scrape_job)
        else:
            # scrape succeeded → 检查 library 阶段
            library_job = _latest_library_job_for_unit()
            if library_job is None:
                # library rebuild 尚未入队：没有可重试的失败阶段
                pass
            elif library_job["status"] in ("failed", "cancelled"):
                retried["library"] = _requeue_terminal(library_job)
            elif library_job["status"] in ("queued", "running"):
                _stage_active("library", library_job)
            # library succeeded：全链路完成，没有可重试的失败阶段

    if not retried and not already_active:
        raise HTTPException(
            status_code=409,
            detail="当前没有可重试的失败阶段",
        )
    payload = _refresh_batch_status(catalog_store.get_import_batch(batch_id) or batch, persist=False)
    payload["retried_stages"] = retried
    if already_active:
        payload["already_active_stages"] = already_active
    return payload
