"""115 网盘目录树来源适配器

解析 115 导出的目录树 txt，输出 RawSnapshot / RawFile。
只负责目录树解析和文件清单生成，不判断作品、季、集等媒体结构。
"""

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from app.catalog.models import SourceNodeInput
from app.integrations.openlist.providers import compat_ingest, compat_provider
from app.raw.models import RawFile, RawSnapshot
from app.sources.base import SourceAdapter

# 已知文件扩展名（小写）
_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".rmvb", ".mov"}
_SUBTITLE_EXTS = {".ass", ".srt", ".ssa", ".vtt"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
_NFO_EXTS = {".nfo"}
_FONT_EXTS = {".ttf", ".ttc", ".otf"}
_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".exe"}
_AUDIO_EXTS = {".mp3"}
_TEXT_EXTS = {".txt"}

_ALL_KNOWN_EXTS = (
    _VIDEO_EXTS | _SUBTITLE_EXTS | _IMAGE_EXTS | _NFO_EXTS
    | _FONT_EXTS | _ARCHIVE_EXTS | _AUDIO_EXTS | _TEXT_EXTS
)

# 系统文件名（小写比较）
_SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

# 115 树形特征字符
_TREE_MARKERS = ["|——", "|——", "|-", "| |-", "| | |-", "├", "└", "│"]

# 正则：首行 |——根目录
_RE_FIRST_LINE = re.compile(r"^\|——(?P<name>.+)$")
# 正则：普通行 若干 "| " + "|-" + 节点名
_RE_NORMAL_LINE = re.compile(r"^(?P<prefix>(?:\| )*)\|-(?P<name>.+)$")


def _detect_encoding(data: bytes) -> str:
    """检测 115 txt 文件编码

    依次尝试 UTF-16 BOM、UTF-8-SIG、UTF-8、GBK、UTF-16。
    用是否包含树形特征字符判断有效性。
    多种编码都可用时，选解码后文本最长且包含树形特征的结果。
    """
    candidates = []

    # 1. UTF-16 BOM
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = data.decode("utf-16")
            if _has_tree_markers(text):
                candidates.append(("utf-16", text))
        except (UnicodeDecodeError, LookupError):
            pass

    # 2. UTF-8-SIG
    try:
        text = data.decode("utf-8-sig")
        if _has_tree_markers(text):
            candidates.append(("utf-8-sig", text))
    except (UnicodeDecodeError, LookupError):
        pass

    # 3. UTF-8
    try:
        text = data.decode("utf-8")
        if _has_tree_markers(text):
            candidates.append(("utf-8", text))
    except (UnicodeDecodeError, LookupError):
        pass

    # 4. GBK
    try:
        text = data.decode("gbk")
        if _has_tree_markers(text):
            candidates.append(("gbk", text))
    except (UnicodeDecodeError, LookupError):
        pass

    # 5. UTF-16 无 BOM
    try:
        text = data.decode("utf-16")
        if _has_tree_markers(text):
            candidates.append(("utf-16", text))
    except (UnicodeDecodeError, LookupError):
        pass

    if not candidates:
        raise ValueError(
            "无法检测 115 目录树文件编码，"
            "请确认文件是 115 导出的目录树 txt"
        )

    # 选解码后文本最长的
    best = max(candidates, key=lambda c: len(c[1]))
    return best[0]


def _has_tree_markers(text: str) -> bool:
    """检查文本是否包含 115 树形特征字符"""
    for marker in _TREE_MARKERS:
        if marker in text:
            return True
    return False


def _get_resource_hint(ext: str) -> str:
    """根据扩展名返回资源类型提示"""
    ext_lower = ext.lower()
    if ext_lower in _VIDEO_EXTS:
        return "video"
    if ext_lower in _SUBTITLE_EXTS:
        return "subtitle"
    if ext_lower in _IMAGE_EXTS:
        return "image"
    if ext_lower in _NFO_EXTS:
        return "nfo"
    if ext_lower in _FONT_EXTS:
        return "font"
    if ext_lower in _ARCHIVE_EXTS:
        return "archive"
    if ext_lower in _AUDIO_EXTS:
        return "audio"
    if ext_lower in _TEXT_EXTS:
        return "text"
    return "other"


def _is_known_extension(name: str) -> bool:
    """判断文件名是否含有已知扩展名"""
    # 从最后一个点号取扩展名
    dot_idx = name.rfind(".")
    if dot_idx <= 0:
        return False
    ext = name[dot_idx:].lower()
    return ext in _ALL_KNOWN_EXTS


def _split_name_ext(name: str) -> tuple[str, str]:
    """分离文件名和扩展名"""
    dot_idx = name.rfind(".")
    if dot_idx <= 0:
        return name, ""
    ext = name[dot_idx:]
    stem = name[:dot_idx]
    return stem, ext


def _is_system_file(name: str) -> bool:
    """判断是否为系统文件"""
    return name.lower() in _SYSTEM_FILES


