"""OpenList 内容提供商与来源路由模型。

三维正交模型：
- provider（内容提供商）：pan115 / baidu / quark / other（local 仅本地来源，不作为路由候选）；
- connector / ingest_method（连接方式）：openlist_api / directory_tree / local_scan；
- route（来源路由）：OpenList 连接下的并列/嵌套子树，映射远端前缀到 provider。

本地路径统一由「本地总挂载根 + 远端路径相对远端总根的各段」推导，
因此不为每个提供商重复保存挂载路径。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# ------------------------------------------------------------
# 提供商与导入方式常量
# ------------------------------------------------------------

PROVIDER_PAN115 = "pan115"
PROVIDER_BAIDU = "baidu"
PROVIDER_QUARK = "quark"
PROVIDER_OTHER = "other"
PROVIDER_LOCAL = "local"  # 仅本地来源，不作为 OpenList 路由候选

#: 可配置为 OpenList 路由的提供商
ROUTABLE_PROVIDERS = (PROVIDER_PAN115, PROVIDER_BAIDU, PROVIDER_QUARK, PROVIDER_OTHER)

PROVIDER_LABELS = {
    PROVIDER_PAN115: "115 网盘",
    PROVIDER_BAIDU: "百度网盘",
    PROVIDER_QUARK: "夸克网盘",
    PROVIDER_OTHER: "其他远程来源",
    PROVIDER_LOCAL: "本地媒体",
}

INGEST_OPENLIST_API = "openlist_api"
INGEST_DIRECTORY_TREE = "directory_tree"
INGEST_LOCAL_SCAN = "local_scan"

#: 兼容回填：旧 source -> (provider_id, ingest_method)
_SOURCE_COMPAT = {
    "pan115": (PROVIDER_PAN115, INGEST_DIRECTORY_TREE),
    "baidu": (PROVIDER_BAIDU, INGEST_DIRECTORY_TREE),
    "local": (PROVIDER_LOCAL, INGEST_LOCAL_SCAN),
    # 项目既有「OpenList 夸克试点」旧记录：兼容回填 quark，不改动媒体身份
    "openlist": (PROVIDER_QUARK, INGEST_OPENLIST_API),
}

#: 按目录名给出的推荐值（仅建议，不自动保存为事实）
_NAME_HINTS = {
    "115": PROVIDER_PAN115,
    "115网盘": PROVIDER_PAN115,
    "百度": PROVIDER_BAIDU,
    "百度网盘": PROVIDER_BAIDU,
    "夸克": PROVIDER_QUARK,
    "夸克网盘": PROVIDER_QUARK,
}


def provider_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id or "", provider_id or "未知来源")


def compat_provider(source: str) -> str:
    return _SOURCE_COMPAT.get(source or "", (PROVIDER_OTHER, INGEST_DIRECTORY_TREE))[0]


def compat_ingest(source: str) -> str:
    return _SOURCE_COMPAT.get(source or "", (PROVIDER_OTHER, INGEST_DIRECTORY_TREE))[1]


def hint_provider_for_name(name: str) -> str:
    """按顶层目录名给出提供商建议值；未命中返回 other（仅建议，不自动保存）。"""
    return _NAME_HINTS.get((name or "").strip(), PROVIDER_OTHER)


# ------------------------------------------------------------
# 路由模型
# ------------------------------------------------------------

@dataclass
class OpenListRouteConfig:
    """OpenList 提供商路由：把远端前缀归属到一个内容提供商。

    - 不保存本地挂载根：本地路径由连接级 mount_root 统一推导；
    - 不保存 OpenList 地址/凭据：连接级单实例字段；
    - 路径名只生成建议，用户确认的 label / provider 才是权威事实。
    """

    route_id: str = ""
    label: str = ""
    remote_prefix: str = ""
    provider_id: str = PROVIDER_OTHER
    enabled: bool = True


def new_route_id() -> str:
    return uuid.uuid4().hex


def normalize_route_prefix(prefix: str) -> str:
    """规范化路由远端前缀；拒绝根路径作路由。"""
    from app.integrations.openlist.client import normalize_remote_path

    value = normalize_remote_path(prefix or "")
    if value == "/":
        raise ValueError("路由远端前缀不能是整个连接根，请选择其下的具体目录")
    return value


def is_ancestor_or_self(prefix: str, path: str) -> bool:
    """prefix 是否为 path 的祖先（或相等）；以路径段边界判断。"""
    from app.integrations.openlist.client import normalize_remote_path

    prefix = normalize_remote_path(prefix)
    path = normalize_remote_path(path)
    if prefix == "/":
        return True
    if path == prefix:
        return True
    return path.startswith(prefix + "/")


def route_prefixes_overlap(left: str, right: str) -> bool:
    """两个路由前缀是否重叠（互为祖先）。"""
    return is_ancestor_or_self(left, right) or is_ancestor_or_self(right, left)


def match_route(routes: list[OpenListRouteConfig], remote_path: str) -> OpenListRouteConfig | None:
    """最长前缀匹配启用的路由；未启用或未命中返回 None。"""
    from app.integrations.openlist.client import normalize_remote_path

    normalized = normalize_remote_path(remote_path)
    best: OpenListRouteConfig | None = None
    best_len = -1
    for route in routes:
        if not route.enabled:
            continue
        prefix = normalize_remote_path(route.remote_prefix)
        if prefix == "/":
            continue
        if not is_ancestor_or_self(prefix, normalized):
            continue
        if len(prefix) > best_len:
            best = route
            best_len = len(prefix)
    return best


def provider_for_remote(
    routes: list[OpenListRouteConfig],
    remote_path: str,
) -> tuple[str, str]:
    """返回 (route_id, provider_id)；未命中启用路由时 provider 归 other、route_id 为空。

    调用方（导入校验）负责在需要明确 provider 时拒绝 other 之外的空路由场景；
    浏览阶段允许未归类目录继续浏览。
    """
    route = match_route(routes, remote_path)
    if route is None:
        return "", PROVIDER_OTHER
    return route.route_id, route.provider_id


def derive_local_path(mount_root: str, remote_root: str, remote_path: str) -> str:
    """由连接级挂载根推导本地路径：mount_root + 远端相对 remote_root 的各段。"""
    from app.integrations.openlist.client import normalize_remote_path

    rroot = normalize_remote_path(remote_root or "/")
    rpath = normalize_remote_path(remote_path)
    if rroot != "/":
        if not is_ancestor_or_self(rroot, rpath):
            raise ValueError("选择的远端目录不在映射根路径之下，无法映射到本地挂载")
        remainder = rpath[len(rroot):].lstrip("/")
    else:
        remainder = rpath.lstrip("/")

    local = Path(mount_root).expanduser()
    for part in PurePosixPath(remainder).parts:
        local = local / part
    return str(local)
