"""OpenList HTTP 客户端。

职责：
- 登录（``POST /api/auth/login``）并缓存内存 Token；
- 单层目录分页读取（``POST /api/fs/list``）；
- 401 时单次重登后重试；
- 有限退避重试（超时 / 5xx / 429 尊重 Retry-After）；
- 错误归一化为 :class:`OpenListError` 安全消息。

安全边界：
- Token 仅驻留进程内，不写磁盘、不回传前端；
- 禁止跨主机重定向（``follow_redirects=False``，3xx 直接判错）；
- 服务地址只允许 http(s)；公网 HTTP 拒绝，本地/LAN HTTP 需显式确认；
- URL 中禁止嵌入用户名密码（userinfo）；
- 远端条目 name 必须通过危险字符校验；远端子路径一律由
  「当前请求目录 + 校验后的 name」构造，不信任服务端返回的 ``path``。

实现只依据 OpenList 公开 API 文档独立编写（AGPL-3.0 只约束其自身源码，
本模块不引入其源码、SDK 或二进制）。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.catalog import source_health
from app.integrations.openlist.governor import (
    OpenListRequestGovernor,
    get_governor,
    governor_connection_key,
)
from app.integrations.openlist.models import (
    OpenListAuthError,
    OpenListDirPage,
    OpenListEntry,
    OpenListError,
    OpenListNetworkError,
    OpenListNotFoundError,
    OpenListPermissionError,
    OpenListRateLimitedError,
    OpenListRedirectError,
    OpenListRiskControlError,
    OpenListSourceCoolingDownError,
    OpenListTimeoutError,
    OpenListValidationError,
)

# OpenList 分页上限（文档：per_page 最大 100，默认 30）
MAX_PER_PAGE = 100
_DEFAULT_PER_PAGE = 100
# 登录 / 目录接口路径
_LOGIN_PATH = "/api/auth/login"
_LIST_PATH = "/api/fs/list"
# 请求超时（秒）
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_CONNECT_TIMEOUT = 6.0
# 每个请求最大尝试次数（首次 + 重试）
_MAX_ATTEMPTS = 3
# 429 时最多等待秒数（尊重 Retry-After 但封顶，避免长时间挂死任务）
_MAX_RETRY_AFTER = 10.0
# Windows 与远端路径都禁止的字符
_UNSAFE_NAME_RE = re.compile(r'[<>:"|?*\x00-\x1f/\\]')


# 风控拦截页识别：仅当响应确认为风控 HTML 页面时才归类为 risk_control。
# 至少覆盖 2026-08-12 真实事件（115 阿里云盾）：HTTP 405 + text/html +
# errors.aliyun.com / 访问被阻断。不保存原始 HTML，不把 trace / Token /
# URL / Authorization 写入日志或前端。
_RISK_CONTROL_HTML_MARKERS = ("errors.aliyun.com", "访问被阻断")
# 风控页正文只做有限截断检查，防止把超大 HTML 读入内存
_RISK_CONTROL_BODY_LIMIT = 8192

_logger = logging.getLogger(__name__)


# ============================================================
# 服务地址校验
# ============================================================

def _is_private_or_loopback_host(host: str) -> bool:
    """判断主机是否为回环 / 私网 / 链路本地地址。

    仅凭主机名无法证明内网身份；http 域名一律按公网处理（拒绝）。
    """
    normalized = host.strip().lower().rstrip(".").lstrip("[")
    if normalized.rstrip("]") in {"localhost", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(normalized.rstrip("]"))
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def validate_server_url(url: str) -> tuple[bool, str]:
    """校验 OpenList 服务地址。

    规则：
    - 只允许 http / https；其他 scheme 拒绝；
    - URL 中禁止用户名密码（userinfo）；
    - 不允许 query 与 fragment；
    - HTTPS 任意主机；HTTP 仅回环/私网，且需要调用方显式确认风险。
    """
    value = (url or "").strip()
    if not value:
        return False, "请输入 OpenList 服务地址"
    try:
        parts = urlsplit(value)
    except ValueError:
        return False, "OpenList 服务地址格式不合法"
    if parts.scheme not in {"http", "https"}:
        return False, "OpenList 服务地址只支持 http 或 https"
    if not parts.hostname:
        return False, "OpenList 服务地址缺少主机名"
    if parts.username is not None or parts.password is not None:
        return False, "OpenList 服务地址中禁止包含用户名或密码"
    if parts.query or parts.fragment:
        return False, "OpenList 服务地址不允许携带查询参数或锚点"
    if parts.scheme == "http" and not _is_private_or_loopback_host(parts.hostname):
        return False, (
            "公网 HTTP 明文传输不安全，已拒绝；"
            "请使用 HTTPS，或填写局域网/本机地址并确认风险"
        )
    return True, ""


def normalize_openlist_server_url(url: str) -> str:
    """规范化 OpenList API 根地址。

    OpenList 的 WebDAV 入口通常是 ``/dav/``，但目录浏览与认证必须使用
    服务根下的 ``/api/...``。允许用户直接粘贴 WebDAV 地址，并在进入客户端
    前归一化为对应 API 根地址。
    """
    value = (url or "").strip()
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if path.casefold() == "/dav":
        path = ""
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def normalize_remote_path(path: str) -> str:
    """规范化远端路径：必须 ``/`` 开头、无 ``..`` / ``.`` 段、无尾斜杠。"""
    value = (path or "").strip()
    if not value:
        return "/"
    if not value.startswith("/"):
        value = "/" + value
    parts: list[str] = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise OpenListValidationError("远端路径包含非法段（..），已拒绝")
        if _UNSAFE_NAME_RE.search(part):
            raise OpenListValidationError("远端路径包含非法字符，已拒绝")
        parts.append(part)
    return "/" + "/".join(parts)


def validate_entry_name(name: str) -> str:
    """校验远端条目名；返回去除首尾空白后的安全名称。"""
    stripped = (name or "").strip()
    if not stripped:
        raise OpenListValidationError("OpenList 返回了空条目名，已跳过")
    if stripped in {".", ".."}:
        raise OpenListValidationError("OpenList 返回了危险条目名，已跳过")
    if _UNSAFE_NAME_RE.search(stripped):
        raise OpenListValidationError("OpenList 返回了包含非法字符的条目名，已跳过")
    if stripped.endswith("."):
        raise OpenListValidationError("OpenList 返回了 Windows 非法结尾的条目名，已跳过")
    return stripped


def join_remote_path(parent: str, name: str) -> str:
    """由「当前请求目录 + 校验后的 name」构造远端子路径。"""
    parent = normalize_remote_path(parent)
    if parent == "/":
        return "/" + name
    return parent + "/" + name


def _safe_modified(value: Any) -> float | None:
    """把 modified 字段防御性转成 Unix 秒；非法值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# ============================================================
