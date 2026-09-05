"""TXT 来源输入的不可变受控归档（C3 合同）。

选中目录树 TXT 后、返回任务身份前，把原文件只读复制到 KumiPlayer 数据
目录下的受控归档并记录 SHA-256。原 TXT 永不改写；同一内容哈希重复选择
时复用不可变归档。归档是 durable 扫描恢复的唯一输入依据：进程失联后，
只有「归档存在且哈希一致」才允许重新排队，避免从挂载盘或临时路径反推
输入。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from app.core.paths import get_data_dir
from app.media_v4.sources.scanner import MAX_TREE_FILE_BYTES

_SAFE_SEGMENT_RE = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]{1,120}$")


class InputArchiveError(ValueError):
    """归档输入不满足大小/内容/路径边界。"""


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip(" .")
    return cleaned or fallback


def archive_root() -> Path:
    return get_data_dir() / "source_inputs"


def archive_tree_input(root_id: str, tree_file: str | Path) -> dict:
    """只读复制一次原 TXT 到受控归档；返回归档事实（相对路径 + 哈希）。

    流程：读一次原文件 → 校验大小边界 → 临时文件 → 原子换位 → 复核
    SHA-256。同一哈希重复选择时直接复用既有不可变归档。绝不写回原文件。
    """

    source = Path(tree_file).expanduser()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    safe_name = _safe_segment(source.name, "directory-tree.txt")
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    with open(source, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_TREE_FILE_BYTES:
                raise InputArchiveError("目录树文件过大，请拆分后重新导出")
            digest.update(chunk)
            chunks.append(chunk)
    if size == 0:
        raise InputArchiveError("目录树文件为空，请重新导出 TXT")
    sha256 = digest.hexdigest()

    target_dir = archive_root() / _safe_segment(root_id, "root")
    target = target_dir / f"{sha256}_{safe_name}"
    relative_path = f"source_inputs/{_safe_segment(root_id, 'root')}/{target.name}"
    if target.is_file():
        if _file_sha256(target) == sha256:
            return _archive_fact(relative_path, sha256, source.name, size)
        # 哈希前缀冲突（极小概率同名不同内容）：换一个不冲突的归档名。
        target = target_dir / f"{sha256}_{uuid.uuid4().hex[:8]}_{safe_name}"
        relative_path = f"source_inputs/{_safe_segment(root_id, 'root')}/{target.name}"

    target_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        # 内容已在内存中；写临时文件后原子换位，replace 之后还会复核哈希，
        # 断电留下的半截文件会在恢复路径上被哈希校验拒绝。
        temporary.write_bytes(b"".join(chunks))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if _file_sha256(target) != sha256:
        # 复核失败：不留下不可信归档，恢复路径宁可显式失败。
        target.unlink(missing_ok=True)
        raise InputArchiveError("归档复核失败，请重新选择 TXT")
    return _archive_fact(relative_path, sha256, source.name, size)


def resolve_archive_path(archive_relative_path: str) -> Path:
    """把受控相对路径解析为归档根内的绝对路径。

    相对路径必须是固定三段 `source_inputs/<root_id>/<文件名>`：先逐段
    白名单校验（禁止 `..`、空段与非法字符），再拼接并复核仍在归档根内。
    """

    value = str(archive_relative_path or "").strip().replace("\\", "/")
    parts = value.split("/")
    if len(parts) != 3 or parts[0] != "source_inputs":
        raise InputArchiveError("归档路径不受信任，拒绝读取")
    if any(part in {"", ".", ".."} for part in parts):
        raise InputArchiveError("归档路径不受信任，拒绝读取")
    if any(not _SAFE_SEGMENT_RE.fullmatch(part) for part in parts[1:]):
        raise InputArchiveError("归档路径不受信任，拒绝读取")
    archive_base = archive_root().resolve()
    root_dir = (archive_base / parts[1]).resolve()
    candidate = (root_dir / parts[2]).resolve()
    if candidate.parent != root_dir or root_dir.parent != archive_base:
        raise InputArchiveError("归档路径不受信任，拒绝读取")
    return candidate


def archive_is_intact(archive_relative_path: str, expected_sha256: str) -> bool:
    """归档存在且哈希一致才允许恢复。"""

    if not archive_relative_path or not expected_sha256:
        return False
    try:
        candidate = resolve_archive_path(archive_relative_path)
    except (InputArchiveError, OSError):
        return False
    if not candidate.is_file():
        return False
    try:
        return _file_sha256(candidate) == expected_sha256
    except OSError:
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_fact(relative_path: str, sha256: str, original_filename: str, size: int) -> dict:
    return {
        "archive_path": relative_path,
        "sha256": sha256,
        "original_filename": original_filename,
        "size": size,
    }
