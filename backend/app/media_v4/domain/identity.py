"""C-001/C-004 身份与决策的不可变值对象。

本模块只放"谁的证据、什么来源、哪条规则"这类确定性词法与值对象：

- ``SourceIdentityDecision`` 表达一次来源文件连续性判定（C-001）；
- ``NumberingEvidence`` / ``DecisionTrace`` 表达编号与字段的来源记录（C-004）；
- ``canonical_json`` 系列函数是集合级排序、签名与落库 JSON 的唯一实现，
  UTF-8 紧凑 JSON，字典按键排序，标题本身绝不按冒号或竖线拆分。

所有对象都是 frozen dataclass；读取历史 JSON 失败时返回 unknown/legacy 分支，
不把不可解析内容提升为 explicit。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# --- 来源文件连续性（C-001） ------------------------------------------------

CONTINUITY_NEW = "new"
CONTINUITY_UNCHANGED = "unchanged"
CONTINUITY_RENAMED = "renamed"
CONTINUITY_REPLACED = "replaced"
CONTINUITY_UNCERTAIN = "uncertain"
CONTINUITY_STATES = frozenset(
    {
        CONTINUITY_NEW,
        CONTINUITY_UNCHANGED,
        CONTINUITY_RENAMED,
        CONTINUITY_REPLACED,
        CONTINUITY_UNCERTAIN,
    }
)

STRENGTH_PROVIDER_ID = "provider_id"
STRENGTH_CONTENT_HASH = "content_hash"
STRENGTH_STAT = "stat"
STRENGTH_LOCATOR_ONLY = "locator_only"
STRENGTH_NONE = "none"
STRENGTH_LEVELS = (
    STRENGTH_PROVIDER_ID,
    STRENGTH_CONTENT_HASH,
    STRENGTH_STAT,
    STRENGTH_LOCATOR_ONLY,
    STRENGTH_NONE,
)

IDENTITY_KIND_LOCATOR = "locator"
IDENTITY_KIND_PROVIDER_ID = "provider_id"
IDENTITY_KIND_CONTENT_HASH = "content_hash"
IDENTITY_KINDS = frozenset(
    {IDENTITY_KIND_LOCATOR, IDENTITY_KIND_PROVIDER_ID, IDENTITY_KIND_CONTENT_HASH}
)

# --- 分类与未知值（C-002） --------------------------------------------------

CONTENT_CLASS_REGULAR = "regular"
CONTENT_CLASS_MOVIE = "movie"
CONTENT_CLASS_STANDALONE = "standalone"
CONTENT_CLASS_ATTACHED_SPECIAL = "attached_special"
CONTENT_CLASS_AUXILIARY = "auxiliary"
CONTENT_CLASS_UNKNOWN = "unknown"
CONTENT_CLASSES = (
    CONTENT_CLASS_REGULAR,
    CONTENT_CLASS_MOVIE,
    CONTENT_CLASS_STANDALONE,
    CONTENT_CLASS_ATTACHED_SPECIAL,
    CONTENT_CLASS_AUXILIARY,
    CONTENT_CLASS_UNKNOWN,
)
# 不生成媒体实体、不参与刮削、不生成镜像的类别。
NON_IMPORTABLE_CONTENT_CLASSES = frozenset(
    {CONTENT_CLASS_ATTACHED_SPECIAL, CONTENT_CLASS_AUXILIARY}
)

CLASSIFICATION_EXPLICIT = "explicit"
CLASSIFICATION_INFERRED = "inferred"
CLASSIFICATION_CONFLICT = "conflict"
CLASSIFICATION_UNKNOWN = "unknown"
CLASSIFICATION_STATES = (
    CLASSIFICATION_EXPLICIT,
    CLASSIFICATION_INFERRED,
    CLASSIFICATION_CONFLICT,
    CLASSIFICATION_UNKNOWN,
)

MEDIA_TYPE_TV = "tv"
MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_UNKNOWN = "unknown"
MEDIA_TYPES = (MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE, MEDIA_TYPE_UNKNOWN)

WORK_TYPE_SERIES = "series"
WORK_TYPE_MOVIE = "movie"
WORK_TYPE_UNKNOWN = "unknown"
WORK_TYPES = (WORK_TYPE_SERIES, WORK_TYPE_MOVIE, WORK_TYPE_UNKNOWN)

SEASON_KIND_REGULAR = "regular"
SEASON_KIND_UNASSIGNED = "unassigned"

# --- 字段来源（C-004） ------------------------------------------------------

ORIGIN_FILENAME = "filename"
ORIGIN_DIRECTORY = "directory"
ORIGIN_STRUCTURED_HINT = "structured_hint"
ORIGIN_SIDECAR = "sidecar"
ORIGIN_PARSER_RULE = "parser_rule"
ORIGIN_EXPLICIT_FILENAME = "explicit_filename"
ORIGIN_EXPLICIT_DIRECTORY = "explicit_directory"
ORIGIN_EXPLICIT_ABSOLUTE = "explicit_absolute"
ORIGIN_LOCAL_UNSCOPED = "local_unscoped"
ORIGIN_UNKNOWN = "unknown"

TRACE_ORIGINS = frozenset(
    {
        ORIGIN_FILENAME,
        ORIGIN_DIRECTORY,
        ORIGIN_STRUCTURED_HINT,
        ORIGIN_SIDECAR,
        ORIGIN_PARSER_RULE,
        ORIGIN_UNKNOWN,
        # 编号来源也是合法的 trace 来源，必须可无损落库/读回。
        ORIGIN_EXPLICIT_FILENAME,
        ORIGIN_EXPLICIT_DIRECTORY,
        ORIGIN_EXPLICIT_ABSOLUTE,
        ORIGIN_LOCAL_UNSCOPED,
    }
)
NUMBERING_ORIGINS = frozenset(
    {
        ORIGIN_EXPLICIT_FILENAME,
        ORIGIN_EXPLICIT_DIRECTORY,
        ORIGIN_EXPLICIT_ABSOLUTE,
        ORIGIN_LOCAL_UNSCOPED,
        ORIGIN_UNKNOWN,
    }
)

CONTRACT_VERSION = 1


def canonical_json(value: Any) -> str:
    """UTF-8 紧凑 JSON；字典键排序，保证同一语义值得到同一字节串。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_json_array(value: Any) -> str:
    """以数组表达的规范键；不把标题按冒号或竖线拆成伪层级。"""

    return canonical_json(value)