def _make_stable_id(source: str, relative_path: str) -> str:
    """生成稳定的 RawFile ID

    同一个 relative_path 重复解析时 ID 应稳定。
    """
    content = f"{source}:{relative_path}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _make_snapshot_id(source: str, input_path: str, content_hash: str) -> str:
    """生成 snapshot ID"""
    content = f"{source}:{input_path}:{content_hash}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _parse_line(line: str) -> tuple[int, str] | None:
    """解析一行 115 目录树

    返回 (depth, name) 或 None（空行 / 无法识别）。
    """
    stripped = line.rstrip("\r\n")
    if not stripped:
        return None

    # 首行：|——根目录
    m = _RE_FIRST_LINE.match(stripped)
    if m:
        return (1, m.group("name").strip())

    # 普通行：若干 "| " + "|-" + 节点名
    m = _RE_NORMAL_LINE.match(stripped)
    if m:
        prefix = m.group("prefix")
        name = m.group("name").strip()
        # 深度 = prefix 中 "| " 的数量 + 1
        depth = prefix.count("| ") + 1
        return (depth, name)

    return None


class Pan115Adapter(SourceAdapter):
    """115 网盘目录树适配器"""

    @property
    def source_id(self) -> str:
        return "pan115"

    @property
    def mirror_namespace(self) -> str:
        return "115"

    def parse(self, input_path: str, source_root: str) -> RawSnapshot:
        """解析 115 目录树 txt，输出 RawSnapshot

        参数:
            input_path: 115 目录树 txt 文件路径
            source_root: 115 挂载根目录，如 H:\115open

        返回:
            RawSnapshot 包含所有文件节点的 RawFile
        """
        # 读取文件
        data = Path(input_path).read_bytes()
        encoding = _detect_encoding(data)
        text = data.decode(encoding)
        lines = text.splitlines()

        # 内容 hash 用于 snapshot_id 稳定性
        content_hash = hashlib.md5(data).hexdigest()[:12]
        snapshot_id = _make_snapshot_id("pan115", input_path, content_hash)
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()

        # 路径栈还原
        stack: list[str] = []  # stack[depth-1] = name
        files: list[RawFile] = []

        for line_no, line in enumerate(lines, start=1):
            result = _parse_line(line)
            if result is None:
                continue

            depth, name = result

            # 截断更深层 stack
            if len(stack) >= depth:
                stack = stack[:depth]
            # 补齐中间层（理论上不应该出现，但防御性处理）
            while len(stack) < depth - 1:
                stack.append("")

            # 写入当前层
            if len(stack) < depth:
                stack.append(name)
            else:
                stack[depth - 1] = name

            # 跳过根目录（depth=1）
            if depth <= 1:
                continue

            # 判断文件 / 目录
            is_file = _is_known_extension(name)

            # 跳过系统文件
            if is_file and _is_system_file(name):
                continue

            # 只输出文件节点
            if not is_file:
                continue

            # 构建 source_path_parts：从 depth 2 开始（跳过根目录）
            source_path_parts = stack[1:depth]

            # relative_path
            relative_path = "/".join(source_path_parts)

            # virtual_root = 分类层目录（depth 2）
            virtual_root = stack[1] if len(stack) > 1 else ""

            # parent_path
            if len(source_path_parts) > 1:
                parent_path = "/".join(source_path_parts[:-1])
            else:
                parent_path = ""

            # 文件名、扩展名
            stem, ext = _split_name_ext(name)
            resource_hint = _get_resource_hint(ext)

            # real_path
            real_path = self.build_real_path(relative_path, source_root)

            # 稳定 ID
            file_id = _make_stable_id("pan115", relative_path)

            raw_file = RawFile(
                id=file_id,
                snapshot_id=snapshot_id,
                source="pan115",
                source_root=source_root,
                virtual_root=virtual_root,
                source_path_parts=source_path_parts,
                relative_path=relative_path,
                real_path=real_path,
                name=name,
                stem=stem,
                ext=ext,
                depth=depth,
                parent_path=parent_path,
                is_file=True,
                resource_hint=resource_hint,
                size=None,
                mtime=None,
            )
            files.append(raw_file)

        # 统计
        file_count = len(files)
        video_count = sum(1 for f in files if f.resource_hint == "video")

        return RawSnapshot(
            snapshot_id=snapshot_id,
            source="pan115",
            provider_id=compat_provider("pan115"),
            ingest_method=compat_ingest("pan115"),
            source_root=source_root,
            created_at=now,
            input_file=input_path,
            file_count=file_count,
            video_count=video_count,
            files=files,
        )

    def snapshot_entries(self, input_path: str, source_root: str) -> list[SourceNodeInput]:
        """从 115 目录树 TXT 一次性生成 SourceNodeInput（复用既有 parse）。"""
        snapshot = self.parse(input_path, source_root)
        return [
            SourceNodeInput(
                name=item.name,
                remote_path=item.relative_path,
                parent_path=item.parent_path,
                kind="dir" if not item.is_file else "file",
                size=item.size,
                mtime=item.mtime,
                logical_locator=item.real_path,
            )
            for item in snapshot.files
        ]

    def build_real_path(self, relative_path: str, source_root: str) -> str:
        """拼接真实播放路径

        采用方案 A：
        source_root = H:\115open
        relative_path = 动画/冰菓.2012/视频.mkv
        real_path = H:\115open\动画\冰菓.2012\视频.mkv
        """
        # 用 PurePosixPath 拆分 relative_path（统一用 / 分隔）
        parts = list(PurePosixPath(relative_path).parts)
        # 拼接为当前系统路径
        result = source_root
        for part in parts:
            result = os.path.join(result, part)
        return result
