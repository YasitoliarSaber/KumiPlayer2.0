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

from app.media_v4.sources.scanner import tree_media_relative_paths, tree_root_id

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


def _deepest_candidates(roots: list[str]) -> list[str]:
    """去掉是其它候选祖先的项，保留每条祖先链最深节点。

    同一祖先链必须收敛到最深层的配置根（如同时配置 `Q:\百度网盘` 与
    `Q:\百度网盘\01动画` 时保留子根）；互不为祖先的多个最深节点交由
    调用方报歧义。基于集合成员判断，候选顺序不影响结果。
    """

    return [
        root for root in roots
        if not any(_is_ancestor(root, other) for other in roots)
    ]


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


def tree_scan_root_id(
    *,
    provider: str,
    configured_roots: list[str],
    resolution: TreeRootResolution,
    tree_file: str | Path,
) -> str:
    """同步与 durable 共用的「解析后身份根」。

    成功映射统一采用 `resolution.root`；解析失败时回退首个稳定配置身份，
    用于保存失败任务。两条入口必须共享同一纯词法输入（configured_roots +
    TXT 解析结果），同一逻辑来源不得因入口不同分裂成不同 root；调用方
    必须在创建 root/scan 身份之前完成 TXT 读取与解析。
    """

    if resolution.ok and resolution.root:
        identity_root = resolution.root
    elif configured_roots:
        identity_root = str(configured_roots[0])
    else:
        identity_root = ""
    return tree_root_id(provider, identity_root, tree_file)


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
        # 去重（忽略大小写但保留原文字），再沿祖先链收敛到最深子根。
        unique: dict[str, str] = {}
        for candidate in candidates:
            unique.setdefault(_norm(candidate), candidate)
        roots = list(unique.values())
        precise = _deepest_candidates(roots)
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

        def add_without_doubled_scope(value: str) -> None:
            """条目首段已带 scope 时，尾部又是同一 scope 的候选会重复该层。

            例如条目为「01动画/Show/...」而根为 `Q:\百度网盘\01动画`，直接
            拼接会出现两段 01动画；此时以去掉该层的父目录作为候选。
            """

            if entry_has_scope and _root_has_scope(value, scope):
                add(str(_pure(value).parent))
            else:
                add(value)

        for root in self.configured_roots:
            root_path = _pure(root)
            # 优先采用可由 TXT 位置证明的子库范围。
            if tree_parent and _is_ancestor(root_path, _pure(str(tree_parent))):
                add_without_doubled_scope(str(tree_parent))
            elif scope and not entry_has_scope and not _root_has_scope(root, scope):
                add(str(root_path / scope))
            else:
                add_without_doubled_scope(str(root_path))
        for extra in self.extra_candidates:
            add_without_doubled_scope(str(_pure(extra)))
        return candidates
