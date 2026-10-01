"""不暴露原始异常、代理地址或凭据的在线连接失败文案。"""

_REASONS = {
    "provider_network_error": "无法连接在线资料，请检查网络或代理后重试。",
    "provider_timeout": "获取在线资料超时，请检查网络或代理后重试。",
    "provider_proxy_error": "代理连接失败，请检查代理设置后重试。",
    "provider_tls_error": "在线连接的证书校验失败，请检查网络或代理设置后重试。",
}


def transport_failure_reason(code: str, raw_reason: str = "") -> str:
    code = str(code or "").strip().casefold()
    if code in _REASONS:
        return _REASONS[code]
    # 历史快照只在保留了明确传输异常证据时归类；5xx 不推断成代理故障。
    if code not in {"", "source_unavailable"}:
        return ""
    raw = str(raw_reason or "").casefold()
    if any(value in raw for value in ("certificate_verify_failed", "证书校验失败")):
        return _REASONS["provider_tls_error"]
    if any(value in raw for value in ("proxyerror", "代理连接失败")):
        return _REASONS["provider_proxy_error"]
    if any(value in raw for value in ("timed out", "timeout", "请求超时")):
        return _REASONS["provider_timeout"]
    if any(value in raw for value in ("connecterror", "网络连接失败")):
        return _REASONS["provider_network_error"]
    return ""
