"""目录树播放根词法映射（11.19.2 离线合同）。

TXT 只是目录结构证据：播放根由配置根、TXT 位置与导出文件名范围纯词法
推导，不 stat/open/枚举源盘；源盘离线与在线产生完全相同的结果。只有
真实的多根歧义才失败，不做“样本全部可达”的伪造。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.media_v4.sources.scanner import tree_media_relative_paths

# 导出文件名约定：`01动画_文件目录_时间戳` / `根目录_目录树`。
_EXPORT_SUFFIX_RE = re.compile(r"[_\s-]*(?:文件目录|目录树)(?:[_\s-]*\d{6,})?$")

# Windows 设备路径段（CON/NUL/COM1 等），出现在相对路径中一律拒绝。
_WINDOWS_DEVICE_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


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


def _has_export_scope(stem: str) -> bool:
    """文件名是否带明确的 `_文件目录`/`_目录树` 导出范围标记。"""

    value = Path(stem).stem if Path(stem).suffix else stem
    return bool(_EXPORT_SUFFIX_RE.search(value))


def _pure(value: str) -> PureWindowsPath:
    """把配置根/候选根规范化为纯 Windows 词法路径（不触盘）。"""

    return PureWindowsPath(str(value).strip())


def _norm(value: str) -> str:
    return str(_pure(value)).rstrip("\\").casefold()


def _is_ancestor(candidate: str, other: str) -> bool:
    """candidate 是否是 other 的词法祖先（段级比较，忽略大小写）。"""

    candidate_parts = _pure(candidate).parts
    other_parts = _pure(other).parts
    if len(other_parts) <= len(candidate_parts):
        return False
    return all(a.casefold() == b.casefold() for a, b in zip(candidate_parts, other_parts, strict=False))


def _root_has_scope(root: str, scope: str) -> bool:
    """配置根尾段是否已含 scope（如 `Q:\\百度网盘\\01动画`）。"""

    root_parts = _pure(root).parts
    return bool(root_parts) and root_parts[-1].casefold() == scope.casefold()


def _unsafe_relative_reason(relative: str) -> str | None:
    """拒绝 `..`/盘符/绝对路径/NUL/换行/设备路径；先拒原始形态再词法拼接。"""

    if not relative:
        return "目录树条目为空"
    if any(ch in relative for ch in ("\x00", "\n", "\r")):
        return "目录树条目包含控制字符"
    if relative.startswith(("/", "\\")):
        return "目录树条目是绝对路径"
    for part in PurePosixPath(relative).parts:
        if part in {"..", "."}:
            return "目录树条目包含路径跳转"
        if ":" in part or part in {"", "/", "\\"}:
            return "目录树条目包含盘符或分隔符"
        if part.casefold() in _WINDOWS_DEVICE_NAMES:
            return "目录树条目包含设备路径"
    return None


@dataclass(frozen=True, slots=True)
class TreeRootResolution:
    root: str
    ok: bool
    reason: str
    hits: int
    total: int
    candidates: tuple[str, ...]


class TreePlaybackRootResolver:
    """按配置根、TXT 位置与导出文件名词法推导播放根；零源盘 I/O。"""

    def __init__(
        self,
        tree_path: str | Path,
        *,
        configured_roots: Iterable[str | Path],
        extra_candidates: Iterable[str | Path] = (),
    ) -> None:
        self.tree_path = Path(tree_path).expanduser()
        self.configured_roots = [str(root) for root in configured_roots]
        self.extra_candidates = [str(root) for root in extra_candidates]

    def resolve(self, text: str) -> TreeRootResolution:
        relative_paths = tree_media_relative_paths(text)
        for relative in relative_paths:
            reason = _unsafe_relative_reason(relative)
            if reason:
                return TreeRootResolution(
                    "", False, f"目录树条目结构校验拒绝：{reason}（{relative}）",
                    0, 0, (),
                )
        candidates = self._build_candidates(relative_paths)
        if not candidates:
            return TreeRootResolution(
                "", False, "当前内容来源尚未配置本地挂载路径", 0, 0, (),
            )
        # 去重（忽略大小写但保留原文字），去掉被更精确候选包含的祖先候选。
        unique: dict[str, str] = {}
        for candidate in candidates:
            unique.setdefault(_norm(candidate), candidate)
        roots = list(unique.values())
        precise = [
            candidate for candidate in roots
            if not any(_is_ancestor(other, candidate) for other in roots)
        ]
        if len(precise) == 1:
            return TreeRootResolution(
                precise[0], True, "播放路径已根据目录树与来源设置生成",
                0, 0, tuple(roots),
            )
        return TreeRootResolution(
            "", False,
            "多个来源根都可能覆盖该目录树，请检查设置中的来源范围",
            0, 0, tuple(roots),
        )

    def _build_candidates(self, relative_paths: list[str]) -> list[str]:
        candidates: list[str] = []
        tree_parent = self.tree_path.parent
        scope = tree_scope_name(self.tree_path.stem) if _has_export_scope(self.tree_path.stem) else ""
        # 范围已在条目首段时不再追加（如「01动画/Show/...」首段就是 scope）。
        first_segment = PurePosixPath(relative_paths[0]).parts[0] if relative_paths else ""
        entry_has_scope = bool(scope) and first_segment.casefold() == scope.casefold()

        def add(value: str) -> None:
            value = str(value).strip()
            if value and not any(_norm(value) == _norm(existing) for existing in candidates):
                candidates.append(value)

        for root in self.configured_roots:
            root_path = _pure(root)
            # 优先采用可由 TXT 位置证明的子库范围。
            if tree_parent and _is_ancestor(root_path, _pure(str(tree_parent))):
                add(str(tree_parent))
            elif scope and not entry_has_scope and not _root_has_scope(root, scope):
                add(str(root_path / scope))
            else:
                add(str(root_path))
        for extra in self.extra_candidates:
            add(str(_pure(extra)))
        return candidates
