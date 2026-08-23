"""V4 产物路径只使用持久身份，避免标题变化和同名作品互相覆盖。"""

from __future__ import annotations

import re


def work_directory_name(work_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", work_id or "").strip("_")
    if not safe_id:
        raise ValueError("生成媒体产物需要稳定 work_id")
    return f"work_{safe_id}"