# HTTP 客户端
# ============================================================

class OpenListClient:
    """OpenList 只读客户端。

    构造时校验服务地址；``username`` / ``password`` 由调用方传入
    （来自配置与凭据管理器），本类不读取配置、不落盘。
    """

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        max_attempts: int = _MAX_ATTEMPTS,
        max_retry_after: float = _MAX_RETRY_AFTER,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
        governor: OpenListRequestGovernor | None = None,
    ):
        ok, reason = validate_server_url(server_url)
        if not ok:
            raise OpenListValidationError(reason)
        self.server_url = normalize_openlist_server_url(server_url)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_attempts = max_attempts
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._transport = transport  # 测试注入 MockTransport 用
        self._token: str | None = None
        # 连接级限速与健康上报：默认接入进程内共享单例
        self._governor = governor if governor is not None else get_governor()
        #: 连接匿名键（sha256(server_url|username)），用于限速与 source_health 记录
        self._conn_key = governor_connection_key(self.server_url, self.username)

    # -- 健康上报（最终结果；失败记录不改变请求错误语义） ------------------

    def _report_success(self) -> None:
        try:
            source_health.record_success(self._conn_key)
        except Exception:
            _logger.warning("source_health 记录成功状态失败（不影响请求语义）")

    def _report_failure(self, kind: str) -> None:
        try:
            source_health.record_failure(self._conn_key, kind)
        except Exception:
            _logger.warning("source_health 记录失败状态失败（不影响请求语义）")

    # -- 内部请求 ----------------------------------------------------

    def _client(self) -> httpx.Client:
        kwargs: dict = {
            "base_url": self.server_url,
            "timeout": httpx.Timeout(self.timeout, connect=self.connect_timeout),
            "follow_redirects": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "content-type": "application/json"}
        if self._token:
            # OpenList 约定：Authorization 直接携带 Token，无 "Bearer " 前缀
            headers["Authorization"] = self._token
        return headers

    def _retry_delay(self, attempt: int, retry_after: float = 0.0) -> float:
        if retry_after > 0:
            return min(retry_after, self.max_retry_after)
        return min(0.5 * (2 ** attempt), self.max_retry_after)

    @staticmethod
    def _looks_like_risk_control(response: httpx.Response) -> bool:
        """判断响应是否为风控拦截 HTML 页（仅检测特征，不保存正文）。"""
        if response.status_code != 405:
            return False
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return False
        try:
            body = response.content[:_RISK_CONTROL_BODY_LIMIT].decode("utf-8", errors="ignore")
        except Exception:
            return False
        return any(marker in body for marker in _RISK_CONTROL_HTML_MARKERS)

    def _post(
        self,
        path: str,
        payload: dict,
        *,
        retry_on_auth: bool = True,
    ) -> tuple[int, dict]:
        """POST JSON，统一做有限重试与错误归一化。

        返回 (http_status, body)。401 认证失败仅允许一次重登重试。

        网络准入（最后一道门）：**每个物理 attempt 之前**都执行
        「peek 快速预检 → governor 限速 acquire → source_health 最终准入
        → HTTP 请求」。

        - ``peek_request_allowed``（只读、不消费探针）：明确未到期冷却时
          直接拒绝，零网络请求、不进入限速队列；
        - ``governor.acquire``：连接级限速等待；
        - ``can_request``（唯一消费探针的入口）：**governor 等待期间**
          新建立的冷却（另一请求返回 405/429 触发）在此拦截——这是物理
          HTTP 前最后一道门，绝不在拿过 admission 后仍发请求。

        冷却中（含扫描中途 405/429 触发冷却后）在下一次 attempt 的准入处
        立即抛 :class:`OpenListSourceCoolingDownError`，不再发任何请求。

        429 语义：**第一次响应即结束**，不再 sleep+continue 隐藏重试；
        直接 raise :class:`OpenListRateLimitedError`，由外层统一上报
        ``record_failure('rate_limit')`` 进入来源冷却。

        风控识别优先级最高：明确 405 拦截页时**立即失败为 risk_control**，
        不做第二、第三次自动重试，避免继续向同一账号发请求。
        """
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            # 快速预检（只读、不消费探针）：明确未到期冷却 → 直接拒绝，
            # 不进入限速队列、不发任何请求
            peek_allowed, _ = source_health.peek_request_allowed(self._conn_key)
            if not peek_allowed:
                raise OpenListSourceCoolingDownError()
            # 连接级限速：同一连接键的请求间隔 >= 1/rate_per_second（锁外
            # sleep，不同连接互不阻塞）。
            self._governor.acquire(self._conn_key)
            # 真正最终准入（消费探针）：governor 等待期间新建立的冷却
            # （另一请求 405/429 触发）在此拦截——物理 HTTP 前最后一道门
            allowed, _ = source_health.can_request(self._conn_key)
            if not allowed:
                raise OpenListSourceCoolingDownError()
            try:
                with self._client() as client:
                    response = client.post(path, json=payload, headers=self._headers())
            except httpx.TimeoutException:
                last_error = OpenListTimeoutError()
                if attempt + 1 < self.max_attempts:
                    self._sleep(self._retry_delay(attempt))
                continue
            except httpx.HTTPError:
                last_error = OpenListNetworkError()
                if attempt + 1 < self.max_attempts:
                    self._sleep(self._retry_delay(attempt))
                continue

            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                raise OpenListRedirectError()

            # 明确风控拦截页：立即失败，绝不重试
            if self._looks_like_risk_control(response):
                raise OpenListRiskControlError()

            body = self._decode_body(response)
            code = int(body.get("code", status)) if isinstance(body, dict) else status

            if status == 429 or code == 429:
                # 第一次响应即结束：429 不再隐藏重试，直接交外层分类
                # （record_failure('rate_limit') → 来源冷却）
                retry_after = self._parse_retry_after(response.headers.get("retry-after"))
                raise OpenListRateLimitedError(retry_after=retry_after)

            if status in (500, 502, 503, 504):
                if attempt + 1 < self.max_attempts:
                    self._sleep(self._retry_delay(attempt))
                    continue
                raise OpenListNetworkError("OpenList 服务暂时不可用，请稍后重试")

            # 认证失败：进程内单次重登后重试一次。
            #
            # 重要：如果当前请求是 cooldown 到期后的唯一 probe，
            # can_request() 已经把 source_health 状态置为 probe。
            # 该物理请求已经真实到达 OpenList 并收到 401，说明：
            # 1. 远端可达；
            # 2. 当前失败属于 auth，而不是 risk_control / rate_limit。
            #
            # 因此必须先把 auth 结果上报给 SourceHealth：
            # probe + auth(irrelevant) → healthy，
            # 然后 login() 才能正常通过 health admission。
            #
            # 若在这期间另一个请求已经触发 risk_control 并进入 cooling_down，
            # record_failure("auth") 的 cooling 保护会保留 cooldown，
            # 后续 login() 仍会被 peek_request_allowed 拒绝，不会穿透风控。
            if (status == 401 or code == 401) and retry_on_auth:
                self._token = None
                self._report_failure("auth")
                self.login()
                return self._post(path, payload, retry_on_auth=False)

            if code == 401 or status == 401:
                raise OpenListAuthError()
            if code == 403 or status == 403:
                raise OpenListPermissionError()
            if code == 404 or status == 404:
                raise OpenListNotFoundError()
            if code != 200 or status != 200:
                # 归一化为安全消息；服务端原始 message 不进入任何输出
                raise OpenListError(f"OpenList 请求失败（{code}）", status_code=code)
            return status, body

        if isinstance(last_error, OpenListError):
            raise last_error
        raise OpenListNetworkError()

    @staticmethod
    def _decode_body(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse_retry_after(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            return max(0.0, float(value.strip()))
        except ValueError:
            return 0.0

    # -- 对外接口 ----------------------------------------------------

    def login(self) -> str:
        """登录并返回 Token（进程内缓存，不落盘）。

        公共请求入口：来源健康准入 + 连接级限速 + 按最终结果上报 source_health。
        冷却中（含 429/风控触发的冷却）不发任何物理请求，立即抛
        :class:`OpenListSourceCoolingDownError`；429 不重试。

        准入顺序与 :meth:`_post` 一致（peek 预检 → governor → 最终准入），
        governor 等待期间新建立的冷却在最终准入处拦截。
        """
        # 快速预检（只读、不消费探针）：明确未到期冷却 → 直接拒绝
        peek_allowed, _ = source_health.peek_request_allowed(self._conn_key)
        if not peek_allowed:
            raise OpenListSourceCoolingDownError()
        # 连接级限速
        self._governor.acquire(self._conn_key)
        # 真正最终准入（消费探针；governor 等待期间新冷却在此拦截）
        allowed, _ = source_health.can_request(self._conn_key)
        if not allowed:
            raise OpenListSourceCoolingDownError()
        try:
            token = self._login_request()
        except OpenListSourceCoolingDownError:
            # 第一保险：本地准入拒绝（冷却拦截）不是上游失败，直接 re-raise，
            # 不调用 _report_failure（record_failure 对 source_cooling_down
            # 已 NO-OP 兜底，双保险防止冷却期被反复刷新）。
            raise
        except OpenListError as exc:
            self._report_failure(exc.kind)
            raise
        except Exception:
            self._report_failure("unknown")
            raise
        self._report_success()
        return token

    def _login_request(self) -> str:
        """登录实际请求（不限速、不上报，供 login 包装）。"""
        with self._client() as client:
            try:
                response = client.post(
                    _LOGIN_PATH,
                    json={"username": self.username, "password": self.password},
                    headers={"accept": "application/json", "content-type": "application/json"},
                )
            except httpx.TimeoutException:
                raise OpenListTimeoutError() from None
            except httpx.HTTPError:
                raise OpenListNetworkError() from None
        if response.status_code in (301, 302, 303, 307, 308):
            raise OpenListRedirectError()
        if self._looks_like_risk_control(response):
            raise OpenListRiskControlError()
        body = self._decode_body(response)
        if response.status_code == 429 or int(body.get("code", 200)) == 429:
            raise OpenListRateLimitedError()
        if response.status_code != 200 or body.get("code") != 200:
            raise OpenListAuthError()
        data = body.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise OpenListAuthError("OpenList 登录响应缺少令牌")
        self._token = token
        return token

    def list_dir(
        self,
        remote_path: str,
        page: int = 1,
        per_page: int = _DEFAULT_PER_PAGE,
        *,
        refresh: bool = False,
    ) -> OpenListDirPage:
        """读取单个目录的一页条目（不递归）。

        公共请求入口：最终结果上报 source_health（成功回 healthy，
        失败按 exc.kind 记录；错误仍原样抛出，不影响调用方语义）。
        实际限速由内部 ``_post`` 在发请求前通过 governor 完成。

        ``refresh=False`` 使用 OpenList 上游缓存（普通浏览/后台更新）；
        ``refresh=True`` 仅显式强制刷新当前层时使用，不递归刷新后代。
        """
        try:
            page = self._list_dir_request(remote_path, page, per_page, refresh=refresh)
        except OpenListSourceCoolingDownError:
            # 第一保险：本地准入拒绝（冷却拦截）不是上游失败，直接 re-raise，
            # 不调用 _report_failure（record_failure 对 source_cooling_down
            # 已 NO-OP 兜底，双保险防止冷却期被反复刷新）。
            raise
        except OpenListError as exc:
            self._report_failure(exc.kind)
            raise
        except Exception:
            self._report_failure("unknown")
            raise
        self._report_success()
        return page

    def _list_dir_request(
        self,
        remote_path: str,
        page: int = 1,
        per_page: int = _DEFAULT_PER_PAGE,
        *,
        refresh: bool = False,
    ) -> OpenListDirPage:
        """单目录分页读取实际请求（不限速、不上报，供 list_dir 包装）。"""
        path = normalize_remote_path(remote_path)
        per_page = max(1, min(int(per_page), MAX_PER_PAGE))
        status, body = self._post(
            _LIST_PATH,
            {
                "path": path,
                "password": "",
                "refresh": bool(refresh),
                "page": max(1, int(page)),
                "per_page": per_page,
            },
        )
        data = body.get("data") or {}
        content = data.get("content") or []
        entries: list[OpenListEntry] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            name = validate_entry_name(str(item.get("name") or ""))
            entries.append(
                OpenListEntry(
                    name=name,
                    is_dir=bool(item.get("is_dir")),
                    size=_safe_int(item.get("size")),
                    modified=_safe_modified(item.get("modified")),
                    remote_path=join_remote_path(path, name),
                )
            )
        total = _safe_int(data.get("total")) or 0
        return OpenListDirPage(entries=entries, total=total)


def _safe_int(value: Any) -> int | None:
    """防御性转整数；目录或非法值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
