"""把不同入口的扫描条目转换成统一 SourceEvidence。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.sources.file_identity import LocatorError, canonical_source_locator

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def provider_to_source(provider_id: str) -> str:
    """把内容提供商映射为纯解析所需的来源语义。"""

    if provider_id in {"pan115", "baidu", "quark", "other", "local"}:
        return provider_id
    return "openlist"


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """来源适配器的最小输入，不包含任何作品识别结果。"""

    root_id: str
    scan_id: str
    provider: str
    ingest_method: str
    relative_path: str
    source_key: str = ""
    source_locator: str = ""
    playback_locator: str = ""
    raw_file_id: str = ""
    source_route_id: str = ""
    size: int | None = None
    mtime: float | None = None
    fingerprint: str = ""
    entry_kind: str = "video"
    tmdb_hint_id: str = ""
    tmdb_hint_type: str = ""
    import_family: str = "anime"
    target_filename: str = ""
    observed_at: str = ""
    observation_kind: str = "observed"
    parent_evidence_id: str = ""


def _normalize_relative_path(value: str) -> str:
    """词法规范化相对路径：保留合法相邻同名段，不触盘。

    ``.`` 段消去；`..` 逃出所选根时拒绝（不再用 ``normpath().lstrip('/')``
    掩掉越界）；盘符/UNC 开头的绝对路径属于来源根不匹配，直接拒绝。
    """

    text = (value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    if _DRIVE_PREFIX.match(normalized) or normalized.startswith("//"):
        raise LocatorError("absolute_relative", f"相对路径字段收到绝对定位符: {value}")
    return canonical_source_locator(normalized.lstrip("/"))


def to_source_evidence(entry: SourceEntry) -> SourceEvidence:
    """生成稳定 evidence id；不解析标题、季度或集号。"""

    relative_path = _normalize_relative_path(entry.relative_path)
    source_key = entry.source_key or relative_path
    digest_input = "\x1f".join((entry.root_id, entry.scan_id, source_key))
    evidence_id = "ev_" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]
    return SourceEvidence(
        evidence_id=evidence_id,
        scan_id=entry.scan_id,
        root_id=entry.root_id,
        source_key=source_key,
        relative_path=relative_path,
        entry_kind=entry.entry_kind,
        provider=entry.provider,
        size=entry.size,
        mtime=entry.mtime,
        fingerprint=entry.fingerprint,
        raw_file_id=entry.raw_file_id,
        ingest_method=entry.ingest_method,
        source_route_id=entry.source_route_id,
        source_locator=entry.source_locator,
        playback_locator=entry.playback_locator,
        tmdb_hint_id=entry.tmdb_hint_id,
        tmdb_hint_type=entry.tmdb_hint_type,
        import_family=entry.import_family,
        target_filename=entry.target_filename,
        observed_at=entry.observed_at,
        observation_kind=entry.observation_kind,
        parent_evidence_id=entry.parent_evidence_id,
    )
