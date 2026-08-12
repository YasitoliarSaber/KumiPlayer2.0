"""自动刮削质量门禁。

此模块只度量既有自动采用策略的质量，不参与生产匹配、评分或候选排序。
它的职责是将经过人工确认的离线案例转为可回归指标，防止后续改动削弱
KumiPlayer 已有的标题清洗、身份校验、季映射和多来源证据链。
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.scrape.models import ScrapeCandidate, ScrapeTarget

AutoDecision = Callable[
    [ScrapeTarget, list[ScrapeCandidate]],
    tuple[ScrapeCandidate | None, str],
]


@dataclass(frozen=True)
class ScrapeQualityCase:
    """一个已人工确认的自动采用决策案例。"""

    name: str
    target: ScrapeTarget
    candidates: list[ScrapeCandidate]
    expected_tmdb_id: int | None


@dataclass(frozen=True)
class ScrapeQualityReport:
    """质量门禁结果。

    recall：应该自动采用的正确案例中，实际采用正确候选的比例。
    precision：实际自动采用的案例中，采用正确候选的比例。
    """

    total_cases: int
    expected_auto_count: int
    actual_auto_count: int
    correct_auto_count: int
    recall: float
    precision: float
    failed_cases: tuple[str, ...]


def evaluate_auto_decision_quality(
    cases: Iterable[ScrapeQualityCase],
    decide: AutoDecision,
) -> ScrapeQualityReport:
    """以当前自动采用函数评估人工确认案例，不改变任何生产行为。"""
    all_cases = list(cases)
    expected_auto_count = 0
    actual_auto_count = 0
    correct_auto_count = 0
    failed_cases: list[str] = []

    for case in all_cases:
        selected, _reason = decide(case.target, case.candidates)
        actual_id = selected.tmdb_id if selected is not None else None
        expected_id = case.expected_tmdb_id
        if expected_id is not None:
            expected_auto_count += 1
        if actual_id is not None:
            actual_auto_count += 1
        if actual_id == expected_id:
            if expected_id is not None:
                correct_auto_count += 1
        else:
            failed_cases.append(case.name)

    recall = correct_auto_count / expected_auto_count if expected_auto_count else 1.0
    precision = correct_auto_count / actual_auto_count if actual_auto_count else 1.0
    return ScrapeQualityReport(
        total_cases=len(all_cases),
        expected_auto_count=expected_auto_count,
        actual_auto_count=actual_auto_count,
        correct_auto_count=correct_auto_count,
        recall=recall,
        precision=precision,
        failed_cases=tuple(failed_cases),
    )
