"""V4 Work 级元数据提供器；只做在线映射，不修改本地媒体身份。"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from app.core.config import load_config
from app.media_v4.jobs.provider_numbering import (
    normalized_episode_title,
    verified_tmdb_season_offsets,
)
from app.media_v4.resolution.candidates import query_variants, strip_season_marker
from app.media_v4.resolution.ranker import CandidateRanker, RankedCandidate
from app.scrape.tmdb_client import (
    TMDBAuthError,
    TMDBClient,
    TMDBClientError,
    TMDBRateLimitError,
)

_METADATA_STATES = frozenset({"ready", "waiting_metadata", "waiting_review", "source_unavailable", "failed"})

# --- C-005：Provider 编号映射与诚实状态 -------------------------------------
#
# 执行状态、作品资料、剧集映射、产物与刷新各自独立：没有映射到在线条目不等于
# 远端服务故障。作品信息可保留，但正片映射、标题或在线缩略图缺项时，整体
# metadata_state 不能标为 ready；特别篇的可选资料缺项单独处理。

IDENTITY_STATUS_CONFIRMED = "confirmed"
IDENTITY_STATUS_UNRESOLVED = "unresolved"
IDENTITY_STATUS_CONFLICT = "conflict"

WORK_METADATA_READY = "ready"
WORK_METADATA_PARTIAL = "partial"
WORK_METADATA_UNAVAILABLE = "unavailable"
WORK_METADATA_NOT_REQUESTED = "not_requested"

EPISODE_MAPPING_COMPLETE = "complete"
EPISODE_MAPPING_PARTIAL = "partial"
EPISODE_MAPPING_UNMAPPED = "unmapped"
EPISODE_MAPPING_NOT_APPLICABLE = "not_applicable"

ARTIFACT_STATUS_COMPLETE = "complete"
ARTIFACT_STATUS_PARTIAL = "partial"
ARTIFACT_STATUS_UNAVAILABLE = "unavailable"
ARTIFACT_STATUS_NOT_REQUIRED = "not_required"

REFRESH_STATUS_NOT_REQUESTED = "not_requested"
REFRESH_STATUS_PENDING = "pending"
REFRESH_STATUS_SUCCEEDED = "succeeded"
REFRESH_STATUS_PARTIAL = "partial"
REFRESH_STATUS_FAILED = "failed"
REFRESH_STATUS_CANCELLED = "cancelled"

METADATA_SOURCE_CURRENT = "current"
METADATA_SOURCE_RETAINED = "retained"
METADATA_SOURCE_LOCAL = "local"

#: 未映射 Episode 的原因码（C-005/STEP-007）：每个未映射集必须恰好有一个原因，
#: 不许把"在线请求成功但没有命中集号"写成服务故障。
UNMAPPED_MISSING_LOCAL_NUMBER = "missing_local_number"
UNMAPPED_UNKNOWN_LOCAL_SEASON = "unknown_local_season"
UNMAPPED_INSUFFICIENT_EVIDENCE = "insufficient_numbering_evidence"
UNMAPPED_PROVIDER_RESOURCE_MISSING = "provider_resource_missing"
UNMAPPED_PROVIDER_UNAVAILABLE = "provider_unavailable"
UNMAPPED_NUMBERING_CONFLICT = "numbering_conflict"
UNMAPPED_REASONS = (
    UNMAPPED_MISSING_LOCAL_NUMBER,
    UNMAPPED_UNKNOWN_LOCAL_SEASON,
    UNMAPPED_INSUFFICIENT_EVIDENCE,
    UNMAPPED_PROVIDER_RESOURCE_MISSING,
    UNMAPPED_PROVIDER_UNAVAILABLE,
    UNMAPPED_NUMBERING_CONFLICT,
)

#: 只有这些状态证明远端服务/协议确有故障；其余无映射一律不得写成"服务不可用"。
PROVIDER_FAILURE_STATES = frozenset({"source_unavailable", "failed"})

#: 连续编号映射的充分依据（C-004）：逐条既有映射、明确 absolute、版本化已核验
#: 偏移规则、以及 Provider 明确给出的分部边界。
MAPPING_BASIS_EXISTING = "existing_mapping"
MAPPING_BASIS_ABSOLUTE = "explicit_absolute"
MAPPING_BASIS_VERIFIED_OFFSET = "verified_season_offset"
MAPPING_BASIS_EPISODE_TITLE = "verified_episode_title"
MAPPING_BASIS_PROVIDER_BOUNDARY = "provider_season_boundary"
MAPPING_BASIS_SINGLE_PROVIDER_SEASON = "single_provider_season"

#: 这些来源的"绝对编号"只是局部编号，不构成全作品编号证据。
_UNUSABLE_ABSOLUTE_ORIGINS = frozenset({"unknown", "local_unscoped"})
_UNMAPPED_REASON_LABELS = {
    UNMAPPED_MISSING_LOCAL_NUMBER: "本地集号缺失",
    UNMAPPED_UNKNOWN_LOCAL_SEASON: "本地季号未确认",
    UNMAPPED_INSUFFICIENT_EVIDENCE: "缺少足够的编号依据",
    UNMAPPED_PROVIDER_RESOURCE_MISSING: "在线条目不存在",
    UNMAPPED_PROVIDER_UNAVAILABLE: "在线资料暂不可用",
    UNMAPPED_NUMBERING_CONFLICT: "编号证据互相冲突",
}


def _names(items: list[dict[str, Any]] | None) -> list[str]:
    return [str(item.get("name") or "").strip() for item in (items or []) if item.get("name")]


def _local_state(
    state: str,
    reason: str,
    *,
    attempted_queries: list | None = None,
    candidates: list | None = None,
    reason_code: str | None = None,
    candidate_decision: dict | None = None,
    identity_status: str = "unresolved",
    work_metadata_status: str = "unavailable",
    episode_mapping_status: str = "not_applicable",
    failure_stage: str = "",
    retryable: bool | None = None,
    mapping_scope: bool = True,
    metadata_source: str = METADATA_SOURCE_LOCAL,
    refresh_status: str = "",
    refresh_reason_code: str = "",
    metadata_refresh_error: str = "",
    artifact_status: str = ARTIFACT_STATUS_NOT_REQUIRED,
) -> dict:
    """等待/失败类结果：不再把本地 Work ID 伪装成外部 provider identity。

    ``mapping_scope=False`` 表示本轮没有进入在线编号映射范围（类型未确认、提示
    冲突等），此时剧集映射统计保持 ``not_applicable`` 与 0/0，避免把"没去映射"
    伪装成"映射失败"。
    """

    result: dict = {
        "provider": "local",
        "provider_id": "",
        "metadata_state": state,
        "reason": reason,
        "attempted_queries": attempted_queries or [],
        "identity_status": identity_status,
        "work_metadata_status": work_metadata_status,
        "episode_mapping_status": episode_mapping_status,
        "retryable": bool(
            state in {"waiting_metadata", "source_unavailable"}
            if retryable is None
            else retryable
        ),
        "metadata_source": metadata_source,
        "refresh_status": refresh_status,
        "refresh_reason_code": refresh_reason_code,
        "metadata_refresh_error": metadata_refresh_error,
        "artifact_status": artifact_status,
        "_mapping_scope": bool(mapping_scope),
    }
    if failure_stage:
        result["failure_stage"] = failure_stage
    if candidates:
        result["candidates"] = candidates
    if reason_code:
        result["reason_code"] = reason_code
    if candidate_decision is not None:
        result["candidate_decision"] = candidate_decision
    return result


def _tmdb_reason_code(error: TMDBClientError) -> str:
    """把提供方异常归一化为可恢复的业务原因码。"""

    explicit = str(getattr(error, "reason_code", "") or "").strip()
    if explicit:
        return explicit
    if isinstance(error, TMDBAuthError):
        return "provider_auth_required"
    if isinstance(error, TMDBRateLimitError):
        return "provider_rate_limited"
    status_code = int(getattr(error, "status_code", 0) or 0)
    if status_code == 404:
        return "provider_resource_missing"
    if status_code == 422:
        return "invalid_response"
    return "source_unavailable"


def _tmdb_failure_stage(error: TMDBClientError, fallback: str) -> str:
    stage = str(getattr(error, "failure_stage", "") or "").strip()
    return stage or fallback


def _tmdb_retryable(error: TMDBClientError, default: bool = True) -> bool:
    value = getattr(error, "retryable", None)
    if value is not None:
        return bool(value)
    if isinstance(error, TMDBAuthError):
        return False
    status_code = int(getattr(error, "status_code", 0) or 0)
    if status_code in {404, 422}:
        return False
    return default


def _normalize_title(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)


def _title_tokens(value: object) -> set[str]:
    """提取用于特别篇语义比对的最小词集合。

    特别篇名称常见中英文混排；先保留拉丁词，再把 CJK 字符作为单字词，
    既能处理 ``Mystery Camp`` 这类名称，也不会把两个仅共享“Camp”的
    不同标题误认为同一集。
    """

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return {
        token
        for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalized)
        if token
    }


def _special_display_title(episode: dict) -> str:
    """返回特别篇自身语义标题；纯 SP 编号和通用“特别篇”不是证据。"""

    from app.media_v4.parsing.episode_titles import is_generic_special_title, is_special_marker_only

    for raw in (episode.get("display_title"), episode.get("episode_title"), episode.get("title")):
        value = str(raw or "").strip()
        if value and not is_generic_special_title(value) and not is_special_marker_only(value):
            return value
    return ""


def _special_title_score(local_title: str, remote: dict) -> int:
    """计算本地特别篇标题与在线条目的保守语义分数。"""

    local_normalized = _normalize_title(local_title)
    if not local_normalized:
        return 0
    remote_titles = [
        str(remote.get("name") or "").strip(),
        str(remote.get("original_name") or "").strip(),
        str(remote.get("title") or "").strip(),
        str(remote.get("original_title") or "").strip(),
    ]
    best = 0
    local_tokens = _title_tokens(local_title)
    for remote_title in remote_titles:
        remote_normalized = _normalize_title(remote_title)
        if not remote_normalized:
            continue
        if local_normalized == remote_normalized:
            best = max(best, 100)
            continue
        remote_tokens = _title_tokens(remote_title)
        overlap = local_tokens & remote_tokens
        if len(overlap) >= 2:
            # 两个以上完整词重合时允许标题带副标题或标点差异；单词
            # 重合不足的标题必须经过更高的整体相似度门槛。
            ratio = SequenceMatcher(None, local_normalized, remote_normalized).ratio()
            if ratio >= 0.72:
                best = max(best, 82)
        elif len(local_normalized) >= 6 and len(remote_normalized) >= 6:
            ratio = SequenceMatcher(None, local_normalized, remote_normalized).ratio()
            if ratio >= 0.86:
                best = max(best, 72)
    return best


def _match_special_episode(
    episode: dict,
    remote_by_number: dict[int, dict],
    *,
    used_remote_numbers: set[int] | None = None,
) -> tuple[int, dict] | None:
    """按特别篇自身名称匹配在线条目，绝不按本地展示序号盲配。"""

    used = used_remote_numbers if used_remote_numbers is not None else set()
    local_title = _special_display_title(episode)
    if local_title:
        scored = sorted(
            (
                (_special_title_score(local_title, remote), number, remote)
                for number, remote in remote_by_number.items()
                if number not in used
            ),
            key=lambda item: (item[0], -item[1]),
            reverse=True,
        )
        if scored and scored[0][0] >= 82:
            best = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0
            # 相同或近似标题不能静默选第一个，避免再次制造错误绑定。
            exact_matches = [item for item in scored if item[0] == 100]
            if len(exact_matches) == 1 or (
                not exact_matches and best[0] - second_score >= 10
            ):
                return best[1], best[2]
        return None

    # 本地只有 SP01/SP02 或“特别篇”时没有足够语义证据；旧的
    # provider_episode_number 可能正是历史错误的顺序映射，不能把它当成
    # 显式确认再次复用。成功读取 Season 0 后，这类旧映射会由诊断清理。
    return None


def _candidate_summary(item: dict) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "provider": "tmdb",
        "provider_id": str(item.get("id") or ""),
        "title": item.get("name") or item.get("title") or "",
        "original_title": item.get("original_name") or item.get("original_title") or "",
        "year": int(str(item.get("first_air_date") or item.get("release_date") or "")[:4] or 0) or None,
        "media_type": "tv" if "first_air_date" in item or "name" in item else "movie",
        "popularity": _safe_float(item.get("popularity")),
    }
    if item.get("genre_ids") is not None:
        summary["genre_ids"] = list(item.get("genre_ids") or [])
    if item.get("genres") is not None:
        summary["genres"] = list(item.get("genres") or [])
    return summary


def _safe_float(value: object) -> float:
    try:
        return float(str(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _merge_search_result(results_by_id: dict[str, dict], item: dict) -> None:
    """按 Provider ID 合并多次标题查询的摘要，保留最完整的字段。"""

    provider_id = str(item.get("id") or "").strip()
    if not provider_id:
        return
    existing = results_by_id.get(provider_id)
    if existing is None:
        results_by_id[provider_id] = dict(item)
        return

    merged = dict(existing)
    for key, value in item.items():
        if key == "popularity":
            merged[key] = max(_safe_float(merged.get(key)), _safe_float(value))
        elif not merged.get(key) and value:
            merged[key] = value
    results_by_id[provider_id] = merged


def _ranked_candidate_payload(ranked: list) -> list[dict]:
    return [
        {
            "provider": item.provider,
            "provider_id": item.provider_id,
            "media_type": item.media_type,
            "title": item.title,
            "original_title": item.original_title,
            "aliases": list(item.aliases),
            "year": item.year,
            "popularity": item.popularity,
            "score": item.score,
            "reasons": list(item.reasons),
            "recommended": item.recommended,
            "identity_safe": item.identity_safe,
            "blocked": item.blocked,
        }
        for item in ranked
    ]


def _candidate_decision_payload(
    ranked: list[RankedCandidate],
    adopted: RankedCandidate | None,
    reason: str,
) -> dict:
    return {
        "decision": "auto_adopted" if adopted is not None else "waiting_review",
        "reason": reason,
        "selected_provider": adopted.provider if adopted is not None else "",
        "selected_provider_id": adopted.provider_id if adopted is not None else "",
        "selected_score": adopted.score if adopted is not None else None,
        "ranked_candidates": _ranked_candidate_payload(ranked),
    }


def _search_with_year_fallback(
    client: TMDBClient,
    query: str,
    media_type: str,
    year: int | None,
) -> list[dict]:
    """先按年份检索，空结果时回退到纯标题检索。"""

    search = client.search_tv if media_type == "tv" else client.search_movie
    results = search(query, year)
    if not results and year is not None:
        results = search(query, None)
    return results


def _rank_metadata_candidates(
    target: dict,
    media_type: str,
    titles: list[str],
    results_by_id: dict[str, dict],
    client: TMDBClient,
) -> tuple[list[RankedCandidate], RankedCandidate | None, str]:
    """把多次搜索结果统一交给 CandidateRanker 决策。"""

    candidates = enrich_candidate_aliases(
        [_candidate_summary(item) for item in results_by_id.values()],
        titles,
        max_details=8,
        client=client,
    )
    ranker = CandidateRanker()
    ranked = ranker.rank(
        {
            "preferred_title": target.get("preferred_title") or "",
            "queries": titles,
            "media_type": media_type,
            "year": target.get("year"),
            "show_type": target.get("show_type") or "",
            # 季度基名：本地是"某作品的第 N 季"时，在线条目是整部作品，
            # 标题必然不等值；基名等值 + tv 类型允许自动采用（见 `_season_base_titles`）。
            "season_base_titles": _season_base_titles(_target_titles(target)),
            "season_numbers": _local_season_numbers(target),
        },
        candidates,
    )
    adopted, reason = ranker.auto_adopt(ranked)
    return ranked, adopted, reason


def _target_titles(target: dict) -> list[str]:
    """返回已经由本地解析确认、可用于精确匹配的标题事实。

    这里不能从目录名重新猜测，也不接纳父系列名；只使用 Work 已持久化的
    首选标题、原文标题与本次 confirmed 成员提供的自身标题。这样本地化标题没有被 TMDB 搜到时，仍可用同一 Work
    的原文标题安全回退，而不会把外传吸收到父系列。
    """

    titles: list[str] = []
    seen: set[str] = set()
    for raw in (
        target.get("preferred_title"), target.get("original_title"),
        *(target.get("identity_titles") or []),
    ):
        value = str(raw or "").strip()
        normalized = _normalize_title(value)
        if value and normalized and normalized not in seen:
            titles.append(value)
            seen.add(normalized)
    return titles


#: 在线检索词上限：再多只会拖慢每部作品的刮削，收益已经归零。
_SEARCH_TITLE_LIMIT = 8


def _search_titles(target: dict) -> list[str]:
    """在 `_target_titles` 之后追加**检索变体**（发布目录前缀 / 季度标记）。

    为什么必须追加：本地目录名带着不属于作品名的组织信息，原样送给 TMDB 会
    直接返回 0 条结果——实测 `暗杀教室第二季`、`终物语第二季`、`赛马娘 第二季`、
    `K 4k Kanon`、`1.化物语`、`C 4k 吹响吧！上低音号` 全部 0 命中，而清洗后的
    基名一次命中。变体只做单向清洗，不引入任何候选侧信息（用候选标题回搜
    已被 commit fbcf790 证明会破坏身份契约，此处不予采用）。

    第一个词永远是 `_target_titles` 的首个标题，保证主标题不被挤出额度。
    """

    titles = _target_titles(target)
    seen = {_normalize_title(value) for value in titles}
    expanded: list[str] = []
    for title in titles:
        for variant in query_variants(title):
            normalized = _normalize_title(variant)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            expanded.append(variant)
    return (titles[:1] + expanded + titles[1:])[:_SEARCH_TITLE_LIMIT]


def _season_base_titles(titles: list[str]) -> list[str]:
    """季度基名：`暗杀教室第二季` → `暗杀教室`、`LoveLive! Superstar!! S2` → `LoveLive! Superstar!!`。

    在线库（TMDB）把一部作品的多季放在**一个条目**下，所以"本地某一季"与
    "在线整部作品"标题必然不同；基名等值只在本地确实带季度标记时成立。
    季号到在线季号的对应仍由 `_build_tv_episode_mappings` 按本地季号完成。
    """

    bases: list[str] = []
    seen = {_normalize_title(value) for value in titles}
    for title in titles:
        base = strip_season_marker(title)
        normalized = _normalize_title(base)
        if base == title or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        bases.append(base)
    return bases


def _local_season_numbers(target: dict) -> list[int]:
    """本地季号集合（只取正数）：用于判断"本地年份是不是某一季的播出年"。"""

    numbers: set[int] = set()
    for episode in target.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        value = _positive_int(episode.get("local_season_number"))
        if value:
            numbers.add(value)
    return sorted(numbers)


#: 旧实现用"每季尾部未映射集不超过 3 个"把缺口当作可选特别篇；C-005/CHECK-007B
#: 要求按真实计数输出 partial/unmapped，不再按数量改变内容类别或成功定义。


def _has_local_episodes(target: dict) -> bool:
    return any(
        str(item.get("episode_id") or "").strip()
        and str(item.get("episode_kind") or "regular") != "auxiliary"
        for item in target.get("episodes") or []
        if isinstance(item, dict)
    )


def _episode_key(episode: dict) -> str:
    return str(episode.get("episode_id") or "").strip()


def _is_mappable_episode(episode: object) -> bool:
    """可进入 Provider 编号映射的本地剧集：有 ID，且不是辅助内容。"""

    return isinstance(episode, dict) and bool(_episode_key(episode)) and (
        str(episode.get("episode_kind") or "regular").strip().casefold() != "auxiliary"
    )


def _absolute_number(episode: dict) -> int | None:
    """只接受有可用来源的绝对编号（C-004）。

    存在数字但来源是 ``unknown``/``local_unscoped`` 时不构成全作品编号证据，
    不能据此映射 Provider 集号。
    """

    value = _positive_int(episode.get("absolute_episode_number"))
    if not value:
        return None
    origin = str(episode.get("absolute_origin") or "").strip().casefold()
    if origin in _UNUSABLE_ABSOLUTE_ORIGINS:
        return None
    return value


def _remote_episodes_by_number(online_season: dict) -> dict[int, dict]:
    remote: dict[int, dict] = {}
    for item in (online_season or {}).get("episodes") or []:
        if not isinstance(item, dict):
            continue
        number = _positive_int(item.get("episode_number"))
        if number is None or number in remote:
            continue
        remote[number] = item
    return remote


def _normalize_verified_offsets(raw: Any) -> dict[int, dict]:
    """归一化"版本化已核验 local→Provider 季度偏移规则"。

    调用方只应传入已核验规则：``{local_season: offset}`` 或
    ``{local_season: {"offset": n, "basis": ..., "rule_version": ...}}``。
    不合法的条目被忽略而不是让整轮失败；这里不做任何推断。
    """

    if not isinstance(raw, dict):
        return {}
    rules: dict[int, dict] = {}
    for key, value in raw.items():
        season = _positive_int(key)
        if not season:
            continue
        if isinstance(value, dict):
            offset = _positive_int(value.get("offset"))
            if offset is None:
                continue
            basis = str(value.get("basis") or MAPPING_BASIS_VERIFIED_OFFSET).strip()
            if basis not in {MAPPING_BASIS_VERIFIED_OFFSET, MAPPING_BASIS_PROVIDER_BOUNDARY}:
                basis = MAPPING_BASIS_VERIFIED_OFFSET
            rule: dict[str, Any] = {
                "offset": offset,
                "basis": basis,
                "rule_version": str(value.get("rule_version") or ""),
            }
            provider_season = _positive_int(value.get("provider_season_number"))
            if provider_season:
                rule["provider_season_number"] = provider_season
            for extra in ("previous_season_total", "provider_boundary", "max_local_episode_number",
                          "episode_title_aliases", "source_url"):
                if value.get(extra) is not None:
                    rule[extra] = value.get(extra)
        else:
            offset = _positive_int(value)
            if offset is None:
                continue
            rule = {
                "offset": offset,
                "basis": MAPPING_BASIS_VERIFIED_OFFSET,
                "rule_version": "",
            }
        rules[int(season)] = rule
    return rules


def _empty_mapping_diagnostics() -> dict[str, Any]:
    return {
        "unmatched_special_episode_ids": [],
        "unmapped_episode_ids": [],
        "unmapped_reasons": [],
        "mapping_conflicts": [],
        "mapping_basis_counts": {},
    }


def _record_mapping_basis(diagnostics: dict[str, Any] | None, basis: str) -> None:
    if diagnostics is None or not basis:
        return
    counts = diagnostics.setdefault("mapping_basis_counts", {})
    counts[str(basis)] = int(counts.get(str(basis), 0)) + 1


def _record_unmapped(
    diagnostics: dict[str, Any] | None,
    episode: dict,
    reason: str,
) -> None:
    """为单个未映射 Episode 记录唯一原因（C-005/STEP-007）。"""

    if diagnostics is None:
        return
    code = reason if reason in UNMAPPED_REASONS else UNMAPPED_INSUFFICIENT_EVIDENCE
    episode_id = _episode_key(episode)
    reasons = diagnostics.setdefault("unmapped_reasons", [])
    for item in reasons:
        if isinstance(item, dict) and item.get("episode_id") == episode_id:
            return
    reasons.append({
        "episode_id": episode_id,
        "reason": code,
        "local_season_number": _positive_int(episode.get("local_season_number")),
        "local_episode_number": _positive_int(episode.get("local_episode_number")),
    })
    ids = diagnostics.setdefault("unmapped_episode_ids", [])
    if episode_id and episode_id not in ids:
        ids.append(episode_id)


def _record_conflict(diagnostics: dict[str, Any] | None, episode: dict, reason: str) -> None:
    if diagnostics is None:
        return
    conflicts = diagnostics.setdefault("mapping_conflicts", [])
    entry = {
        "episode_id": _episode_key(episode),
        "reason": reason if reason in UNMAPPED_REASONS else UNMAPPED_NUMBERING_CONFLICT,
    }
    if entry not in conflicts:
        conflicts.append(entry)


def _fallback_unmapped_reason(state: str) -> str:
    """本轮没有任何逐集原因时，按结果状态给出唯一退化原因。"""

    return (
        UNMAPPED_PROVIDER_UNAVAILABLE
        if state in PROVIDER_FAILURE_STATES or state in {"waiting_metadata", "waiting_review"}
        else UNMAPPED_INSUFFICIENT_EVIDENCE
    )


def _unmapped_local_ids(target: dict, mappings: list[dict] | None) -> list[str]:
    """当前仍未映射到在线条目的本地集 ID（只统计可映射的常规条目）。"""

    total_ids = [
        _episode_key(item)
        for item in target.get("episodes") or []
        if _is_mappable_episode(item)
    ]
    mapped_ids = {_episode_key(item) for item in mappings or [] if isinstance(item, dict) and _episode_key(item)}
    return [episode_id for episode_id in total_ids if episode_id not in mapped_ids]


def _all_unmapped_are_special(target: dict, mappings: list[dict] | None) -> bool:
    """未映射的可映射集是否全部是明确特别篇。

    特别篇缺少线上条目只是可选补全（C-002/C-005/STEP-007）；作品身份与详情已经
    成功时，不能把它当作需要处理的剧集资料缺项。数字 0 的 legacy regular 条目
    不是已证明的特别篇，因此不在这里被豁免。
    """

    unmapped = set(_unmapped_local_ids(target, mappings))
    if not unmapped:
        return False
    matched = [
        item
        for item in target.get("episodes") or []
        if isinstance(item, dict) and _episode_key(item) in unmapped
    ]
    if len(matched) != len(unmapped):
        return False
    return all(_is_special_episode(item) for item in matched)


def _unmapped_warning(target: dict, result: dict, diagnostics: dict[str, Any] | None) -> str:
    """诚实描述本地编号缺项：不说成服务故障，也不猜"多为特别篇"。"""

    unmapped = set(_unmapped_local_ids(target, result.get("episode_mappings")))
    codes = sorted({
        str(item.get("reason") or "")
        for item in (diagnostics or {}).get("unmapped_reasons") or []
        if isinstance(item, dict) and str(item.get("episode_id") or "") in unmapped
    })
    labels = "、".join(_UNMAPPED_REASON_LABELS.get(code, code) for code in codes if code)
    return (
        f"本地有 {len(unmapped)} 集没有对应的在线条目（{labels or '缺少编号依据'}）；"
        "作品资料已获取，可正常浏览与播放。"
    )


def _episode_mapping_accounting(
    target: dict,
    mappings: list[dict],
    diagnostics: dict[str, Any],
    *,
    in_scope: bool,
    fallback_reason: str,
) -> dict[str, Any]:
    """统计 mapped/total 与每个未映射集的唯一原因，推导诚实映射状态。"""

    basis_counts = {
        str(key): int(value)
        for key, value in (diagnostics.get("mapping_basis_counts") or {}).items()
    }
    total_ids = [
        _episode_key(item)
        for item in target.get("episodes") or []
        if _is_mappable_episode(item)
    ]
    mapped_ids = {_episode_key(item) for item in mappings if _episode_key(item)}
    if not in_scope or not total_ids:
        return {
            "episode_mapping_status": EPISODE_MAPPING_NOT_APPLICABLE,
            "mapped_count": 0,
            "total_count": 0,
            "unmapped_episode_ids": [],
            "unmapped_reasons": [],
            "unmapped_reason_codes": [],
            "mapping_basis_counts": {},
            "mapping_conflicts": [],
        }
    unmapped_ids = [episode_id for episode_id in total_ids if episode_id not in mapped_ids]
    unmapped_set = set(unmapped_ids)
    reported: list[dict] = [
        dict(item)
        for item in diagnostics.get("unmapped_reasons") or []
        if isinstance(item, dict) and item.get("episode_id") in unmapped_set
    ]
    seen = {str(item.get("episode_id") or "") for item in reported}
    for episode_id in unmapped_ids:
        if episode_id in seen:
            continue
        reported.append({"episode_id": episode_id, "reason": fallback_reason})
    if unmapped_ids and not mapped_ids:
        status = EPISODE_MAPPING_UNMAPPED
    elif unmapped_ids:
        status = EPISODE_MAPPING_PARTIAL
    else:
        status = EPISODE_MAPPING_COMPLETE
    return {
        "episode_mapping_status": status,
        "mapped_count": len([item for item in total_ids if item in mapped_ids]),
        "total_count": len(total_ids),
        "unmapped_episode_ids": unmapped_ids,
        "unmapped_reasons": reported,
        "unmapped_reason_codes": sorted({str(item.get("reason") or "") for item in reported if item.get("reason")}),
        "mapping_basis_counts": basis_counts,
        "mapping_conflicts": [
            dict(item) for item in diagnostics.get("mapping_conflicts") or [] if isinstance(item, dict)
        ],
    }


def finalize_metadata_statuses(target: dict, result: dict) -> dict:
    """公开入口：统一补齐 C-005 诚实状态（内部实现见 _finalize_metadata_result）。"""

    return _finalize_metadata_result(target, result)


def _finalize_metadata_result(target: dict, result: dict) -> dict:
    """把 Provider 结果补齐为 C-005 的诚实状态集合（不改变既有字段语义）。"""

    finalized = dict(result)
    diagnostic_payload = finalized.pop("_mapping_diagnostics", None)
    diagnostics = diagnostic_payload if isinstance(diagnostic_payload, dict) else {}
    mapping_scope = bool(finalized.pop("_mapping_scope", True))
    mappings = [item for item in finalized.get("episode_mappings") or [] if isinstance(item, dict)]
    state = str(finalized.get("metadata_state") or "").strip()
    provider = str(finalized.get("provider") or "").strip()
    provider_id = str(finalized.get("provider_id") or "").strip()
    has_identity = provider not in {"", "local"} and bool(provider_id)
    finalized.update(_episode_mapping_accounting(
        target,
        mappings,
        diagnostics,
        in_scope=mapping_scope,
        fallback_reason=_fallback_unmapped_reason(state),
    ))
    finalized.setdefault(
        "identity_status",
        IDENTITY_STATUS_CONFIRMED if has_identity else IDENTITY_STATUS_UNRESOLVED,
    )
    finalized.setdefault(
        "work_metadata_status",
        WORK_METADATA_READY if state == "ready" else WORK_METADATA_UNAVAILABLE,
    )
    finalized.setdefault(
        "metadata_source",
        METADATA_SOURCE_CURRENT if state == "ready" and has_identity else METADATA_SOURCE_LOCAL,
    )
    finalized.setdefault("metadata_snapshot_id", "")
    finalized.setdefault("artifact_status", ARTIFACT_STATUS_NOT_REQUIRED)
    refresh_status = str(finalized.get("refresh_status") or "").strip()
    if not refresh_status:
        if state == "ready":
            refresh_status = (
                REFRESH_STATUS_SUCCEEDED
                if finalized.get("episode_mapping_status") == EPISODE_MAPPING_COMPLETE
                else REFRESH_STATUS_PARTIAL
            )
        elif state == "waiting_metadata":
            refresh_status = REFRESH_STATUS_PENDING
        elif state in PROVIDER_FAILURE_STATES or state == "waiting_review":
            refresh_status = REFRESH_STATUS_FAILED
        else:
            refresh_status = REFRESH_STATUS_NOT_REQUESTED
    finalized["refresh_status"] = refresh_status
    refresh_reason_code = str(finalized.get("refresh_reason_code") or "").strip()
    if not refresh_reason_code and refresh_status in {
        REFRESH_STATUS_PARTIAL,
        REFRESH_STATUS_FAILED,
        REFRESH_STATUS_PENDING,
    }:
        refresh_reason_code = str(finalized.get("reason_code") or "").strip()
    finalized["refresh_reason_code"] = refresh_reason_code
    metadata_refresh_error = str(finalized.get("metadata_refresh_error") or "").strip()
    if not metadata_refresh_error and refresh_status == REFRESH_STATUS_FAILED:
        metadata_refresh_error = str(finalized.get("reason") or "").strip()
    finalized["metadata_refresh_error"] = metadata_refresh_error
    finalized.setdefault("retryable", False)
    from app.media_v4.jobs.metadata_quality import require_regular_episode_metadata

    return require_regular_episode_metadata(target.get('episodes') or [], finalized)


def _is_special_episode(episode: dict) -> bool:
    """只看明确的 content_class / episode_kind / season_kind。

    数字 0 的旧 regular 条目是历史未定位内容，不是"已证明的特别篇"：据此请求
    Provider Season 0 会把 legacy 行重新分类，也会产生无依据的在线请求。
    """

    if str(episode.get("content_class") or "").strip().casefold() in {
        "attached_special",
        "auxiliary",
    }:
        return True
    if str(episode.get("episode_kind") or "").strip().casefold() == "special":
        return True
    return str(episode.get("season_kind") or "").strip().casefold() == "special"


def _season_result_is_optional_special(item: dict, target: dict) -> bool:
    """判断季度错误是否只影响可选的特别篇。"""

    reason_code = str(item.get("reason_code") or "").strip().casefold()
    # 只有“在线条目缺失”是可选补全；认证、限流、网络或响应异常必须
    # 保持原有的可恢复故障状态，不能因为本地恰好是 Season 0 就被吞掉。
    if reason_code not in {"episode_not_found", "provider_resource_missing"}:
        return False
    if _positive_int(item.get("local_season_number")) == 0:
        return True
    season_id = str(item.get("season_id") or "").strip()
    members = [
        episode for episode in target.get("episodes") or []
        if isinstance(episode, dict)
        and (not season_id or str(episode.get("season_id") or "") == season_id)
        and _positive_int(episode.get("local_season_number")) == _positive_int(item.get("local_season_number"))
    ]
    return bool(members) and all(_is_special_episode(episode) for episode in members)


def _select_search_result(results: list[dict], target: dict, media_type: str) -> dict | None:
    target_titles = {_normalize_title(value) for value in _target_titles(target)}
    target_titles.discard("")
    exact = [
        item
        for item in results
        if target_titles
        and target_titles & {
            _normalize_title(item.get("name") or item.get("title")),
            _normalize_title(item.get("original_name") or item.get("original_title")),
        }
    ]
    target_year = int(target["year"]) if target.get("year") else None
    if target_year is not None:
        date_key = "first_air_date" if media_type == "tv" else "release_date"
        exact = [
            item
            for item in exact
            if str(item.get(date_key) or "")[:4] == str(target_year)
        ]
    unique_ids = {str(item.get("id") or "") for item in exact if item.get("id")}
    return exact[0] if len(unique_ids) == 1 else None


def _select_enriched_candidate(candidates: list[dict], target: dict, media_type: str) -> dict | None:
    """从补全别名后的候选中只接受唯一等值匹配。

    首轮搜索摘要通常只带主标题和原文标题。对于目录名使用了可信中文、日文或
    发行别名的作品，只有补充详情中的 alternative_titles / translations 后才能
    安全判定。这里仍坚持“唯一 + 等值 + 年份不冲突”，绝不把近似标题静默绑定。
    """

    target_titles = {_normalize_title(value) for value in _target_titles(target)}
    target_titles.discard("")
    if not target_titles:
        return None
    target_year = int(target["year"]) if target.get("year") else None
    matched: list[dict] = []
    for item in candidates:
        names = [
            item.get("title"),
            item.get("original_title"),
            *(item.get("aliases") or []),
        ]
        if not target_titles & {_normalize_title(name) for name in names}:
            continue
        item_year = item.get("year")
        if target_year is not None and item_year is not None and int(item_year) != target_year:
            continue
        if str(item.get("media_type") or media_type) != media_type:
            continue
        if str(item.get("provider_id") or ""):
            matched.append(item)
    unique_ids = {str(item["provider_id"]) for item in matched}
    return matched[0] if len(unique_ids) == 1 else None


def open_tmdb_client() -> TMDBClient | None:
    """按当前配置打开 TMDB 客户端；未配置 Token 时返回 None。

    一次候选搜索要跑多个 query 并补全别名。调用方必须复用一个客户端，否则每次
    请求都会重新握手 TCP/TLS，并且 `_response_cache`、连接池与限速状态全部作废。
    客户端构造集中在这里，调用方（如 `resolution.candidates`）不需要知道怎么建。
    """

    config = load_config()
    if not config.tmdb_bearer_token:
        return None
    return TMDBClient(bearer_token=config.tmdb_bearer_token)


def search_tmdb_candidates(
    query: str,
    media_type: str,
    year: int | None = None,
    *,
    client: TMDBClient | None = None,
) -> list[dict] | None:
    """手动元数据恢复：按标题搜索 TMDB 候选；未配置 Token 时返回 None。

    传入 ``client`` 时复用它（一次候选搜索会跑最多 8 个 query，逐个新建客户端等于
    每次重新握手 TCP/TLS，且 `_response_cache` 与连接池全部作废）。
    """

    config = load_config()
    if not config.tmdb_bearer_token:
        return None
    media_type = "tv" if media_type in {"tv", "series"} else "movie"
    if client is not None:
        results = _search_with_year_fallback(client, query, media_type, year)
    else:
        with TMDBClient(bearer_token=config.tmdb_bearer_token) as owned:
            # 本地年份可能来自压制目录、季度目录或发行年份，并不一定等于
            # Provider 的首播/上映年份。年份过滤无结果时必须回退到纯标题搜索，
            # 这是旧版候选链的关键兜底，否则正确作品会被误报为“搜索不到”。
            results = _search_with_year_fallback(owned, query, media_type, year)
    return [_candidate_summary(item) for item in results[:8]]


def _extract_aliases(detail: dict, media_type: str) -> list[str]:
    """从详情 alternative_titles / translations 提取可信本地化标题别名。"""

    aliases: list[str] = []
    alternative = (detail.get("alternative_titles") or {}).get("results") or []
    for item in alternative:
        title = str(item.get("title") or "").strip()
        if title:
            aliases.append(title)
    translations = (detail.get("translations") or {}).get("translations") or []
    for item in translations:
        data = item.get("data") or {}
        for raw_title in (data.get("title"), data.get("name")):
            value = str(raw_title or "").strip()
            if value:
                aliases.append(value)
    seen: list[str] = []
    for value in aliases:
        if value not in seen:
            seen.append(value)
    return seen[:12]


def enrich_candidate_aliases(
    results: list[dict],
    local_queries: list[str],
    *,
    max_details: int = 8,
    detail_cache: dict[tuple[str, str], dict[str, Any] | None] | None = None,
    detail_budget: list[int] | None = None,
    client: TMDBClient | None = None,
) -> list[dict]:
    """为候选补全可信别名：primary/original 已精确匹配的候选不请求详情；
    其余在预算内调用详情接口，失败/超预算保持无别名（不猜测 high）。"""

    cache = detail_cache if detail_cache is not None else {}
    budget = detail_budget if detail_budget is not None else [0]
    local_norms = {_normalize_title(q) for q in local_queries}
    local_norms.discard("")
    config = load_config() if client is None else None
    enriched: list[dict] = []
    for item in results:
        primary = _normalize_title(item.get("title") or item.get("name"))
        original = _normalize_title(item.get("original_title") or item.get("original_name"))
        if primary in local_norms or original in local_norms:
            enriched.append({**item, "aliases": []})
            continue
        media_type = str(item.get("media_type") or "tv")
        media_type = "tv" if media_type in {"tv", "series"} else "movie"
        provider_id = str(item.get("provider_id") or "")
        cache_key = (media_type, provider_id)
        # None 也是一次已完成请求的缓存结果：详情失败不能让同一 Provider
        # 身份在同一 draft 的其他 Work 上反复请求。
        if cache_key not in cache and budget[0] < max_details:
            budget[0] += 1
            try:
                if client is not None:
                    detail = (
                        client.get_tv_detail(int(provider_id))
                        if media_type == "tv"
                        else client.get_movie_detail(int(provider_id))
                    )
                else:
                    assert config is not None
                    with TMDBClient(bearer_token=config.tmdb_bearer_token) as detail_client:
                        detail = (
                            detail_client.get_tv_detail(int(provider_id))
                            if media_type == "tv"
                            else detail_client.get_movie_detail(int(provider_id))
                        )
            except Exception:
                detail = None
            cache[cache_key] = detail
        detail = cache.get(cache_key)
        aliases = _extract_aliases(detail, media_type) if detail else []
        enriched_item = {**item, "aliases": aliases}
        if detail:
            detail_genres = list(detail.get("genres") or [])
            if detail_genres:
                enriched_item["genres"] = detail_genres
                enriched_item["genre_ids"] = [
                    genre.get("id")
                    for genre in detail_genres
                    if isinstance(genre, dict) and genre.get("id") is not None
                ]
        enriched.append(enriched_item)
    return enriched


def default_metadata_provider(target: dict) -> dict:
    """返回带真实状态的元数据结果（C-005 诚实状态）。

    状态语义：
    - ready              已有可用的在线资料（作品资料可用，剧集映射可以只是 partial）；
    - waiting_metadata   未配置 Token 等提供方缺配，尚未获取；
    - waiting_review     搜索无唯一结果/候选歧义/类型或提示冲突，需人工确认；
    - source_unavailable 提供方网络/鉴权/限流故障；
    - failed             协议或产物错误。
    本地 Work ID 不再被包装成外部 provider identity；编号是否完整由
    ``episode_mapping_status`` 与 mapped/total 单独表达，不折算成"服务不可用"。
    """

    return _finalize_metadata_result(target, _fetch_provider_metadata(target))


def _fetch_provider_metadata(target: dict) -> dict:
    """执行一次 Provider 资料获取；只返回本轮真实得到的事实。"""

    config = load_config()
    bindings = target.get("provider_bindings") or []
    tmdb_binding = next((item for item in bindings if item.get("provider") == "tmdb"), None)
    raw_media_type = str(
        (tmdb_binding or {}).get("media_type") or target.get("work_type") or ""
    ).strip().casefold()
    media_type = raw_media_type if raw_media_type in {"tv", "series", "movie"} else ""
    if media_type == "series":
        media_type = "tv"
    saved_provider_id = str((tmdb_binding or {}).get("provider_id") or "").strip()
    if not media_type:
        # 未知类型不得自行猜测为电影，也不得据此发起任何在线查询（CHECK-007C）。
        return _local_state(
            "waiting_review",
            "本地内容类型尚未确认，已保留本地信息；确认作品类型后再获取在线资料。",
            reason_code="unknown_media_type",
            identity_status=IDENTITY_STATUS_UNRESOLVED,
            work_metadata_status=WORK_METADATA_NOT_REQUESTED,
            episode_mapping_status=EPISODE_MAPPING_NOT_APPLICABLE,
            failure_stage="classification",
            retryable=False,
            mapping_scope=False,
            refresh_status=REFRESH_STATUS_NOT_REQUESTED,
        )
    if bool(target.get("provider_hint_conflict")) and not saved_provider_id:
        # 文件名与结构化提示互相矛盾时不得自动绑定：只保留本地，不发起搜索。
        return _local_state(
            "waiting_review",
            "文件名与结构化来源给出的在线作品不一致，已保留本地信息；请人工确认作品身份。",
            reason_code="provider_hint_conflict",
            identity_status=IDENTITY_STATUS_CONFLICT,
            work_metadata_status=WORK_METADATA_NOT_REQUESTED,
            episode_mapping_status=EPISODE_MAPPING_NOT_APPLICABLE,
            failure_stage="hint_arbitration",
            retryable=False,
            mapping_scope=False,
            refresh_status=REFRESH_STATUS_NOT_REQUESTED,
        )
    if not config.tmdb_bearer_token:
        if saved_provider_id:
            return {
                "provider": "tmdb",
                "provider_id": saved_provider_id,
                "media_type": media_type,
                "title": str(target.get("preferred_title") or ""),
                "metadata_state": "waiting_metadata",
                "reason": "未配置 TMDB API Token，无法获取在线元数据；请先在设置页配置后重新刮削",
                "reason_code": "provider_auth_required",
                "episode_mappings": [],
                "identity_status": "confirmed",
                "work_metadata_status": "unavailable",
                "episode_mapping_status": "not_applicable",
                "failure_stage": "configuration",
                "retryable": True,
                "refresh_status": REFRESH_STATUS_PENDING,
                "refresh_reason_code": "provider_auth_required",
            }
        return _local_state(
            "waiting_metadata",
            "未配置 TMDB API Token，无法获取在线元数据；请先在设置页配置后重新刮削",
            reason_code="provider_auth_required",
            failure_stage="configuration",
            retryable=True,
            refresh_status=REFRESH_STATUS_PENDING,
            refresh_reason_code="provider_auth_required",
        )

    provider_id: int | None = None
    candidate_decision: dict[str, Any]

    try:
        with TMDBClient(bearer_token=config.tmdb_bearer_token) as client:
            retained = target.get('retained_work_metadata')
            if isinstance(retained, dict) and retained.get('provider') == 'tmdb' and str(retained.get('provider_id')) == saved_provider_id:
                provider_id = int(saved_provider_id)
                diagnostics = _empty_mapping_diagnostics()
                season_results: list[dict] = []
                mappings = _build_tv_episode_mappings(client, provider_id, target,
                    season_results=season_results, diagnostics=diagnostics,
                    work_detail=retained.get('provider_work_detail') or {}) if media_type == 'tv' else []
                return {**retained, 'episode_mappings': mappings, '_mapping_diagnostics': diagnostics,
                        'season_results': season_results, 'metadata_state': 'ready',
                        'refresh_status': REFRESH_STATUS_SUCCEEDED}
            if tmdb_binding:
                provider_id = int(tmdb_binding["provider_id"])
                candidate_decision = {
                    "decision": "trusted_binding",
                    "reason": "复用已确认的 TMDB 作品身份",
                    "selected_provider": "tmdb",
                    "selected_provider_id": str(provider_id),
                    "selected_score": None,
                    "ranked_candidates": [],
                }
            else:
                titles = _search_titles(target)
                attempted_queries: list[dict] = []
                results_by_id: dict[str, dict] = {}
                search_errors: list[TMDBClientError] = []
                successful_queries = 0
                for title in titles:
                    query_record = {"query": title, "year": target.get("year")}
                    try:
                        results = _search_with_year_fallback(
                            client,
                            title,
                            media_type,
                            target.get("year"),
                        )
                    except TMDBClientError as exc:
                        search_errors.append(exc)
                        query_record.update({
                            "status": "source_unavailable",
                            "error": type(exc).__name__,
                        })
                        attempted_queries.append(query_record)
                        continue
                    successful_queries += 1
                    query_record.update({"status": "completed", "result_count": len(results)})
                    attempted_queries.append(query_record)
                    for item in results:
                        _merge_search_result(results_by_id, item)

                if search_errors and successful_queries == 0 and titles:
                    return _local_state(
                        "source_unavailable",
                        f"在线资料服务暂不可用，请稍后重试（{type(search_errors[-1]).__name__}）",
                        attempted_queries=attempted_queries,
                        reason_code=_tmdb_reason_code(search_errors[-1]),
                        failure_stage=_tmdb_failure_stage(search_errors[-1], "search"),
                        retryable=_tmdb_retryable(search_errors[-1]),
                    )

                # 主标题、原文标题及别名查询的所有摘要统一进入评分器。即使
                # 首个查询已经出现完整标题，也不能提前采用，否则会丢失后续
                # 查询的更可信候选及热度排序信号。
                ranked, adopted, adopt_reason = _rank_metadata_candidates(
                    target,
                    media_type,
                    titles,
                    results_by_id,
                    client,
                )
                candidate_decision = _candidate_decision_payload(ranked, adopted, adopt_reason)
                if adopted is None:
                    ranked_payload = _ranked_candidate_payload(ranked)
                    review_reason = (
                        "在线资料中没有找到候选作品，请检查作品标题后重试"
                        if not ranked
                        else (
                            # "候选分"不是匹配百分比（类型/动画域会先给基础分，名称可能得 0），
                            # 而且只有 1 个候选时也不能说成"多个可能作品"。
                            "只找到 1 个候选作品，但缺少可信的同名或别名证据，需要人工确认"
                            if len(ranked) == 1
                            else "在线作品候选不足以自动确认，需要人工确认后再继续"
                        )
                    )
                    return _local_state(
                        "waiting_review",
                        review_reason,
                        attempted_queries=attempted_queries,
                        candidates=ranked_payload,
                        reason_code="no_candidates" if not ranked else "ambiguous_candidates",
                        candidate_decision=candidate_decision,
                        failure_stage="search",
                        retryable=False,
                        mapping_scope=False,
                        refresh_status=REFRESH_STATUS_FAILED,
                        refresh_reason_code="no_candidates" if not ranked else "ambiguous_candidates",
                    )
                provider_id = int(adopted.provider_id)
            detail = client.get_tv_detail(provider_id) if media_type == "tv" else client.get_movie_detail(provider_id)
            images = detail.get("images") or {}
            poster = client.select_best_poster(images) or detail.get("poster_path") or ""
            fanart = client.select_best_backdrop(images) or detail.get("backdrop_path") or ""
            clearlogo = client.select_best_logo(images) or ""
            date = detail.get("first_air_date") if media_type == "tv" else detail.get("release_date")
            runtimes = detail.get("episode_run_time") or [detail.get("runtime") or 0]
            result: dict[str, Any] = {
                "provider": "tmdb",
                "provider_id": str(provider_id),
                "media_type": media_type,
                "title": detail.get("name") or detail.get("title") or target.get("preferred_title") or "",
                "original_title": detail.get("original_name") or detail.get("original_title") or "",
                "year": int(str(date)[:4]) if date and str(date)[:4].isdigit() else target.get("year"),
                "plot": detail.get("overview") or "",
                "rating": float(detail.get("vote_average") or 0),
                "genres": _names(detail.get("genres")),
                "studios": _names(detail.get("networks") or detail.get("production_companies")),
                "premiered": date or "",
                "runtime": int(runtimes[0] or 0),
                "poster_url": client.build_image_url(poster, "w780") if poster else "",
                "fanart_url": client.build_image_url(fanart, "original") if fanart else "",
                "poster_file_path": poster,
                "fanart_file_path": fanart,
                "clearlogo_url": client.build_image_url(clearlogo, "original") if clearlogo else "",
                "clearlogo_file_path": clearlogo,
                "metadata_state": "ready",
                "candidate_decision": candidate_decision,
                "provider_work_detail": {'seasons': detail.get('seasons') or []},
            }
            if media_type == "tv":
                season_results = []
                mapping_diagnostics: dict[str, Any] = _empty_mapping_diagnostics()
                result["episode_mappings"] = _build_tv_episode_mappings(
                    client,
                    provider_id,
                    target,
                    season_results=season_results,
                    diagnostics=mapping_diagnostics,
                    work_detail=detail,
                )
                result["_mapping_diagnostics"] = mapping_diagnostics
                mapped_episode_ids = {
                    str(item.get("episode_id") or "")
                    for item in result["episode_mappings"]
                    if str(item.get("episode_id") or "").strip()
                }
                # 已经有逐集原因的集（缺编号依据、编号冲突等）不再补一条
                # “在线集数未匹配”的季度错因，否则原因会被误报。
                explained_episode_ids = {
                    str(item.get("episode_id") or "")
                    for item in mapping_diagnostics["unmapped_reasons"]
                }
                unmapped_by_season: dict[tuple[str, int | None], list[dict]] = {}
                for episode in target.get("episodes") or []:
                    if not isinstance(episode, dict) or str(episode.get("episode_kind") or "regular") == "auxiliary":
                        continue
                    episode_id = str(episode.get("episode_id") or "")
                    if not episode_id or episode_id in mapped_episode_ids:
                        continue
                    if episode_id in explained_episode_ids:
                        continue
                    key = (
                        str(episode.get("season_id") or ""),
                        _positive_int(episode.get("local_season_number")),
                    )
                    unmapped_by_season.setdefault(key, []).append(episode)
                recorded_seasons = {
                    (
                        str(item.get("season_id") or ""),
                        _positive_int(item.get("local_season_number")),
                    )
                    for item in season_results
                }
                for (season_id, local_season_number), _episodes in unmapped_by_season.items():
                    if (season_id, local_season_number) in recorded_seasons:
                        continue
                    provider_season_number = _positive_int(_episodes[0].get("provider_season_number"))
                    if provider_season_number is None:
                        provider_season_number = local_season_number
                    season_results.append({
                        "season_id": season_id,
                        "local_season_number": local_season_number,
                        "provider_season_number": provider_season_number,
                        "status": "partial",
                        "reason_code": "episode_not_found",
                        "failure_stage": "season_detail",
                        "retryable": False,
                    })
                critical_season_results = [
                    item for item in season_results
                    if not _season_result_is_optional_special(item, target)
                ]
                critical_unmapped_episodes = [
                    episode for episodes in unmapped_by_season.values()
                    for episode in episodes
                    if not _is_special_episode(episode)
                ]
                if season_results and not critical_season_results and not critical_unmapped_episodes:
                    # 作品身份和详情已经成功，特别篇缺少线上条目只是可选
                    # 补全，不得把整部作品降级成“服务不可用”。
                    result.update({
                        "metadata_state": "ready",
                        "metadata_warning": "部分特别篇没有对应的在线资料，已保留本地文件名称，不影响播放。",
                        "reason_code": "special_episode_metadata_incomplete",
                        "season_results": season_results,
                        "identity_status": "confirmed",
                        "work_metadata_status": "ready",
                        "episode_mapping_status": "partial",
                        "failure_stage": "season_detail",
                        "retryable": False,
                    })
                elif season_results:
                    # 必须区分两件完全不同的事（否则 20 部作品被误报成“服务不可用”）：
                    #   1) 季度请求本身失败/超时 → 真的是在线服务不可用，要重试；
                    #   2) 季度读取成功，但本地集号与在线条目对不上（本地多出特别篇/
                    #      番外，或整部作品的集号被连续编号）→ 作品资料其实已经完整，
                    #      重试一百次也不会变。
                    # 实测（2026-09-24，186 部作品库）：20 部 episode_mapping_incomplete
                    # 全部属于第 2 类，用户界面上却全部显示“在线资料服务暂不可用”。
                    service_outage = any(
                        str(item.get("status") or "") == "source_unavailable"
                        for item in season_results
                    )
                    if service_outage:
                        result.update({
                            "metadata_state": "source_unavailable",
                            "reason": "部分剧集资料暂不可用，已保留作品信息，可稍后重试",
                            "reason_code": "episode_mapping_incomplete",
                            "season_results": season_results,
                            "identity_status": "confirmed",
                            "work_metadata_status": "ready",
                            "episode_mapping_status": "partial",
                            "failure_stage": "season_detail",
                            "retryable": any(item.get("retryable") for item in season_results),
                        })
                    else:
                        # 在线资料已获取，只是有集号没有对应条目或缺少编号依据：
                        # 不按缺项数量改变内容类别，也不改变作品资料的成功定义。
                        result.update({
                            "metadata_state": "ready",
                            "metadata_warning": _unmapped_warning(target, result, mapping_diagnostics),
                            "reason_code": "episode_mapping_incomplete",
                            "season_results": season_results,
                            "identity_status": "confirmed",
                            "work_metadata_status": "ready",
                            "failure_stage": "season_detail",
                            "retryable": False,
                        })
                else:
                    if _all_unmapped_are_special(target, result["episode_mappings"]):
                        # 只有一个明确特别篇未映射时同样只是可选补全：作品资料可用，
                        # 不降级成待处理缺项，也不出现“服务不可用”。
                        result.update({
                            "metadata_state": "ready",
                            "metadata_warning": "部分特别篇没有对应的在线资料，已保留本地文件名称，不影响播放。",
                            "reason_code": "special_episode_metadata_incomplete",
                            "identity_status": "confirmed",
                            "work_metadata_status": "ready",
                            "failure_stage": "season_detail",
                            "retryable": False,
                        })
                    elif _unmapped_local_ids(target, result["episode_mappings"]):
                        # 在线资料与编号映射流程都已完成，只是有集缺少编号依据或没有
                        # 对应条目（例如只知道本地 S2E1 而在线只编了一季）：输出诚实的
                        # partial/unmapped 计数，不折算成服务故障。
                        result.update({
                            "metadata_state": "ready",
                            "metadata_warning": _unmapped_warning(target, result, mapping_diagnostics),
                            "reason_code": "episode_mapping_incomplete",
                            "identity_status": "confirmed",
                            "work_metadata_status": "ready",
                            "retryable": False,
                        })
                    else:
                        result.update({
                            "identity_status": "confirmed",
                            "work_metadata_status": "ready",
                            "retryable": False,
                        })
                if mapping_diagnostics["unmatched_special_episode_ids"]:
                    # 包含成功读取但名称不匹配，以及明确不存在的特别篇季度。
                    # scrape 阶段据此清掉旧顺序映射，临时网络失败不误删。
                    result["clear_episode_mapping_ids"] = list(dict.fromkeys(
                        mapping_diagnostics["unmatched_special_episode_ids"]
                    ))
            else:
                result.update({
                    "identity_status": "confirmed",
                    "work_metadata_status": "ready",
                    "episode_mapping_status": "not_applicable",
                    "retryable": False,
                })
            return result
    except TMDBClientError as exc:
        if provider_id is not None:
            # 已经确认过的 Provider 身份不能因为一次详情/季度请求失败
            # 被降级成 local；保留标题和 ID，下一次重试可直接复用。
            return {
                "provider": "tmdb",
                "provider_id": str(provider_id),
                "media_type": media_type,
                "title": str(target.get("preferred_title") or ""),
                "original_title": str(target.get("original_title") or ""),
                "metadata_state": "source_unavailable",
                "reason": f"在线资料服务暂不可用，请稍后重试（{type(exc).__name__}）",
                "reason_code": _tmdb_reason_code(exc),
                "episode_mappings": [],
                "identity_status": "confirmed",
                "work_metadata_status": "unavailable",
                "episode_mapping_status": "not_applicable",
                "failure_stage": _tmdb_failure_stage(exc, "work_detail"),
                "retryable": _tmdb_retryable(exc),
            }
        return _local_state(
            "source_unavailable",
            f"在线资料服务暂不可用，请稍后重试（{type(exc).__name__}）",
            reason_code=_tmdb_reason_code(exc),
            failure_stage=_tmdb_failure_stage(exc, "search"),
            retryable=_tmdb_retryable(exc),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if provider_id is not None:
            return {
                "provider": "tmdb",
                "provider_id": str(provider_id),
                "media_type": media_type,
                "title": str(target.get("preferred_title") or ""),
                "original_title": str(target.get("original_title") or ""),
                "metadata_state": "failed",
                "reason": f"在线资料返回内容无效，请稍后重试（{type(exc).__name__}）",
                "reason_code": "invalid_response",
                "episode_mappings": [],
                "identity_status": "confirmed",
                "work_metadata_status": "unavailable",
                "episode_mapping_status": "not_applicable",
                "failure_stage": "work_detail",
                "retryable": False,
            }
        return _local_state(
            "failed",
            f"获取在线元数据失败: {type(exc).__name__}",
            reason_code="invalid_response",
            failure_stage="work_detail",
            retryable=False,
        )


def _build_tv_episode_mappings(
    client: TMDBClient,
    provider_id: int,
    target: dict,
    *,
    season_results: list[dict] | None = None,
    diagnostics: dict[str, Any] | None = None,
    work_detail: dict | None = None,
) -> list[dict]:
    """把已确认的本地 Episode 映射到 TMDB 的标题与剧照。

    本地季度和集号永远不在这里改写；这里只保存 provider 的映射和可展示的远端
    字段。连续编号只接受 C-004 的充分依据（逐条既有映射、明确 absolute、版本化
    已核验偏移规则），不再按文件数量或播出间隔推断偏移；证据不足的集保留本地
    编号并记录唯一原因，季度请求的 404 与真实服务故障分别记录。
    """

    local_episodes = [item for item in target.get("episodes") or [] if _is_mappable_episode(item)]
    season_cache: dict[int, dict] = {}
    season_errors: dict[int, dict] = {}
    reported_seasons: set[int] = set()
    provider_regular_seasons = sorted({
        int(item["season_number"])
        for item in (work_detail or {}).get("seasons") or []
        if isinstance(item, dict) and _positive_int(item.get("season_number"))
    })
    verified_offsets = _normalize_verified_offsets(target.get("verified_season_offsets"))

    def fetch(provider_season: int) -> dict | None:
        """按 Provider 季号读取并缓存季详情；失败只记录一次错误分类。"""

        if provider_season in season_cache:
            return season_cache[provider_season]
        if provider_season in season_errors:
            return None
        try:
            season = client.get_tv_season_episodes(provider_id, provider_season)
        except TMDBClientError as exc:
            # 404 已明确该季在线不存在，与网络/认证/限流故障不同。
            season_errors[provider_season] = {
                "reason_code": _tmdb_reason_code(exc),
                "failure_stage": _tmdb_failure_stage(exc, "season_detail"),
                "retryable": _tmdb_retryable(exc),
                "verified_missing": _tmdb_reason_code(exc) == "provider_resource_missing",
            }
            return None
        season_cache[provider_season] = season if isinstance(season, dict) else {"episodes": []}
        return season_cache[provider_season]

    # 在线只有一季、本地却分了多季：只能按充分依据逐条映射，绝不按集数推断偏移。
    continuous_scope = provider_regular_seasons == [1] and any(
        not _is_special_episode(item)
        and (_positive_int(item.get("local_season_number")) or 0) > 1
        for item in local_episodes
    )
    continuous_unmapped_ids: set[str] = set()
    if continuous_scope:
        online_season = fetch(1)
        if online_season is not None:
            # 规则属于 Provider 编号层，不写回 confirmed revision。显式已核验规则优先。
            catalog_offsets = verified_tmdb_season_offsets(provider_id)
            if any(item.get("local_season_number") == 1 and not _is_special_episode(item)
                   for item in local_episodes):
                # S01 正片已存在时，不把同作品 S02 标签例外也塞进 S01 的同一集。
                catalog_offsets = {s: r for s, r in catalog_offsets.items()
                                   if not r.get("episode_title_aliases")}
            verified_offsets = {**catalog_offsets, **verified_offsets}
            local_episodes = _map_continuous_season(
                local_episodes,
                online_season,
                verified_offsets=verified_offsets,
                provider_season_number=1,
                diagnostics=diagnostics,
            )
            for item in local_episodes:
                episode_id = _episode_key(item)
                if (
                    episode_id
                    and item.get("unmapped_reason")
                    and _positive_int(item.get("provider_episode_number")) is None
                ):
                    continuous_unmapped_ids.add(episode_id)

    # 本地全部是未分季正片、在线只有一个常规季时，允许在该唯一季的命名空间映射；
    # 本地 null 季号不变，只记录 mapping_basis=single_provider_season。
    single_season_scope = (
        provider_regular_seasons == [1]
        and bool(local_episodes)
        and any(not _is_special_episode(item) for item in local_episodes)
        and all(
            _is_special_episode(item) or _positive_int(item.get("local_season_number")) is None
            for item in local_episodes
        )
    )
    if single_season_scope:
        local_numbers = [
            _positive_int(item.get("local_episode_number"))
            for item in local_episodes
            if not _is_special_episode(item) and _positive_int(item.get("provider_episode_number")) is None
        ]
        numbers = [number for number in local_numbers if number]
        if len(numbers) == len(local_numbers) and len(set(numbers)) == len(numbers):
            online_season = fetch(1)
            if online_season is not None:
                remote_numbers = _remote_episodes_by_number(online_season)
                remapped: list[dict] = []
                for item in local_episodes:
                    copy = dict(item)
                    number = _positive_int(item.get("local_episode_number"))
                    existing_season = _positive_int(item.get("provider_season_number"))
                    if (
                        not _is_special_episode(item)
                        and _positive_int(item.get("provider_episode_number")) is None
                        and number
                        and number in remote_numbers
                        and existing_season in {None, 1}
                    ):
                        copy["provider_season_number"] = 1
                        copy["provider_episode_number"] = number
                        copy["mapping_basis"] = MAPPING_BASIS_SINGLE_PROVIDER_SEASON
                        copy["mapping_evidence"] = {
                            "provider_season_number": 1,
                            "local_episode_number": number,
                            "rule_version": "single_provider_season",
                        }
                        copy.pop("unmapped_reason", None)
                        _record_mapping_basis(diagnostics, MAPPING_BASIS_SINGLE_PROVIDER_SEASON)
                    remapped.append(copy)
                local_episodes = remapped

    by_provider_season: dict[int, list[dict]] = {}
    for episode in local_episodes:
        episode_id = _episode_key(episode)
        if episode_id in continuous_unmapped_ids:
            # 已知在线只有一季：不再去请求本地季号对应的在线季，按真实原因记录缺项。
            _record_unmapped(
                diagnostics,
                episode,
                str(episode.get("unmapped_reason") or UNMAPPED_INSUFFICIENT_EVIDENCE),
            )
            continue
        if _is_special_episode(episode):
            # TMDB 的特别篇身份固定在 Season 0。本地旧映射可能把 SP 的 provider
            # season 写成正片季号，但它不能继续决定本次请求的季度。
            by_provider_season.setdefault(0, []).append(episode)
            continue
        provider_season = _positive_int(episode.get("provider_season_number"))
        if provider_season is None or provider_season < 1:
            provider_season = _positive_int(episode.get("local_season_number"))
        if provider_season is None or provider_season < 1:
            # 数字 0 的旧 regular 条目是历史未定位内容：不请求 Season 0，也不补造季号。
            _record_unmapped(
                diagnostics,
                episode,
                UNMAPPED_MISSING_LOCAL_NUMBER
                if _positive_int(episode.get("local_episode_number")) is None
                else UNMAPPED_UNKNOWN_LOCAL_SEASON,
            )
            continue
        by_provider_season.setdefault(provider_season, []).append(episode)

    result: list[dict] = []
    for provider_season, episodes in sorted(by_provider_season.items()):
        season_mapping_verified = False
        used_remote_numbers: set[int] = set()
        remote_by_number: dict[int, dict] = {}
        failure_reason: str | None = None
        season = fetch(provider_season)
        if season is None:
            error = season_errors.get(provider_season) or {}
            reason_code = str(error.get("reason_code") or "source_unavailable")
            season_mapping_verified = bool(error.get("verified_missing"))
            failure_reason = (
                UNMAPPED_PROVIDER_RESOURCE_MISSING
                if season_mapping_verified
                else UNMAPPED_PROVIDER_UNAVAILABLE
            )
            if season_results is not None and provider_season not in reported_seasons:
                reported_seasons.add(provider_season)
                season_results.append({
                    "season_id": str(episodes[0].get("season_id") or ""),
                    "local_season_number": _positive_int(episodes[0].get("local_season_number")),
                    "provider_season_number": provider_season,
                    # 服务/协议故障才是 source_unavailable；404 明确记 provider_resource_missing。
                    "status": "source_unavailable" if not season_mapping_verified else "provider_resource_missing",
                    "reason_code": reason_code,
                    "failure_stage": str(error.get("failure_stage") or "season_detail"),
                    "retryable": bool(error.get("retryable")),
                })
        else:
            remote_by_number = _remote_episodes_by_number(season)
            season_mapping_verified = True

        for episode in episodes:
            provider_episode: int | None
            if _is_special_episode(episode):
                matched = _match_special_episode(
                    episode,
                    remote_by_number,
                    used_remote_numbers=used_remote_numbers,
                )
                if matched is None:
                    if season_mapping_verified and diagnostics is not None:
                        diagnostics.setdefault("unmatched_special_episode_ids", []).append(
                            str(episode["episode_id"])
                        )
                    _record_unmapped(
                        diagnostics,
                        episode,
                        failure_reason or UNMAPPED_PROVIDER_RESOURCE_MISSING,
                    )
                    continue
                provider_episode, remote = matched
                used_remote_numbers.add(provider_episode)
            else:
                provider_episode = _positive_int(episode.get("provider_episode_number"))
                if provider_episode is None:
                    # 明确季号下"本地集号即 Provider 集号"是既有约定（C-004：Provider
                    # 在自己命名空间映射本地编号）；这里不改变本地值，只做命名空间查找。
                    provider_episode = _positive_int(episode.get("local_episode_number"))
                if provider_episode is None:
                    _record_unmapped(diagnostics, episode, UNMAPPED_MISSING_LOCAL_NUMBER)
                    continue
                remote = remote_by_number.get(provider_episode) or {}
            if not str(remote.get("id") or "").strip():
                # 没有远端 Episode ID 时不能把空映射写成"已映射"；保留本地剧集，
                # 并记录真实原因（服务故障/该条目不存在），不伪装成完整刮削。
                if _is_special_episode(episode) and season_mapping_verified and diagnostics is not None:
                    diagnostics.setdefault("unmatched_special_episode_ids", []).append(
                        str(episode["episode_id"])
                    )
                _record_unmapped(
                    diagnostics,
                    episode,
                    failure_reason or UNMAPPED_PROVIDER_RESOURCE_MISSING,
                )
                continue
            still = str(remote.get("still_path") or "")
            mapping = {
                "episode_id": str(episode["episode_id"]),
                "provider_season_number": provider_season,
                "provider_episode_number": provider_episode,
                "provider_episode_id": str(remote.get("id") or ""),
                "title": str(remote.get("name") or ""),
                "plot": str(remote.get("overview") or ""),
                "runtime": _positive_int(remote.get("runtime")),
                # 详情页横向卡片至少需要 w500；w300 在高 DPI 下既模糊又会被前端再次改写。
                "still_url": client.build_image_url(still, "w500") if still else "",
            }
            result.append(mapping)
            basis = str(episode.get("mapping_basis") or "")
            if basis:
                mapping["mapping_basis"] = basis
                mapping["mapping_evidence"] = dict(episode.get("mapping_evidence") or {})
                _record_mapping_basis(diagnostics, basis)
    return result


def _map_continuous_season(
    episodes: list[dict],
    online_season: dict,
    *,
    verified_offsets: Any = None,
    provider_season_number: int = 1,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict]:
    """只生成线上编号副本，不改本地分季，也不推断偏移（C-004）。

    充分依据包括本集已有的适用逐条 Provider mapping、明确 absolute 来源且
    在线该集存在、带版本的已核验偏移规则、唯一一致的分集标题。文件数量、季内最大
    集号、播出间隔都不再参与推断——证据不足就返回原编号副本并记录
    ``unmapped_reason=insufficient_numbering_evidence``，不抛错、不补 0、不猜 S1E1。

    返回的是新 dict：映射成功的副本带 ``mapping_basis``/``mapping_evidence``，未映射
    的常规副本带 ``unmapped_reason``；特别篇原样返回，由标题匹配负责。
    """

    remote = _remote_episodes_by_number(online_season)
    rules = _normalize_verified_offsets(verified_offsets)
    title_numbers: dict[str, list[int]] = {}
    local_title_counts: dict[str, int] = {}
    for item in episodes:
        if not _is_special_episode(item):
            title = normalized_episode_title(item.get("display_title"))
            if title:
                local_title_counts[title] = local_title_counts.get(title, 0) + 1
    for remote_number, item in remote.items():
        title = normalized_episode_title(item.get("name"))
        if title and str(item.get("id") or "").strip():
            title_numbers.setdefault(title, []).append(remote_number)
    mapped: list[dict] = []
    for episode in episodes:
        copy = dict(episode)
        if _is_special_episode(episode):
            mapped.append(copy)
            continue
        season = _positive_int(episode.get("local_season_number"))
        number = _positive_int(episode.get("local_episode_number"))
        absolute = _absolute_number(episode)
        existing_season = _positive_int(episode.get("provider_season_number"))
        existing_number = _positive_int(episode.get("provider_episode_number"))

        if existing_season == provider_season_number and existing_number:
            # 1) 已有适用逐条映射优先；与明确 absolute 相斥时只记录冲突，不覆盖稳定映射。
            copy["mapping_basis"] = MAPPING_BASIS_EXISTING
            copy["mapping_evidence"] = {
                "provider_season_number": existing_season,
                "provider_episode_number": existing_number,
            }
            if absolute is not None and absolute != existing_number:
                copy["mapping_conflict"] = UNMAPPED_NUMBERING_CONFLICT
                _record_conflict(diagnostics, episode, UNMAPPED_NUMBERING_CONFLICT)
            mapped.append(copy)
            continue

        target_number: int | None = None
        basis: str | None = None
        evidence: dict[str, Any] = {}
        reason: str | None = None
        if absolute is not None:
            # 2) 明确 absolute 来源且在线该集存在。
            if absolute in remote:
                target_number, basis = absolute, MAPPING_BASIS_ABSOLUTE
                evidence = {"absolute_episode_number": absolute}
            else:
                reason = UNMAPPED_PROVIDER_RESOURCE_MISSING
        if target_number is None and season and number and season in rules:
            # 3) 带版本的已核验 local→Provider 偏移规则（允许季号缺口）。
            rule = rules[season]
            candidate = int(rule["offset"]) + number
            maximum = _positive_int(rule.get("max_local_episode_number"))
            aliases = rule.get("episode_title_aliases")
            local_title = normalized_episode_title(episode.get("display_title"))
            title_matches = title_numbers.get(local_title, [])
            title_verified = True
            if isinstance(aliases, dict):
                title = normalized_episode_title(episode.get("display_title"))
                alias = normalized_episode_title(aliases.get(number) or aliases.get(str(number)))
                online_title = normalized_episode_title((remote.get(candidate) or {}).get("name"))
                title_verified = bool(title and title in {alias, online_title})
            if (maximum is not None and number > maximum) or not title_verified:
                reason = UNMAPPED_INSUFFICIENT_EVIDENCE
            elif len(title_matches) == 1 and title_matches[0] != candidate:
                reason = UNMAPPED_NUMBERING_CONFLICT
            elif absolute is not None and absolute != candidate:
                reason = UNMAPPED_NUMBERING_CONFLICT
            elif candidate in remote:
                target_number = candidate
                basis = str(rule.get("basis") or MAPPING_BASIS_VERIFIED_OFFSET)
                evidence = {key: value for key, value in rule.items() if key != "episode_title_aliases"}
                if isinstance(aliases, dict):
                    evidence["matched_title"] = title
            elif reason is None:
                reason = UNMAPPED_PROVIDER_RESOURCE_MISSING
        if (target_number is None and absolute is None and season and season not in rules
                and existing_number is None and existing_season in {None, provider_season_number}):
            # 本地季号不同时，可用唯一分集标题直接定位在线集；同季明确编号仍优先。
            title = normalized_episode_title(episode.get("display_title"))
            matches = title_numbers.get(title, [])
            if (len(matches) == 1 and local_title_counts.get(title) == 1
                    and (season != provider_season_number or number == matches[0])):
                target_number, basis = matches[0], MAPPING_BASIS_EPISODE_TITLE
                evidence = {'rule_version': 'unique_episode_title-v1',
                            'provider_episode_number': target_number, 'matched_title': title}
        if (target_number is None and absolute is None and season == provider_season_number
                and season not in rules and number in remote and existing_season in {None, provider_season_number}
                and existing_number is None):
            # 同季同集的命名空间对应不需要跨季偏移；后续季仍须独立编号证据。
            target_number = number
            basis = MAPPING_BASIS_SINGLE_PROVIDER_SEASON
            evidence = {'local_season_number': season, 'local_episode_number': number,
                        'provider_season_number': provider_season_number, 'rule_version': 'same_season_number'}
        if target_number is not None:
            copy["provider_season_number"] = provider_season_number
            copy["provider_episode_number"] = target_number
            copy["mapping_basis"] = basis
            copy["mapping_evidence"] = evidence
            copy.pop("unmapped_reason", None)
        else:
            if reason is None:
                if not season:
                    reason = UNMAPPED_UNKNOWN_LOCAL_SEASON
                elif not number:
                    reason = UNMAPPED_MISSING_LOCAL_NUMBER
                else:
                    # 只有后续季、缺集、文件顺序或相邻连续数字都不证明偏移。
                    reason = UNMAPPED_INSUFFICIENT_EVIDENCE
            copy["unmapped_reason"] = reason
            _record_unmapped(diagnostics, episode, reason)
        mapped.append(copy)
    # 唯一标题/规则产生的新对应不能占用另一个本地 Episode 已使用的在线集。
    # 同一集的多 Asset 在图里共享 Episode ID，不会被误判为这里的冲突。
    claims: dict[tuple[int, int], set[str]] = {}
    for item in mapped:
        if _is_special_episode(item):
            continue
        provider_season = _positive_int(item.get("provider_season_number"))
        provider_number = _positive_int(item.get("provider_episode_number"))
        if provider_season and provider_number:
            claims.setdefault((provider_season, provider_number), set()).add(_episode_key(item))
    for item in mapped:
        claim = (item.get("provider_season_number"), item.get("provider_episode_number"))
        if (len(claims.get(claim, set())) > 1
                and item.get("mapping_basis") in {MAPPING_BASIS_EPISODE_TITLE, MAPPING_BASIS_VERIFIED_OFFSET,
                                                 MAPPING_BASIS_PROVIDER_BOUNDARY}):
            item.pop("provider_season_number", None)
            item.pop("provider_episode_number", None)
            item.pop("mapping_basis", None)
            item.pop("mapping_evidence", None)
            item["unmapped_reason"] = UNMAPPED_NUMBERING_CONFLICT
            _record_unmapped(diagnostics, item, UNMAPPED_NUMBERING_CONFLICT)
    return mapped


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
