"""C-001 来源文件身份：定位符词法规范化与连续性判定（纯函数，零文件系统 I/O）。

三种身份必须分开：

- ``SourceEvidence`` 是一次观察（含 scan_id）；
- ``SourceFile`` 是来源命名空间内持续追踪的文件槽位；
- ``Asset`` 是其中一个可播放内容代次。

本模块只做词法判断，不访问 ``exists``/``resolve(strict=True)``/``open``，也不读挂载盘、
不算内容哈希。Windows 命名空间只在**比较键**上 casefold，显示与实际 locator 保留原大小写；
远端 POSIX 路径按服务语义区分大小写，不做通用 casefold。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.media_v4.domain.identity import (
    CONTINUITY_NEW,
    CONTINUITY_RENAMED,
    CONTINUITY_REPLACED,
    CONTINUITY_UNCERTAIN,
    CONTINUITY_UNCHANGED,
    IDENTITY_KIND_LOCATOR,
    STRENGTH_CONTENT_HASH,
    STRENGTH_LOCATOR_ONLY,
    STRENGTH_NONE,
    STRENGTH_PROVIDER_ID,
    STRENGTH_STAT,
    SourceIdentityDecision,
)

#: SourceFile 的稳定派生命名空间：同一个 origin evidence 无论预览多少次都得到同一 ID。
SOURCE_FILE_UUID_NAMESPACE = uuid.UUID("0f6b6a2e-6c1f-5a4b-9d2e-8a7c3b5d1e40")

_DRIVE_RE = re.compile(r"^([A-Za-z]):(?=[/\\]|$)")

#: 大小写不敏感的命名空间：Windows 盘符/UNC 与本地扫描。
_WINDOWS_NAMESPACE_KINDS = frozenset({"windows", "local", "unc", "drive", "mounted"})

_NAMESPACE_ALIASES = {
    "local": "local",
    "local_scan": "local",
    "windows": "local",
    "drive": "local",
    "unc": "local",
    "pan115": "mounted",
    "baidu": "mounted",
    "quark": "mounted",
    "other": "mounted",
    "openlist": "remote",
}


class LocatorError(ValueError):
    """来源定位符词法错误；``code`` 供上层映射为可读原因。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def derived_source_file_id(evidence_id: str) -> str:
    """由一个出生观察推导稳定的 SourceFile 候选 ID；重复预览结果一致。"""

    return "sf_" + uuid.uuid5(SOURCE_FILE_UUID_NAMESPACE, str(evidence_id)).hex


def namespace_kind(provider: str) -> str:
    """把 provider/ingest 标签映射为命名空间种类：local / mounted / remote。"""

    return _NAMESPACE_ALIASES.get((provider or "").strip().casefold(), "remote")


def namespace_key(*, root_id: str, route_id: str = "") -> str:
    """来源命名空间以 root_id 为边界；OpenList 再叠加连接/路由身份。"""

    parts = ["root", str(root_id or "")]
    if route_id:
        parts.append(str(route_id))
    return "\x1f".join(parts)


