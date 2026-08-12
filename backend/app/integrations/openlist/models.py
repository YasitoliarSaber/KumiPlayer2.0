"""OpenList 目录条目模型与错误归一化。

只保留 KumiPlayer 需要的最小字段白名单：
name / is_dir / size / modified，以及客户端自行构造的派生远端路径。
直链、缩略图、哈希、存储内部 path 一律丢弃，禁止进入快照与镜像。
"""

from dataclasses import dataclass, field


@dataclass
class OpenListEntry:
    """OpenList ``/api/fs/list`` 返回条目的白名单投影。

    ``remote_path`` 不是服务端返回的 ``path`` 字段，而是客户端用
    「当前请求目录 + 校验后的 name」自行构造，防止信任存储侧路径。
    """

    name: str = ""
    is_dir: bool = False
    size: int | None = None  # 文件大小（字节）；目录通常为 None
    modified: float | None = None  # 修改时间（Unix 秒）
    remote_path: str = ""  # 派生远端路径，如 /夸克网盘/动画/冰菓/视频.mkv
    depth: int = 0  # 相对扫描根的层级深度


class OpenListError(RuntimeError):
    """OpenList 请求失败的归一化错误。

    只携带面向用户的安全消息；服务端原始错误、Token、密码绝不进入
    message，防止写入日志、任务结果或前端响应。
    """

    def __init__(self, message: str, status_code: int = 0, kind: str = "error"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


class OpenListAuthError(OpenListError):
    """认证失败（登录失败或 Token 无效且重登失败）。"""

    def __init__(self, message: str = "OpenList 认证失败，请检查用户名或密码"):
        super().__init__(message, status_code=401, kind="auth")


class OpenListPermissionError(OpenListError):
    """已认证但被拒绝访问目标目录。"""

    def __init__(self, message: str = "没有访问该 OpenList 目录的权限"):
        super().__init__(message, status_code=403, kind="permission")


class OpenListNotFoundError(OpenListError):
    """远端目录不存在或已被移动。"""

    def __init__(self, message: str = "OpenList 目录不存在或已被移动"):
        super().__init__(message, status_code=404, kind="not_found")


class OpenListRateLimitedError(OpenListError):
    """429 限流；``retry_after`` 为服务端建议等待秒数。"""

    def __init__(self, message: str = "OpenList 请求过于频繁", retry_after: float = 0.0):
        super().__init__(message, status_code=429, kind="rate_limit")
        self.retry_after = retry_after


class OpenListRiskControlError(OpenListError):
    """远端网盘疑似触发访问保护（风控拦截页，如 115 阿里云盾 405）。

    语义与 rate_limit / network / timeout / permission / not_found 严格区分：
    检测到后**不允许本请求再自动重试**，并应立即进入来源级冷却。

    安全边界：message 只携带面向用户的固定安全文本；服务端 HTML、
    trace 页面、Token、URL、Authorization 一律不进入 message、日志或前端。
    """

    def __init__(self, message: str = "远端网盘疑似触发访问保护，KumiPlayer 已暂停该来源的自动请求"):
        super().__init__(message, status_code=405, kind="risk_control")


class OpenListTimeoutError(OpenListError):
    """连接或读取超时。"""

    def __init__(self, message: str = "连接 OpenList 超时，请确认服务地址可达"):
        super().__init__(message, kind="timeout")


class OpenListNetworkError(OpenListError):
    """网络层失败（DNS、连接被拒等）。"""

    def __init__(self, message: str = "无法连接 OpenList 服务"):
        super().__init__(message, kind="network")


class OpenListRedirectError(OpenListError):
    """服务器返回重定向；跨主机重定向一律拒绝。"""

    def __init__(self, message: str = "OpenList 服务器返回重定向，已拒绝跟随"):
        super().__init__(message, kind="redirect")


class OpenListValidationError(OpenListError):
    """配置或远端条目不合法（URL 规则、危险目录名等）。"""

    def __init__(self, message: str):
        super().__init__(message, kind="validation")


class OpenListScanLimitExceeded(OpenListError):
    """递归扫描超过安全上限；调用方不得保存半成品快照。"""

    def __init__(self, message: str = "远端目录条目过多，已超过安全上限，请选择更精确的目录"):
        super().__init__(message, kind="scan_limit")


@dataclass
class OpenListDirPage:
    """单层目录的一页结果。"""

    entries: list[OpenListEntry] = field(default_factory=list)
    total: int = 0  # 服务端报告的当前目录条目总数
