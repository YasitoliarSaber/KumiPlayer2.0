"""播放定位校验：scan / confirm / mirror / playback 共用同一规则。

规则只读取文件元数据，不打开视频内容、不计算哈希、不递归枚举目录。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

_REMOTE_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def validate_playback_locator(locator: str) -> tuple[bool, str]:
    """校验单个可播放定位符。

    返回 (ok, reason)。本地绝对路径必须真实存在；远程地址只做语法校验。
    失败原因使用面向用户的可操作中文，不泄露内部路径细节。
    """
    value = (locator or "").strip()
    if not value:
        return False, "媒体播放定位为空，请重新扫描来源"
    if _REMOTE_SCHEME.match(value):
        parsed = urlsplit(value)
        if parsed.scheme and parsed.netloc:
            return True, ""
        return False, "媒体播放定位不是有效的远程地址"
    path = Path(value)
    if not path.is_absolute():
        return False, "媒体播放定位不是本地绝对路径，请检查来源映射"
    if not path.is_file():
        return False, "媒体挂载路径不可访问，请检查来源映射或挂载状态"
    return True, ""