def is_absolute_locator(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    normalized = text.replace("\\", "/")
    return normalized.startswith("//") or normalized.startswith("/") or _DRIVE_RE.match(normalized) is not None


def canonical_source_locator(value: str, *, allow_escape: bool = False) -> str:
    """按协议词法规范化来源定位符。

    保留：UNC 的 ``\\\\server\\share`` 前两段、以及**合法相邻同名段**
    （``C:\\media\\作品甲\\作品甲\\ep01.mkv`` 完全不变）。
    处理：``.`` 段词法消去；``..`` 允许在同一根内回退，逃出根（相对路径开头、
    绝对路径越过根、UNC 越过 share）抛出 ``LocatorError('escapes_root')``。
    不访问文件系统，不按媒体名称清洗规则改写任何段。
    """

    text = (value or "").strip()
    if not text:
        return ""
    separator = "\\" if "\\" in text else "/"
    raw = text.replace("\\", "/")
    prefix = ""
    if raw.startswith("//"):
        parts = raw[2:].split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise LocatorError("unc_incomplete", f"UNC 定位符缺少 server/share: {value}")
        prefix = separator * 2 + parts[0] + separator + parts[1]
        rest = parts[2:]
    else:
        drive = _DRIVE_RE.match(raw)
        if drive is not None:
            prefix = drive.group(1).upper() + ":"
            rest = raw[drive.end():].lstrip("/").split("/")
        elif raw.startswith("/"):
            prefix = "/"
            rest = raw.lstrip("/").split("/")
        else:
            rest = raw.split("/")

    segments: list[str] = []
    for segment in rest:
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
                continue
            if prefix or not allow_escape:
                raise LocatorError("escapes_root", f"定位符跳出所选根: {value}")
            segments.append("..")
            continue
        segments.append(segment)

    body = separator.join(segments)
    if not prefix:
        return body
    if prefix == "/":
        return "/" + body
    if prefix.endswith(":"):
        return prefix + separator + body if body else prefix + separator
    # UNC：prefix 已含 server/share 两段，只需再补一个分隔符。
    return prefix + (separator + body if body else "")


def locator_comparison_key(value: str, *, namespace_kind_name: str = "remote") -> str:
    """比较键：Windows/挂载命名空间 casefold，远端 POSIX 保留大小写。"""

    try:
        canonical = canonical_source_locator(value, allow_escape=True)
    except LocatorError:
        canonical = (value or "").strip().replace("\\", "/")
    if namespace_kind_name in _WINDOWS_NAMESPACE_KINDS:
        return canonical.casefold()
    return canonical


@dataclass(frozen=True, slots=True)
class ObservedFile:
    """一次观察的连续性输入；不含任何作品或集号语义。"""

    evidence_id: str
    source_key: str = ""
    locator: str = ""
    identity_namespace: str = ""
    namespace_kind_name: str = "remote"
    source_file_id: str = ""
    asset_id: str | None = None
    size: int | None = None
    mtime: float | None = None
    content_hash: str = ""
    raw_file_id: str = ""
    identity_kind: str = IDENTITY_KIND_LOCATOR
    mtime_reliable: bool = True

    @property
    def slot_key(self) -> str:
        return locator_comparison_key(
            self.locator or self.source_key, namespace_kind_name=self.namespace_kind_name
        )


def _current_strength(current: ObservedFile) -> str:
    if current.raw_file_id:
        return STRENGTH_PROVIDER_ID
    if current.content_hash:
        return STRENGTH_CONTENT_HASH
    if current.size is not None or current.mtime is not None:
        return STRENGTH_STAT
    if current.locator or current.source_key:
        return STRENGTH_LOCATOR_ONLY
    return STRENGTH_NONE


def _decision(
    current: ObservedFile,
    *,
    continuity: str,
    strength: str,
    source_file_id: str,
    previous_evidence_id: str | None,
    reasons: tuple[str, ...],
) -> SourceIdentityDecision:
    return SourceIdentityDecision(
        source_file_id=source_file_id or derived_source_file_id(current.evidence_id),
        continuity=continuity,
        strength=strength,
        previous_evidence_id=previous_evidence_id,
        reasons=reasons,
    )


def _unique_rename_match(
    current: ObservedFile, candidates: Sequence[ObservedFile]
) -> tuple[ObservedFile | None, str]:
    """改名只在可靠 raw_file_id 唯一相等、或受信内容哈希唯一相等时成立。"""

    if current.raw_file_id:
        matches = [item for item in candidates if item.raw_file_id == current.raw_file_id]
        if len(matches) == 1:
            return matches[0], STRENGTH_PROVIDER_ID
        if len(matches) > 1:
            return None, "raw_file_id_ambiguous"
    if current.content_hash:
        matches = [item for item in candidates if item.content_hash == current.content_hash]
        if len(matches) == 1:
            return matches[0], STRENGTH_CONTENT_HASH
        if len(matches) > 1:
            return None, "duplicate_hash_ambiguous"
    return None, "no_rename_evidence"


def decide_source_identity(
    current: ObservedFile,
    previous_observations: Sequence[ObservedFile],
    *,
    namespace: str | None = None,
) -> SourceIdentityDecision:
    """判定本次观察是同一个文件槽位还是新的内容代次（C-001 分支 1—4）。"""

    target_namespace = namespace or current.identity_namespace
    same_namespace = [
        item
        for item in previous_observations
        if (item.identity_namespace or target_namespace) == target_namespace
    ]

    slot = current.slot_key
    same_slot = next((item for item in same_namespace if item.slot_key == slot), None)

    if same_slot is None:
        rename, strength = _unique_rename_match(current, same_namespace)
        if rename is not None:
            return _decision(
                current,
                continuity=CONTINUITY_RENAMED,
                strength=strength,
                source_file_id=rename.source_file_id,
                previous_evidence_id=rename.evidence_id,
                reasons=("renamed_by_" + strength,),
            )
        return _decision(
            current,
            continuity=CONTINUITY_NEW,
            strength=_current_strength(current),
            source_file_id="",
            previous_evidence_id=None,
            reasons=(strength,) if strength in {"raw_file_id_ambiguous", "duplicate_hash_ambiguous"} else ("no_previous_slot",),
        )

    reasons: list[str] = []
    if current.content_hash and same_slot.content_hash:
        if current.content_hash == same_slot.content_hash:
            return _decision(
                current,
                continuity=CONTINUITY_UNCHANGED,
                strength=STRENGTH_CONTENT_HASH,
                source_file_id=same_slot.source_file_id,
                previous_evidence_id=same_slot.evidence_id,
                reasons=("content_hash_equal",),
            )
        return _decision(
            current,
            continuity=CONTINUITY_REPLACED,
            strength=STRENGTH_CONTENT_HASH,
            source_file_id=same_slot.source_file_id,
            previous_evidence_id=same_slot.evidence_id,
            reasons=("content_hash_changed",),
        )
    if current.raw_file_id and same_slot.raw_file_id:
        equal = current.raw_file_id == same_slot.raw_file_id
        return _decision(
            current,
            continuity=CONTINUITY_UNCHANGED if equal else CONTINUITY_REPLACED,
            strength=STRENGTH_PROVIDER_ID,
            source_file_id=same_slot.source_file_id,
            previous_evidence_id=same_slot.evidence_id,
            reasons=("provider_id_equal",) if equal else ("provider_id_changed",),
        )
    if current.size is not None and same_slot.size is not None and current.size != same_slot.size:
        return _decision(
            current,
            continuity=CONTINUITY_REPLACED,
            strength=STRENGTH_STAT,
            source_file_id=same_slot.source_file_id,
            previous_evidence_id=same_slot.evidence_id,
            reasons=("size_changed",),
        )
    mtime_changed = (
        current.mtime is not None
        and same_slot.mtime is not None
        and current.mtime != same_slot.mtime
    )
    if mtime_changed:
        if current.mtime_reliable and same_slot.mtime_reliable:
            return _decision(
                current,
                continuity=CONTINUITY_UNCERTAIN,
                strength=STRENGTH_STAT,
                source_file_id=same_slot.source_file_id,
                previous_evidence_id=same_slot.evidence_id,
                reasons=("mtime_changed_without_content_evidence",),
            )
        reasons.append("mtime_drift_not_content_evidence")
    strength = (
        STRENGTH_CONTENT_HASH
        if (current.content_hash and same_slot.content_hash)
        else STRENGTH_PROVIDER_ID
        if (current.raw_file_id and same_slot.raw_file_id)
        else STRENGTH_STAT
        if (current.size is not None and same_slot.size is not None)
        else STRENGTH_LOCATOR_ONLY
    )
    reasons.append("locator_equal")
    return _decision(
        current,
        continuity=CONTINUITY_UNCHANGED,
        strength=strength,
        source_file_id=same_slot.source_file_id,
        previous_evidence_id=same_slot.evidence_id,
        reasons=tuple(reasons),
    )


def continuity_reuses_asset(continuity: str) -> bool:
    """unbounded 判定：这些连续性允许沿用原 Asset 与观看进度。"""

    return continuity in {CONTINUITY_UNCHANGED, CONTINUITY_RENAMED}


def continuity_creates_new_asset(continuity: str) -> bool:
    return continuity in {CONTINUITY_NEW, CONTINUITY_REPLACED, CONTINUITY_UNCERTAIN}
