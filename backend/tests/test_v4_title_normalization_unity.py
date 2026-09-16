"""O17：标题规范化必须有两个**明确且同源**的语义，跨组件比较不得各行其是。

历史上有四份同名私有函数：三份是"身份"语义（会进入持久化身份键），一份是"匹配"
语义（只用于比较）。同一对标题因此在草稿候选评分里判不等、在刮削排名里判等值，
用户会看到"导入预览说没把握、刮削却自动采用了"（或反向）。
"""

from __future__ import annotations

import pytest

_SAMPLES = [
    "摇曳露营！",
    "《摇曳露营》",
    "Re:Zero",
    "Re Zero",
    "Fate/stay night",
    "  Show  ",
    "葬送的芙莉莲 第一季",
    "",
]


def test_identity_semantics_keeps_inner_punctuation_but_trims_edges():
    from app.media_v4.resolution.title_norm import normalize_identity_title

    # 两端的装饰标点被剥掉，正文里的标点属于标题本身（NFKC 会把全角 ！ 折成半角 !）。
    assert normalize_identity_title("《摇曳露营》") == "摇曳露营"
    assert normalize_identity_title("摇曳露营！") == "摇曳露营!"
    assert normalize_identity_title("Re:Zero") == "re:zero"
    assert normalize_identity_title("  Show  ") == "show"


def test_match_semantics_ignores_all_punctuation_and_spaces():
    from app.media_v4.resolution.title_norm import normalize_match_title

    assert normalize_match_title("摇曳露营！") == normalize_match_title("摇曳露营")
    assert normalize_match_title("Re:Zero") == normalize_match_title("Re Zero")
    assert normalize_match_title("Fate/stay night") == normalize_match_title("Fate stay night")


def test_all_private_helpers_delegate_to_the_shared_implementations():
    from app.media_v4.resolution import candidates as candidates_module
    from app.media_v4.resolution import ranker as ranker_module
    from app.media_v4.resolution import resolver as resolver_module
    from app.media_v4.resolution.title_norm import (
        normalize_identity_title,
        normalize_match_title,
    )
    from app.media_v4.revisions import service as service_module

    for sample in _SAMPLES:
        assert ranker_module._normalize_title(sample) == normalize_match_title(sample)
        assert candidates_module._normalize_title(sample) == normalize_identity_title(sample)
        assert resolver_module._normalize_title(sample) == normalize_identity_title(sample)
        assert service_module._normalize_title(sample) == normalize_identity_title(sample)


def test_ranker_and_draft_scorer_agree_on_a_punctuation_only_difference():
    """核心断言：同一对"只差标点"的标题，刮削排名与草稿评分必须同结论。"""

    from app.media_v4.domain.models import ResolvedWork
    from app.media_v4.resolution.candidates import WorkCandidate, _score_candidate
    from app.media_v4.resolution.ranker import _title_identity_level

    local_title = "摇曳露营！"
    candidate_title = "摇曳露营"

    level, reason = _title_identity_level([local_title], {"title": candidate_title})
    assert level == 2, f"刮削排名应判完整等值，实际 {level}/{reason}"

    scored = _score_candidate(
        WorkCandidate(
            work_key="work:1",
            provider="tmdb",
            provider_id="1",
            media_type="tv",
            title=candidate_title,
            year=2024,
            evidence="online_search",
            confidence="medium",
        ),
        ResolvedWork(work_key="work:1", preferred_title=local_title, year=2024, media_type="tv"),
        queries=[],
        nfo_titles=[],
    )

    assert scored.confidence == "high", "草稿评分必须与刮削排名一致（否则一边说没把握、一边自动采用）"


@pytest.mark.parametrize("left, right", [
    ("摇曳露营！", "摇曳露营"),
    ("Re:Zero", "Re Zero"),
    ("《摇曳露营》", "摇曳露营"),
])
def test_identity_semantics_are_not_loosened(left, right):
    """反证：身份语义**不能**被放宽成匹配语义，否则会改写已持久化的身份键。"""

    from app.media_v4.resolution.title_norm import normalize_identity_title

    if left == "《摇曳露营》":
        assert normalize_identity_title(left) == normalize_identity_title(right)
    else:
        assert normalize_identity_title(left) != normalize_identity_title(right), (
            "身份键必须保持稳定：同一个标题的标点差异不能在重新导入时算出新键（会产生重复作品）"
        )
