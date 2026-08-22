"""受信任远程图片 URL 校验（SSRF 防护）。

所有把外部图片 URL 抓取、代理或写入镜像/索引的路径必须经此模块校验：

- ``validate_remote_asset_url``：元数据图片 CDN（TMDB / AniList 官方 CDN）白名单；
- ``validate_bangumi_image_url``：Bangumi 官方图片域（``lain.bgm.tv``）白名单。

统一约束：仅标准 HTTPS、无显式非 443 端口、无 URL 内嵌凭据；拒绝重定向由
调用方负责（代理抓取时使用 ``follow_redirects=False``）。
"""

from urllib.parse import urlparse

#: 受信任元数据图片 CDN 前缀白名单（host → 允许的路径前缀）
TRUSTED_IMAGE_PATHS: dict[str, tuple[str, ...]] = {
    "image.tmdb.org": ("/t/p/",),
    "s4.anilist.co": ("/file/anilistcdn/",),
}

#: Bangumi 官方图片域（来自 Bangumi API 真实响应：lain.bgm.tv）
BANGUMI_IMAGE_HOSTS: tuple[str, ...] = ("lain.bgm.tv",)


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
    allowed_prefixes = TRUSTED_IMAGE_PATHS.get(parsed.hostname, ())
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
