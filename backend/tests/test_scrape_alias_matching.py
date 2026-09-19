"""刮削匹配语料回归：同一部作品的不同语言写法必须能自动采用。

用户反馈的场景：本地目录名是罗马音（``Yuru Camp``），在线资料的主标题是中文
（``摇曳露营△``）、原名是日文（``映画 ゆるキャン△``）。匹配必须能凭**别名/原名**
判等并自动采用，不能要求用户每次人工确认。

规则现状（本次只加回归、不放宽规则）：
- 匹配语义只保留字母与数字（``△``、``！``、``:`` 等符号与空白全部忽略），
  因此 ``Yuru Camp`` 与 ``Yuru Camp△``、``摇曳露营`` 与 ``摇曳露营！`` 等值；
- 身份语义（会写进数据库的键）**不允许放宽**，否则同一标题会算出两个身份、重复导入会生成重复作品。

脏标题（带季号）只拿到"长前缀弱证据"，这是**刻意**的：它说明本地名字还有发布信息，
应当先由识别层清理，而不是靠放宽匹配把噪声也认下来。
"""

from __future__ import annotations

from app.media_v4.resolution.ranker import (
    AUTO_ADOPT_MIN_SCORE,
    auto_adopt,
    rank_candidates,
    score_candidate,
)

# 一部真实形状的在线资料：中文主标题 + 日文原名 + 常见别名
CANDIDATE = {
    "provider": "tmdb",
    "provider_id": "999001",
    "title": "摇曳露营△",
    "original_title": "ゆるキャン△",
    "aliases": ["Yuru Camp", "Laid-Back Camp", "摇曳露营"],
    "year": 2018,
    "media_type": "tv",
}


def _target(title: str, *, year: int | None = 2018) -> dict:
    return {
        "preferred_title": title,
        "queries": [],
        "aliases": [],
        "media_type": "tv",
        "year": year,
    }


def _adopt(title: str) -> tuple[bool, str, float]:
    ranked = rank_candidates(_target(title), [dict(CANDIDATE)])
    adopted, reason = auto_adopt(ranked)
    score = ranked[0].score if ranked else 0.0
    return adopted is not None, reason, score


def test_romaji_local_name_matches_chinese_online_title():
    safe, reason, score = _adopt("Yuru Camp")

    assert safe, f"罗马音目录名必须能凭别名自动采用（原因：{reason}，分数：{score}）"
    assert score >= AUTO_ADOPT_MIN_SCORE


def test_every_language_variant_of_the_same_work_is_adoptable():
    for title in ("Yuru Camp", "Yuru Camp△", "Laid-Back Camp", "摇曳露营", "摇曳露营！"):
        safe, reason, _score = _adopt(title)
        assert safe, f"{title!r} 应当可以自动采用，实际被拒：{reason}"


def test_punctuation_and_symbols_do_not_break_equality():
    """``△``/``！``/``:`` 这类符号不参与判等（``Re:Zero`` 与 ``Re Zero`` 同理）。"""

    candidate = dict(CANDIDATE)
    candidate.update({"title": "Re:Zero", "original_title": "", "aliases": []})
    for title in ("Re Zero", "Re:Zero", "re：zero"):
        ranked = rank_candidates(_target(title, year=None), [candidate])
        assert ranked[0].identity_safe, f"{title!r} 应与 Re:Zero 等值"


def test_dirty_title_only_gets_weak_evidence():
    """带季号的脏标题只拿弱证据——这是刻意的，用来暴露"识别层没清理干净"。"""

    safe, reason, score = _adopt("Yuru Camp Season 2")

    assert not safe, "脏标题不应被自动采用，否则会把发布信息写进作品身份"
    assert score < AUTO_ADOPT_MIN_SCORE
    assert "弱证据" in reason or "无完整标题" in reason


def test_year_mismatch_still_blocks_even_with_alias_equality():
    """别名等值不能压过年份硬冲突（防止把不同年份的续作/剧场版混为一部）。"""

    candidate = dict(CANDIDATE)
    candidate["year"] = 2024
    ranked = rank_candidates(_target("Yuru Camp", year=2018), [candidate])

    assert ranked[0].blocked, "年份硬冲突必须阻断"
    adopted, _reason = auto_adopt(ranked)
    assert adopted is None


def test_score_is_decomposable_for_diagnostics():
    """分数可解释：等值 50/55 + 类型 5 + 领域 10，便于排查"为什么没自动采用"。"""

    ranked = rank_candidates(_target("Yuru Camp"), [dict(CANDIDATE)])
    reasons = " ".join(ranked[0].reasons)

    assert "等值" in reasons
    assert ranked[0].score >= 55.0
