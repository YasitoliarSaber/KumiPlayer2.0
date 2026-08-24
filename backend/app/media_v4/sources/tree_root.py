"""目录树精确播放根解析与扫描元数据暂存。

配置中的 provider 总根只是安全边界和候选起点，不等于每个 TXT 实际覆盖的
媒体作用域根。本模块只构造少量有界候选，并用头/中/尾均匀视频样本验证，
绝不递归枚举整块网盘；只有唯一全部命中的候选才被采用。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir
from app.media_v4.sources.scanner import (
    _path_is_within,
    read_directory_tree_text,
    tree_media_relative_paths,
)

_MAX_SAMPLES = 3


def tree_scope_name(stem: str) -> str:
    """从目录树文件名提取媒体作用域，例如「01动画_文件目录_时间戳」→「01动画」。

    115 的「根目录...」导出覆盖整个挂载根，不额外增加子目录。
    传入完整文件名时先去掉扩展名，避免作用域带上 .txt。
    """
    value = stem
    suffix = Path(value).suffix
    if suffix:
        value = Path(value).stem
    scope = re.sub(r"[_\s-]*(?:文件目录|目录树)(?:[_\s-]*\d{6,})?$", "", value).strip()
    if scope.casefold().startswith("根目录"):
        return ""
    return scope


def _norm(value: str) -> str:
    return str(Path(value).expanduser()).replace("/", "\\").rstrip("\\").casefold()


def _video_samples(relative_paths: list[str]) -> list[str]:
    if not relative_paths:
        return []
    if len(relative_paths) <= _MAX_SAMPLES:
        return list(relative_paths)
    indexes = sorted({0, len(relative_paths) - 1, len(relative_paths) // 2})
    return [relative_paths[index] for index in indexes]


@dataclass(frozen=True, slots=True)
class TreeRootResolution:
    root: str
    ok: bool
    reason: str
    hits: int
    total: int
    candidates: tuple[str, ...]


class TreePlaybackRootResolver:
    """解析 TXT 实际覆盖的精确播放根；候选根全部由配置与文件位置派生。"""

    def __init__(
        self,
        tree_path: str | Path,
        *,
        configured_roots: Iterable[str | Path],
        extra_candidates: Iterable[str | Path] = (),
    ) -> None:
        self.tree_path = Path(tree_path).expanduser()
        self.configured_roots = [str(Path(root).expanduser()) for root in configured_roots]
        self.extra_candidates = [str(Path(root).expanduser()) for root in extra_candidates]

    def resolve(self) -> TreeRootResolution:
        text = read_directory_tree_text(self.tree_path)
        relative_paths = tree_media_relative_paths(text)
        samples = _video_samples(relative_paths)
        total = len(samples)
        candidates = self._build_candidates()
        if not candidates:
            return TreeRootResolution("", False, "当前内容来源尚未配置本地挂载路径", 0, total, ())
        results: list[tuple[str, int]] = []
        for candidate in candidates:
            hits = 0
            for relative in samples:
                target = Path(candidate)
                for part in PurePosixPath(relative).parts:
                    target /= part
                try:
                    if target.is_file():
                        hits += 1
                except OSError:
                    hits = 0
                    break
            results.append((candidate, hits))
        all_hit = [(candidate, hits) for candidate, hits in results if total > 0 and hits == total]
        unique = {_norm(candidate): candidate for candidate, _hits in all_hit}
        if len(unique) == 1:
            root = next(iter(unique.values()))
            return TreeRootResolution(
                root, True, "目录树视频样本全部可达", total, total, tuple(candidates)
            )
        if not all_hit:
            return TreeRootResolution(
                "",
                False,
                "目录树视频样本在当前映射下不可达，请检查来源范围或挂载状态",
                0,
                total,
                tuple(candidates),
            )
        return TreeRootResolution(
            "",
            False,
            "多个候选根都能命中视频样本，请检查设置中的来源范围",
            total,
            total,
            tuple(candidates),
        )

    def _build_candidates(self) -> list[str]:
        candidates: list[str] = []

        def add(value: str) -> None:
            value = str(Path(value).expanduser()).rstrip("\\/")
            if value and not any(_norm(value) == _norm(existing) for existing in candidates):
                candidates.append(value)

        tree_parent = self.tree_path.parent
        for root in self.configured_roots:
            if _path_is_within(tree_parent, root):
                add(str(tree_parent))
        for root in self.configured_roots:
            add(root)
        scope = tree_scope_name(self.tree_path.stem)
        if scope:
            for root in self.configured_roots:
                add(str(Path(root) / scope))
        for extra in self.extra_candidates:
            add(extra)
        return candidates


# ---------------------------------------------------------------
# 扫描元数据暂存：scan 产生后端权威的播放根与抽样验证结果，
# preview/confirm 根据 scan_id 读取，不再采信前端拼装的总根副本。
# ---------------------------------------------------------------


def _metadata_root() -> Path:
    return get_data_dir() / "scan_metadata"


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _metadata_path(scan_id: str) -> Path:
    return _metadata_root() / f"{_safe_key(scan_id)}.json"


def stage_scan_metadata(scan_id: str, metadata: dict) -> None:
    path = _metadata_path(scan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with DATA_WRITE_LOCK:
        write_json_atomic(path, metadata)


def load_scan_metadata(scan_id: str) -> dict | None:
    try:
        payload = json.loads(_metadata_path(scan_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def consume_scan_metadata(scan_id: str) -> None:
    """确认成功后清理暂存；失败或放弃的扫描元数据不污染后续确认。"""

    _metadata_path(scan_id).unlink(missing_ok=True)
