"""RWK-14/15：Provider SourceRoot 的 OpenList binding 安全契约。

bind 端点与 durable runtime（rescan 端点 / discovery job 执行）共用同一套
校验，保证：

1. root 是允许绑定的 Provider SourceRoot（pan115 / baidu）；
2. binding remote_locator 必须位于**当前** configured openlist_remote_root 内；
3. binding remote_locator 必须命中**启用**的 provider route；
4. route.provider_id 必须等于 Provider SourceRoot 的 provider（pan115/baidu），
   禁止把 pan115 root 绑到 baidu/quark/other route；
5. 连接身份：可信 resolver 的当前 conn hash 必须等于 binding 时持久化的
   conn hash（server_url + username）；config 的 remote_root / routes 变化
   不能绕过 scope/provider 校验。

非法时：0 OpenList 请求、0 binding mutation、0 generation bump、0 job
enqueue。校验函数抛 ValueError（调用方转 HTTPException 或 Job 失败）。
"""
from __future__ import annotations

from app.integrations.openlist.providers import PROVIDER_OTHER, provider_for_remote


#: 允许绑定的 Provider 来源类型（OpenList 增量通道只能挂在网盘 Provider 上）
BINDABLE_PROVIDERS = {"pan115", "baidu"}


def _root_provider(root) -> str:
    """由 SourceRootRecord 推导 Provider（source_id 前缀，与 discovery 分派一致）。"""
    sid = str(getattr(root, "source_id", "") or "")
    for provider in BINDABLE_PROVIDERS:
        if sid.startswith(provider + "-"):
            return provider
    return ""


def validate_binding_contract(
    *,
    root,
    remote_locator: str,
    routes: list,
    remote_root: str,
) -> dict:
    """校验 binding 契约（纯校验，0 mutation / 0 请求）。

    参数：
    - root：SourceRootRecord（provider 由 source_id 前缀推导）
    - remote_locator：归一化的绑定远端定位
    - routes：当前启用的 provider routes（OpenListRouteConfig 列表）
    - remote_root：当前 configured openlist_remote_root（归一化）

    返回 {"route_id": ..., "provider_id": ...}；非法抛 ValueError。
    """
    provider_id = _root_provider(root)
    if not provider_id:
        raise ValueError(
            "该来源根不是可绑定 Provider（需 pan115-/baidu- 来源），"
            "OpenList 增量仅支持 115/百度 Provider 来源根"
        )

    # 1. binding locator 必须位于当前 remote_root 内
    rroot = (remote_root or "/").rstrip("/") or "/"
    path = (remote_locator or "").rstrip("/") or "/"
    if rroot != "/" and path != rroot and not path.startswith(rroot + "/"):
        raise ValueError(
            f"绑定目录 {remote_locator} 不在当前 OpenList 远端总根 {rroot} 之内，"
            "已拒绝绑定（不扩大扫描范围）"
        )

    # 2/3. 必须命中启用的 provider route，且 provider 必须与 root 一致
    route_id, route_provider = provider_for_remote(routes, path)
    if not route_id or route_provider == PROVIDER_OTHER:
        raise ValueError(
            f"绑定目录 {remote_locator} 尚未归类到内容提供商路由，"
            "请先在设置页配置来源目录路由"
        )
    if route_provider != provider_id:
        raise ValueError(
            f"Provider 身份不一致：来源根是 {provider_id}，"
            f"但该 OpenList 目录路由归属 {route_provider}，已拒绝绑定"
        )
    return {"route_id": route_id, "provider_id": route_provider}


def validate_runtime_binding(
    *,
    root,
    bound_conn_hash: str,
    current_conn_hash: str,
    routes: list,
    remote_root: str,
) -> dict:
    """运行时（rescan / job 执行）完整复核 binding 契约。

    在 bump_generation / enqueue / 任何 OpenList 请求之前调用。
    比 bind 时多校验连接身份（conn hash）；配置（remote_root/routes）变化
    会在这里被拦截——server+username 不变但 scope/route 变了也拒绝。
    """
    if not bound_conn_hash:
        raise ValueError("该来源根尚未绑定 OpenList 增量通道，请先绑定")
    if current_conn_hash != bound_conn_hash:
        raise ValueError(
            "OpenList 连接已变更（服务器或账号与绑定不一致），"
            "已拒绝扫描，请重新绑定"
        )
    locator = str(getattr(root, "openlist_remote_locator", "") or "")
    if not locator:
        raise ValueError("该来源根缺少绑定的 OpenList 远端定位，请重新绑定")
    return validate_binding_contract(
        root=root,
        remote_locator=locator,
        routes=routes,
        remote_root=remote_root,
    )
