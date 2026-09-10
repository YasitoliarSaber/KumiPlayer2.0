"""V4 作品身份的边界和 Provider 合并策略。

解析器只产生事实；本模块只根据已解析的 Work 边界判断哪些 Provider
身份可以用于跨语言/跨来源合并，避免把父系列关系误当成同一作品。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.media_v4.domain.models import ResolutionIssue, ResolvedMediaGraph, ResolvedWork

if TYPE_CHECKING:
    from app.media_v4.resolution.candidates import WorkCandidate


_INDEPENDENT_RELATIONS = frozenset({
    "spin_off",
    "movie",
    "remake",
    "prequel",
    "sequel",
})


def _boundary_key(value: str) -> tuple[str, str, str] | None:
    """把历史 identity_key 的不同编码归一到作品边界。"""

    text = value.strip()
    if text.startswith("series:"):
        body = text[len("series:"):]
        try:
            title, media_type = body.rsplit(":", 1)
        except ValueError:
            return None
        return "series", title, media_type
    if text.startswith("work:"):
        body = text[len("work:"):]
        try:
            title, media_type = body.rsplit(":", 1)
        except ValueError:
            return None
        return "work", title, media_type
    if text.startswith("title:"):
        body = text[len("title:"):]
        try:
            title, media_type = body.rsplit(":", 1)
        except ValueError:
            return None
        return "work", title.split(":", 1)[0], media_type
    return None


@dataclass(frozen=True, slots=True)
class IdentityDecision:
    """一个旧身份对当前 Work 是否仍然可复用的决定。"""

    decision: str
    work_key: str
    provider: str = ""
    media_type: str = ""
    provider_id: str = ""
    evidence_ids: tuple[str, ...] = ()
    reason_code: str = ""


def is_independent_work(work: ResolvedWork) -> bool:
    """明确的外传/电影边界不能仅按父系列 Provider 身份合并。"""

    return (
        work.card_type in {"standalone", "movie"}
        or work.relation_type in _INDEPENDENT_RELATIONS
    )


def provider_identity_conflicts(
    graph: ResolvedMediaGraph,
    candidates_by_key: dict[str, list[WorkCandidate]],
) -> tuple[set[tuple[str, str, str]], list[ResolutionIssue]]:
    """找出同一 Provider ID 横跨独立作品边界的候选身份。

    同一主系列的多语言标题仍允许合并；只有图中存在明确独立关系时才
    阻断。旧绑定候选会被降为 rejected，避免后续确认阶段再次冻结它。
    """

    members: dict[tuple[str, str, str], list[tuple[ResolvedWork, WorkCandidate]]] = {}
    for work in graph.works:
        for candidate in candidates_by_key.get(work.work_key, []):
            if candidate.status != "confirmed":
                continue
            identity = (candidate.provider, candidate.media_type, candidate.provider_id)
            members.setdefault(identity, []).append((work, candidate))

    blocked: set[tuple[str, str, str]] = set()
    issues: list[ResolutionIssue] = []
    for identity, identity_members in members.items():
        work_keys = {work.work_key for work, _candidate in identity_members}
        if len(work_keys) < 2:
            continue
        independent = [work for work, _candidate in identity_members if is_independent_work(work)]
        if not independent:
            continue
        blocked.add(identity)
        evidence_id = next(
            (
                evidence_id
                for work, _candidate in identity_members
                for evidence_id in work.source_evidence_ids
            ),
            "",
        )
        issues.append(
            ResolutionIssue(
                code="work_identity_conflict",
                evidence_id=evidence_id,
                message=(
                    f"Provider 身份 {identity[0]}:{identity[2]} 同时命中独立作品，"
                    "不能自动合并；请修正历史作品绑定"
                ),
            )
        )
    return blocked, issues


def reject_conflicting_candidates(
    graph: ResolvedMediaGraph,
    candidates_by_key: dict[str, list[WorkCandidate]],
) -> tuple[dict[str, list[WorkCandidate]], list[ResolutionIssue]]:
    """把冲突身份标记为 rejected，并返回需要阻断确认的 issue。"""

    from dataclasses import replace

    blocked, issues = provider_identity_conflicts(graph, candidates_by_key)
    if not blocked:
        return candidates_by_key, issues
    protected: dict[str, list[WorkCandidate]] = {}
    for work_key, candidates in candidates_by_key.items():
        protected[work_key] = [
            replace(
                candidate,
                status="rejected",
            )
            if (candidate.provider, candidate.media_type, candidate.provider_id) in blocked
            else candidate
            for candidate in candidates
        ]
    return protected, issues


def can_merge_provider_identity(
    graph: ResolvedMediaGraph,
    work_keys: set[str],
) -> bool:
    """最终 merge 门只允许同一主系列边界合并。"""

    works = [work for work in graph.works if work.work_key in work_keys]
    return len(work_keys) < 2 or not any(is_independent_work(work) for work in works)


def historical_identity_conflict(
    work: ResolvedWork,
    structural_keys: set[str],
) -> IdentityDecision | None:
    """判断旧 Work 的结构绑定是否已经跨越互斥作品边界。

    ``work_source_bindings`` 是来源结构的历史证据，不是新的识别结果。
    因此只能用其中明确的 ``series:`` / ``work:`` 身份键做边界校验；普通
    目录哈希不会触发冲突。这样同一主系列的多个季度仍可复用，而同时含有
    主系列与外传/电影的污染 Work 会被导入和修复流程共同拦截。
    """

    canonical = {
        boundary
        for value in structural_keys
        if (boundary := _boundary_key(value)) is not None
    }
    current_key = _boundary_key(str(work.work_key or ""))
    if not canonical or current_key is None:
        return None
    other_keys = canonical - {current_key}
    if not other_keys:
        return None
    if not is_independent_work(work) and not any(key[0] == "work" for key in other_keys):
        return None
    return IdentityDecision(
        decision="repair_required",
        work_key=str(work.work_key or ""),
        reason_code="historical_identity_conflict",
        evidence_ids=tuple(work.source_evidence_ids),
    )
