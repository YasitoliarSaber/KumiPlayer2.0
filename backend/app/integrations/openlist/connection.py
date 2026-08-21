"""OpenList 候选连接探测（Fresh Probe）。

职责：
- 对「候选配置解析后的有效凭据」执行真实连接探测：登录 + 读取远端根目录；
- 结果统一为 :class:`OpenListConnectionProbeResult`（``ok / code / phase / message``），
  前端依据固定 ``code`` 判断错误类型，不依赖中文消息猜测；
- 探测使用全新构造的 :class:`OpenListClient`，**绝不经过 production client pool**：
  pooled client 可能携带旧密码 / 旧 Token，用它证明候选正确会产生假成功 / 假失败。

安全边界：
- 密码、Token、Authorization、服务端原始响应一律不进入结果与日志；
- 探测成功后产生的临时 Token 随 client 销毁，不写入任何缓存或运行时池；
- 不塞入扫描、导入、路由逻辑。
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.catalog import source_health
from app.integrations.openlist.client import (
    OpenListClient,
    normalize_openlist_server_url,
    normalize_remote_path,
    validate_server_url,
)
from app.integrations.openlist.governor import governor_connection_key
from app.integrations.openlist.models import (
    OpenListAuthError,
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

_logger = logging.getLogger(__name__)
#: 探测时读取远端根的页参数（只验证可读性，不拉全量）
_PROBE_PAGE = 1
_PROBE_PER_PAGE = 10


@dataclass(frozen=True)
class OpenListConnectionProbeResult:
    """候选连接探测结果。

    ``phase`` 固定为 ``validation`` / ``credential`` / ``root``；
    ``code`` 固定支持：
    ``connected`` / ``not_configured`` / ``invalid_configuration`` /
    ``credential_store_unavailable`` / ``credential_rejected`` /
    ``root_permission_denied`` / ``root_not_found`` / ``rate_limited`` /
    ``risk_control`` / ``cooling_down`` / ``timeout`` /
    ``network_unavailable`` / ``server_unavailable`` / ``redirect_rejected`` /
    ``unexpected_error``。
    """

    ok: bool
    code: str
    phase: str
    message: str


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


def _classify(exc: OpenListError, *, phase: str) -> OpenListConnectionProbeResult:
    """把客户端归一化错误映射为固定 code + phase。

    - credential 阶段（登录）失败 → ``credential_*``；
    - root 阶段（目录读取）失败 → ``root_*`` / 网络类；
    - 登录成功后 token 失效且重登失败（OpenListAuthError）本质仍是凭据问题，
      映射为 ``credential_rejected``。
    """
    if isinstance(exc, OpenListSourceCoolingDownError):
        return OpenListConnectionProbeResult(False, "cooling_down", phase, str(exc))
    if isinstance(exc, OpenListRiskControlError):
        return OpenListConnectionProbeResult(False, "risk_control", phase, str(exc))
    if isinstance(exc, OpenListRateLimitedError):
        return OpenListConnectionProbeResult(
            False, "rate_limited", phase, "OpenList 请求过于频繁，请稍后再试"
        )
    if isinstance(exc, OpenListAuthError):
        return OpenListConnectionProbeResult(
            False,
            "credential_rejected",
            "credential",
            str(exc),
        )
    if isinstance(exc, OpenListPermissionError):
        return OpenListConnectionProbeResult(
            False,
            "root_permission_denied",
            "root",
            "已成功登录 OpenList，但当前远端根目录没有读取权限",
        )
    if isinstance(exc, OpenListNotFoundError):
        return OpenListConnectionProbeResult(
            False,
            "root_not_found",
            "root",
            "远端根目录不存在或已被移动，请检查远端根目录设置",
        )
    if isinstance(exc, OpenListTimeoutError):
        return OpenListConnectionProbeResult(
            False, "timeout", phase, "连接 OpenList 超时，请确认服务地址可达"
        )
    if isinstance(exc, OpenListNetworkError):
        message = str(exc)
        if "服务暂时不可用" in message:
            return OpenListConnectionProbeResult(
                False, "server_unavailable", phase, message
            )
        return OpenListConnectionProbeResult(
            False,
            "network_unavailable",
            phase,
            "暂时无法访问 OpenList 服务，已保存的登录信息不会被删除",
        )
    if isinstance(exc, OpenListRedirectError):
        return OpenListConnectionProbeResult(
            False, "redirect_rejected", phase, "OpenList 服务器返回重定向，已拒绝跟随"
        )
    if isinstance(exc, OpenListValidationError):
        return OpenListConnectionProbeResult(False, "invalid_configuration", "validation", str(exc))
    return OpenListConnectionProbeResult(
        False, "unexpected_error", phase, "连接 OpenList 时发生意外错误，请稍后重试"
    )


def probe_openlist_connection(
    *,
    server_url: str,
    remote_root: str,
    username: str,
    password: str,
    allow_insecure_http: bool,
) -> OpenListConnectionProbeResult:
    """对候选配置执行真实连接探测。

    探测契约（固定）：
    1. 候选 URL / 远端根合法性校验（``phase=validation``）；
    2. 全新 OpenListClient 登录（``phase=credential``）；
    3. 读取候选 ``remote_root`` 第一页（``phase=root``）。

    探测绝不使用 production client pool；产生的临时 Token 随 client 销毁。
    每次探测记录安全诊断日志（purpose / phase / code / 耗时 / 匿名连接键），
    不包含 username / password / token / Authorization / 响应体。
    """
    started = time.monotonic()
    result = _probe_impl(
        server_url=server_url,
        remote_root=remote_root,
        username=username,
        password=password,
        allow_insecure_http=allow_insecure_http,
    )
    duration_ms = (time.monotonic() - started) * 1000
    try:
        conn_key = governor_connection_key(
            normalize_openlist_server_url(server_url or ""), username or ""
        )
    except Exception:
        conn_key = "unknown"
    _logger.info(
        "openlist probe: purpose=%s phase=%s code=%s duration_ms=%.0f conn_key=%s",
        "test_connection",
        result.phase,
        result.code,
        duration_ms,
        conn_key,
    )
    return result


def _probe_impl(
    *,
    server_url: str,
    remote_root: str,
    username: str,
    password: str,
    allow_insecure_http: bool,
) -> OpenListConnectionProbeResult:
    """探测主体（无日志包装，便于单测直接调用）。"""
    url = (server_url or "").strip()
    ok, reason = validate_server_url(url)
    if not ok:
        message = reason
        if "公网 HTTP" in message:
            message = "该地址是公网 HTTP 明文传输，已拒绝连接；请改用 HTTPS"
        return OpenListConnectionProbeResult(False, "invalid_configuration", "validation", message)
    normalized_url = normalize_openlist_server_url(url)
    if (
        normalized_url.startswith("http://")
        and not _is_loopback_http(normalized_url)
        and not allow_insecure_http
    ):
        return OpenListConnectionProbeResult(
            False,
            "invalid_configuration",
            "validation",
            "本地/局域网 HTTP 将以明文传输密码，请在设置中确认风险后重试",
        )
    try:
        root = normalize_remote_path(remote_root)
    except OpenListError as exc:
        return OpenListConnectionProbeResult(False, "invalid_configuration", "validation", str(exc))

    if not username or not password:
        return OpenListConnectionProbeResult(
            False,
            "not_configured",
            "validation",
            "尚未配置 OpenList 连接，请先到设置页完成配置",
        )

    # 冷却预检（与客户端内部准入同一 SourceHealth 状态）：冷却中主动测试
    # 也零网络请求，直接返回 cooling_down，绝不尝试登录绕过访问保护。
    allowed, _health = source_health.peek_request_allowed(
        governor_connection_key(normalized_url, username)
    )
    if not allowed:
        return OpenListConnectionProbeResult(
            False,
            "cooling_down",
            "credential",
            "远端网盘疑似触发访问保护，KumiPlayer 已暂停该来源的自动请求，请稍后再试",
        )

    # Fresh Probe Client：直接构造，禁止 get_openlist_client（不进入 production pool）
    client = OpenListClient(normalized_url, username, password)
    try:
        client.login()
    except OpenListSourceCoolingDownError as exc:
        return OpenListConnectionProbeResult(False, "cooling_down", "credential", str(exc))
    except OpenListError as exc:
        return _classify(exc, phase="credential")

    try:
        client.list_dir(root, page=_PROBE_PAGE, per_page=_PROBE_PER_PAGE)
    except OpenListError as exc:
        return _classify(exc, phase="root")

    return OpenListConnectionProbeResult(
        True,
        "connected",
        "root",
        "连接成功，认证与目录读取权限正常",
    )
