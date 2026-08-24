"""目录树精确播放根解析。

配置中的 provider 总根只是安全边界和候选起点，不等于每个 TXT 实际覆盖的
媒体作用域根。本模块只构造少量有界候选，并用头/中/尾均匀视频样本验证，
绝不递归枚举整块网盘；只有唯一全部命中的候选才被采用。

scan 调用方负责只读取和解码 TXT 一次：把同一份已解码文本同时交给
resolve() 与 evidence 构建，避免重复访问挂载盘。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.media_v4.sources.scanner import _path_is_within, tree_media_relative_paths

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

    def resolve(self, text: str) -> TreeRootResolution:
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
