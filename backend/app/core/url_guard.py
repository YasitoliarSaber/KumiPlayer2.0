"""受信任远程图片 URL 校验（SSRF 防护）。

所有把外部图片 URL 抓取、代理或写入镜像/索引的路径必须经此模块校验：

- ``validate_remote_asset_url``：元数据图片 CDN（TMDB / AniList 官方 CDN）白名单；
- ``validate_bangumi_image_url``：Bangumi 官方图片域（``lain.bgm.tv``）白名单；
- ``assert_public_dns_resolution``：解析目标主机并拒绝私网/环回/链路本地地址。

统一约束：仅标准 HTTPS、无显式非 443 端口、无 URL 内嵌凭据；拒绝重定向由
调用方负责（代理抓取时使用 ``follow_redirects=False``）。
"""

import ipaddress
import socket
from urllib.parse import urlparse

#: 受信任元数据图片 CDN 前缀白名单（host → 允许的路径前缀）
TRUSTED_IMAGE_PATHS: dict[str, tuple[str, ...]] = {
    "image.tmdb.org": ("/t/p/",),
    "s4.anilist.co": ("/file/anilistcdn/",),
}

#: Bangumi 官方图片域（来自 Bangumi API 真实响应：lain.bgm.tv）
BANGUMI_IMAGE_HOSTS: tuple[str, ...] = ("lain.bgm.tv",)
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def assert_public_dns_resolution(hostname: str, *, allow_proxy_fake_ip: bool = False) -> None:
    """解析目标主机并拒绝解析到非公网地址的结果（SSRF 防护）。

    域名白名单仍可能被 DNS 劫持或 rebinding 指向内网/云元数据地址；请求前
    解析全部地址并逐个校验，默认任一非公网地址都拒绝本次请求。只有调用方
    已完成严格 URL 白名单校验时，才可显式接受本机代理使用的 Fake-IP 地址段。
    """

    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError(f"无法解析远程图片主机: {hostname}") from error
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("远程图片主机没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if allow_proxy_fake_ip and ip in _PROXY_FAKE_IP_NETWORK:
            # Fake-IP 由本机代理接管，HTTP 请求仍固定使用已验证的 HTTPS 主机名。
            # 仅供调用方在严格 URL 白名单之后显式启用，默认远程资源仍拒绝该地址段。
            continue
        if (
            not ip.is_global
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("远程图片主机解析到非公网地址，已拒绝请求")


def _validate_standard_https(parsed) -> None:
    """公共前置校验：标准 HTTPS、无凭据、无非法端口。"""
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("远程图片端口无效") from error
    if (
        parsed.scheme != "https"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ValueError("远程图片必须使用标准 HTTPS")


def validate_remote_asset_url(url: str):
    """校验元数据图片 URL（仅 TMDB / AniList 官方 CDN）。

    :raises ValueError: URL 不满足标准 HTTPS 或不在受信任 CDN 范围内。
    :returns: ``urlparse`` 结果，供调用方继续使用。
    """
    parsed = urlparse(url)
    _validate_standard_https(parsed)
    trusted = False
    allowed_prefixes = TRUSTED_IMAGE_PATHS.get(parsed.hostname or "", ())
    for prefix in allowed_prefixes:
        if parsed.path.startswith(prefix):
            trusted = True
            break
    if not trusted:
        raise ValueError("远程图片地址不在受信任 CDN 范围内")
    return parsed


def validate_bangumi_image_url(url: str):
    """校验 Bangumi 图片 URL（仅 Bangumi 官方图片域）。

    :raises ValueError: URL 不满足标准 HTTPS 或不在 Bangumi 官方图片域内。
    :returns: ``urlparse`` 结果，供调用方继续使用。
    """
    parsed = urlparse(url)
    _validate_standard_https(parsed)
    if parsed.hostname not in BANGUMI_IMAGE_HOSTS:
        raise ValueError("Bangumi 图片地址不在受信任域名范围内")
    return parsed