def digest_of(value: Any) -> str:
    """对规范 JSON 取 SHA-256，用于语义签名与 digest 比较。"""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _load_json(raw: str, fallback: Any) -> Any:
    """读取历史 JSON；不可解析时返回 fallback，绝不提升为 explicit。"""

    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True, slots=True)
class SourceIdentityDecision:
    """一次"这个观察是否还是同一个文件槽位"的判定结果（C-001）。"""

    source_file_id: str
    continuity: str = CONTINUITY_NEW
    strength: str = STRENGTH_NONE
    previous_evidence_id: str | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return canonical_json(
            {
                "source_file_id": self.source_file_id,
                "continuity": self.continuity,
                "strength": self.strength,
                "previous_evidence_id": self.previous_evidence_id,
                "reasons": list(self.reasons),
            }
        )


@dataclass(frozen=True, slots=True)
class NumberingEvidence:
    """编号字段各自的来源与作用域（C-004）；缺证据用 unknown。"""

    season_origin: str = ORIGIN_UNKNOWN
    episode_origin: str = ORIGIN_UNKNOWN
    absolute_origin: str = ORIGIN_UNKNOWN
    scope_key: str = ""
    basis: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return canonical_json(
            {
                "season_origin": self.season_origin,
                "episode_origin": self.episode_origin,
                "absolute_origin": self.absolute_origin,
                "scope_key": self.scope_key,
                "basis": list(self.basis),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> NumberingEvidence:
        data = _load_json(raw, None)
        if not isinstance(data, dict):
            return cls()
        season_origin = str(data.get("season_origin") or ORIGIN_UNKNOWN)
        episode_origin = str(data.get("episode_origin") or ORIGIN_UNKNOWN)
        absolute_origin = str(data.get("absolute_origin") or ORIGIN_UNKNOWN)
        basis_raw = data.get("basis")
        basis = tuple(str(item) for item in basis_raw) if isinstance(basis_raw, list) else ()
        return cls(
            season_origin=season_origin if season_origin in NUMBERING_ORIGINS else ORIGIN_UNKNOWN,
            episode_origin=episode_origin if episode_origin in NUMBERING_ORIGINS else ORIGIN_UNKNOWN,
            absolute_origin=absolute_origin if absolute_origin in NUMBERING_ORIGINS else ORIGIN_UNKNOWN,
            scope_key=str(data.get("scope_key") or ""),
            basis=basis,
        )


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    """一个字段为什么取这个值：来源、作用域、规则与被拒候选（C-004）。"""

    field: str
    value: Any
    origin: str
    scope: str = ""
    rule_id: str = ""
    confidence: str = "medium"
    # 空元组是不可变默认值，不使用 dataclasses.field（该名字被本类的 field 字段遮蔽）。
    alternatives: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "origin": self.origin if self.origin in TRACE_ORIGINS else ORIGIN_UNKNOWN,
            "scope": self.scope,
            "rule_id": self.rule_id,
            "confidence": self.confidence,
            "alternatives": list(self.alternatives),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> DecisionTrace:
        if not isinstance(payload, dict):
            return cls(field="", value=None, origin=ORIGIN_UNKNOWN)
        alternatives_raw = payload.get("alternatives")
        return cls(
            field=str(payload.get("field") or ""),
            value=payload.get("value"),
            origin=str(payload.get("origin") or ORIGIN_UNKNOWN),
            scope=str(payload.get("scope") or ""),
            rule_id=str(payload.get("rule_id") or ""),
            confidence=str(payload.get("confidence") or "medium"),
            alternatives=tuple(str(item) for item in alternatives_raw)
            if isinstance(alternatives_raw, list)
            else (),
        )


def decision_trace_json(traces: tuple[DecisionTrace, ...]) -> str:
    return canonical_json([trace.to_payload() for trace in traces])


def load_decision_traces(raw: str) -> tuple[DecisionTrace, ...]:
    data = _load_json(raw, None)
    if not isinstance(data, list):
        return ()
    return tuple(DecisionTrace.from_payload(item) for item in data)


def trace_field(traces: tuple[DecisionTrace, ...], field_name: str) -> DecisionTrace | None:
    for trace in traces:
        if trace.field == field_name:
            return trace
    return None


def stable_sorted_keys(keys: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """集合级序列化的稳定顺序：按规范字节串排序，不依赖遍历顺序。"""

    return tuple(sorted(str(key) for key in keys))
