# -*- coding: utf-8 -*-
"""镜像生成结果数据结构"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class MirrorItemResult:
    """单个 .strm 生成结果"""

    item_id: str = ""
    raw_file_id: str = ""
    source: str = ""
    status: str = ""       # generated / skipped / failed
    strm_path: str = ""    # 生成的 .strm 路径
    real_path: str = ""    # .strm 内容（真实视频路径）
    message: str = ""


@dataclass
class MirrorGenerateResult:
    """镜像生成整体结果"""

    plan_id: str = ""
    source: str = ""
    mirror_root: str = ""
    status: str = ""       # success / partial_failed / failed
    generated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    items: List[MirrorItemResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
