"""百度网盘来源适配器

解析百度导出的目录树文件（tree 命令格式）。
百度目录树规则不能复用 115 规则。

百度目录树格式（标准 tree 命令输出）：

├── 新番
│   ├── [Sakurato] Steel Ball Run.mkv
├── 刮削好的动画
│   ├── 石纪元 (2019) {tmdbid-86031}
│   │   ├── Season 1
│   │   │   ├── 石纪元 - S01E01.mkv

特点：
- 使用 ├── 表示同级节点
- 使用 │   表示层级缩进（4个字符：│ + 3个空格）
- 使用 └── 表示最后一个节点
"""

import hashlib
import logging
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
_TEXT_EXTS = {".txt", ".xml"}
_OTHER_FILE_EXTS = {".torrent"}

_ALL_KNOWN_EXTS = (
    _VIDEO_EXTS | _SUBTITLE_EXTS | _IMAGE_EXTS | _NFO_EXTS
    | _FONT_EXTS | _ARCHIVE_EXTS | _AUDIO_EXTS | _TEXT_EXTS
    | _OTHER_FILE_EXTS
)

_SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

# 百度 tree 格式正则
# 匹配：若干 "│   " 或 "    " 前缀 + 常见分支符 + 节点名。
# 百度/Windows/类 Unix 导出的树形文件会混用 "├── " 与 "├─"。
_RE_TREE_LINE = re.compile(
    r"^(?P<prefix>(?:(?:│ {2,3})|(?: {3,4}))*)"
    r"(?:├──\s?|└──\s?|├─\s?|└─\s?)"
    r"(?P<name>.+)$"
)

_RE_TREE_PREFIX_TOKEN = re.compile(r"(?:│ {2,3})|(?: {3,4})")


def _get_resource_hint(ext: str) -> str:
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


def _is_system_file(name: str) -> bool:
    return name.lower() in _SYSTEM_FILES


def _split_name_ext(name: str) -> tuple[str, str]:
    dot_idx = name.rfind(".")
    if dot_idx <= 0:
        return name, ""
    return name[:dot_idx], name[dot_idx:]


def _make_stable_id(source: str, relative_path: str) -> str:
    content = f"{source}:{relative_path}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _make_snapshot_id(source: str, input_path: str, content_hash: str) -> str:
    content = f"{source}:{input_path}:{content_hash}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _parse_line(line: str) -> tuple[int, str] | None:
    """解析一行百度目录树

    返回 (depth, name) 或 None。
    depth 从 0 开始（顶层节点）。
    """
    stripped = line.rstrip("\r\n")
    if not stripped:
        return None

    m = _RE_TREE_LINE.match(stripped)
    if m:
        prefix = m.group("prefix")
        name = m.group("name").strip()
        # 每个前缀 token 代表一层深度。百度/Windows/tree 输出存在
        # 3 字符与 4 字符两种缩进宽度。
        depth = len(_RE_TREE_PREFIX_TOKEN.findall(prefix))
        return (depth, name)

    return None


class BaiduAdapter(SourceAdapter):
    """百度网盘来源适配器"""

    @property
    def source_id(self) -> str:
        return "baidu"

    @property
    def mirror_namespace(self) -> str:
        return "baidu"

    def parse(self, input_path: str, source_root: str) -> RawSnapshot:
        """解析百度目录树文件

        参数:
            input_path: 百度目录树文件路径
            source_root: 百度挂载根目录

        返回:
            RawSnapshot
        """
        data = Path(input_path).read_bytes()
        # 尝试 UTF-8，回退 GBK
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # GBK 回退的替换符会以乱码文件名进入识别/镜像，必须留下可观测线索
            text = data.decode("gbk", errors="replace")
            logging.getLogger(__name__).warning(
                "百度目录树非 UTF-8 编码，GBK 回退产生替换符（文件数约 %d 行）",
                len(text.splitlines()),
            )

        lines = text.splitlines()
        content_hash = hashlib.md5(data).hexdigest()[:12]
        snapshot_id = _make_snapshot_id("baidu", input_path, content_hash)
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()

        # 路径栈：stack[depth] = name
        stack: list[str] = []
        files: list[RawFile] = []

        for line in lines:
            result = _parse_line(line)
            if result is None:
                continue

            depth, name = result

            # 截断更深层 stack
            if len(stack) > depth:
                stack = stack[:depth]
            # 补齐中间层
            while len(stack) < depth:
                stack.append("")

            # 写入当前层
            if len(stack) == depth:
                stack.append(name)
            else:
                stack[depth] = name

            # 跳过系统文件
            if _is_system_file(name):
                continue

            # 判断是否为文件（有已知扩展名）
            stem, ext = _split_name_ext(name)
            is_file = ext.lower() in _ALL_KNOWN_EXTS

            if not is_file:
                continue

            # 构建 relative_path
            parts = stack[:depth + 1]
            relative_path = "/".join(parts)

            # virtual_root = 第一层目录
            virtual_root = parts[0] if len(parts) > 1 else ""

            # parent_path
            parent_path = "/".join(parts[:-1]) if len(parts) > 1 else ""

            resource_hint = _get_resource_hint(ext)
            real_path = self.build_real_path(relative_path, source_root)
            file_id = _make_stable_id("baidu", relative_path)

            raw_file = RawFile(
                id=file_id,
                snapshot_id=snapshot_id,
                source="baidu",
                source_root=source_root,
                virtual_root=virtual_root,
                source_path_parts=parts,
                relative_path=relative_path,
                real_path=real_path,
                name=name,
                stem=stem,
                ext=ext,
                depth=len(parts),
                parent_path=parent_path,
                is_file=True,
                resource_hint=resource_hint,
                size=None,
                mtime=None,
            )
            files.append(raw_file)

        file_count = len(files)
        video_count = sum(1 for f in files if f.resource_hint == "video")

        return RawSnapshot(
            snapshot_id=snapshot_id,
            source="baidu",
            provider_id=compat_provider("baidu"),
            ingest_method=compat_ingest("baidu"),
            source_root=source_root,
            created_at=now,
            input_file=input_path,
            file_count=file_count,
            video_count=video_count,
            files=files,
        )

    def snapshot_entries(self, input_path: str, source_root: str) -> list[SourceNodeInput]:
        """从百度目录树 TXT 一次性生成 SourceNodeInput（复用既有 parse）。"""
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
        """拼接真实路径"""
        parts = list(PurePosixPath(relative_path).parts)
        result = source_root
        for part in parts:
            result = os.path.join(result, part)
        return result
