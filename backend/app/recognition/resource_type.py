# -*- coding: utf-8 -*-
"""统一资源类型识别

只判断文件的资源类型（video/subtitle/nfo/image/font/archive/audio/text/other），
不判断媒体结构（episode/season/sp/op_ed 等）。
"""

# 已知文件扩展名集合（全部小写）
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".rmvb", ".mov"}
SUBTITLE_EXTS = {".ass", ".srt", ".ssa", ".vtt"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
NFO_EXTS = {".nfo"}
FONT_EXTS = {".ttf", ".ttc", ".otf"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".exe"}
AUDIO_EXTS = {".mp3", ".flac", ".mka", ".wav", ".aac", ".ogg", ".wma"}
TEXT_EXTS = {".txt", ".log", ".cue", ".md", ".ini", ".cfg", ".conf"}

# 扩展名 → 资源类型映射
_EXT_TYPE_MAP: dict[str, str] = {}
for _ext in VIDEO_EXTS:
    _EXT_TYPE_MAP[_ext] = "video"
for _ext in SUBTITLE_EXTS:
    _EXT_TYPE_MAP[_ext] = "subtitle"
for _ext in IMAGE_EXTS:
    _EXT_TYPE_MAP[_ext] = "image"
for _ext in NFO_EXTS:
    _EXT_TYPE_MAP[_ext] = "nfo"
for _ext in FONT_EXTS:
    _EXT_TYPE_MAP[_ext] = "font"
for _ext in ARCHIVE_EXTS:
    _EXT_TYPE_MAP[_ext] = "archive"
for _ext in AUDIO_EXTS:
    _EXT_TYPE_MAP[_ext] = "audio"
for _ext in TEXT_EXTS:
    _EXT_TYPE_MAP[_ext] = "text"


def normalize_ext(ext: str) -> str:
    """归一化扩展名：小写，确保以 . 开头

    参数:
        ext: 扩展名，如 ".MKV"、"mkv"、".mp4"

    返回:
        归一化后的扩展名，如 ".mkv"、".mp4"
    """
    if not ext:
        return ""
    ext = ext.strip().lower()
    if not ext.startswith("."):
        ext = "." + ext
    return ext


def classify_resource_type(
    name: str = "",
    ext: str = "",
    resource_hint: str = "",
) -> str:
    """识别文件的资源类型

    优先级：
    1. 优先使用 ext 识别（归一化后查表）
    2. ext 为空时，尝试从 name 提取扩展名
    3. resource_hint 只作为弱提示，不能覆盖 ext 的判断

    参数:
        name: 文件名，如 "视频.mkv"
        ext: 扩展名，如 ".mkv"
        resource_hint: 来源适配器给出的弱提示

    返回:
        资源类型字符串：video/subtitle/nfo/image/font/archive/audio/text/other
    """
    # 1. 优先用 ext
    norm_ext = normalize_ext(ext) if ext else ""

    # 2. ext 为空时从 name 提取
    if not norm_ext and name:
        dot_idx = name.rfind(".")
        if dot_idx > 0:
            norm_ext = name[dot_idx:].lower()

    # 3. 查表
    if norm_ext and norm_ext in _EXT_TYPE_MAP:
        return _EXT_TYPE_MAP[norm_ext]

    # 4. resource_hint 作为弱提示（只在 ext 无法识别时参考）
    if resource_hint:
        hint_lower = resource_hint.strip().lower()
        valid_types = {"video", "subtitle", "nfo", "image", "font", "archive", "audio", "text"}
        if hint_lower in valid_types:
            return hint_lower

    return "other"


def decide_import_action(resource_type: str, source: str = "") -> str:
    """根据资源类型和来源决定导入动作

    规则：
    - video → generate_strm
    - subtitle → attach_only
    - 其他（nfo/image/font/archive/audio/text/other）→ ignore

    参数:
        resource_type: 资源类型
        source: 来源标识（当前版本不影响动作，预留未来本地来源差异化）

    返回:
        动作字符串：generate_strm / attach_only / ignore
    """
    if resource_type == "video":
        return "generate_strm"
    if resource_type == "subtitle":
        return "attach_only"
    return "ignore"
