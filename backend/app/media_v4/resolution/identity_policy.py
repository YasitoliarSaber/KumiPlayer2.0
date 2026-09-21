"""V4 作品身份的边界和 Provider 合并策略。

解析器只产生事实；本模块只根据已解析的 Work 边界判断哪些 Provider
身份可以用于跨语言/跨来源合并，避免把父系列关系误当成同一作品。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
        # title:<片名>:<年份>:<类型> 只有最后一段是年份。
        # 从左切分会把《福音战士新剧场版:序》等副标题丢掉，导致重导入误报冲突。
        return "work", title.rsplit(":", 1)[0], media_type
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
    """共享在线资料不再是候选冲突。

    在线资料是辅助信息，不是本地 Work 的唯一主键；独立作品是否合并仍由
    :func:`can_merge_provider_identity` 负责，因此共享不会把作品卡混成一张。
    """

    return set(), []


def reject_conflicting_candidates(
    graph: ResolvedMediaGraph,
    candidates_by_key: dict[str, list[WorkCandidate]],
) -> tuple[dict[str, list[WorkCandidate]], list[ResolutionIssue]]:
    """静默丢弃跨作品边界传播的旧绑定，不把导入降级为人工处理。

    数据库和人工确认允许多个本地作品共享同一在线资料。不过，当同一次解析中
    主系列与明确独立的外传同时继承同一条 *历史* 绑定时，这更可能是旧代码留下
    的污染，而不是用户本次作出的确认。这里只拒绝独立作品的历史候选；路径明确
    绑定、NFO、文件名提示和在线搜索结果均不受影响。
    """

    works = {work.work_key: work for work in graph.works}
    members_by_identity: dict[tuple[str, str, str], set[str]] = {}
    for work_key, candidates in candidates_by_key.items():
        for candidate in candidates:
            if candidate.status != "confirmed":
                continue
            identity = (candidate.provider, candidate.media_type, candidate.provider_id)
            members_by_identity.setdefault(identity, set()).add(work_key)

    polluted_slots: set[tuple[str, str, str, str]] = set()
    for identity, work_keys in members_by_identity.items():
        member_works = [works[key] for key in work_keys if key in works]
        if not any(is_independent_work(work) for work in member_works):
            continue
        if not any(not is_independent_work(work) for work in member_works):
            continue
        for work in member_works:
            if is_independent_work(work):
                polluted_slots.add((work.work_key, *identity))

    if not polluted_slots:
        return candidates_by_key, []

    protected: dict[str, list[WorkCandidate]] = {}
    for work_key, candidates in candidates_by_key.items():
        protected[work_key] = [
            replace(candidate, status="rejected")
            if candidate.evidence == "existing_provider_binding"
            and (
                work_key,
                candidate.provider,
                candidate.media_type,
                candidate.provider_id,
            ) in polluted_slots
            else candidate
            for candidate in candidates
        ]
    return protected, []


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
