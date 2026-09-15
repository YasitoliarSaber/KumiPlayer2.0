"""B3：识别门控与标题清洗合同。

- 结构作品身份已经确定时，“解析有不确定信息”只能是提示，不能阻断整批确认；
- 身份确实无法确定的条目仍然必须阻断；
- 前置画质前缀（4k / 8k / 2160p / BDRip…）是发布信息，必须剥离，但不许误伤
  86、22/7、3月的狮子、91Days 这类真标题。
"""

from __future__ import annotations


def test_leading_quality_prefix_is_stripped_without_touching_real_titles():
    from app.recognition.title_cleaner import strip_leading_quality_prefix

    assert strip_leading_quality_prefix("4k偶像大师 灰姑娘女孩 U149") == "偶像大师 灰姑娘女孩 U149"
    assert strip_leading_quality_prefix("4K 莉可丽丝") == "莉可丽丝"
    assert strip_leading_quality_prefix("2160p 冰菓") == "冰菓"
    assert strip_leading_quality_prefix("BDRip 冰菓") == "冰菓"

    # 真标题不许被误伤：数字开头、分数标题、含月份、字母数字混排。
    for title in ("86", "22/7", "3月的狮子", "91Days", "4月是你的谎言"):
        assert strip_leading_quality_prefix(title) == title


def test_container_cleaning_strips_leading_quality_prefix():
    from app.recognition.title_cleaner import clean_work_title_container

    result = clean_work_title_container("4k偶像大师 灰姑娘女孩 U149")

    assert "4k" not in result.title.casefold()
    assert "偶像大师" in result.title
    assert result.changed is True


def test_review_flag_with_known_identity_is_only_a_hint():
    """容器为空但身份可得（例如所选根目录直属的 SP01.mkv）不再阻断确认。"""

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.resolution.resolver import NON_BLOCKING_ISSUE_CODES, MediaResolver

    evidence = SourceEvidence(
        evidence_id="ev-sp",
        scan_id="scan-b3",
        root_id="root-b3",
        source_key="quark:/动画/Show/SP01.mkv",
        relative_path="SP01.mkv",
        entry_kind="video",
        provider="quark",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-sp",
        evidence_id="ev-sp",
        parser_version="fixture",
        work_title="",
        title_candidates=("Show",),
        media_type="tv",
        group_type="special",
        series_group="Show",
        confidence="low",
        needs_review=True,
    )

    graph = MediaResolver().resolve([(evidence, facts)])
    codes = [issue.code for issue in graph.issues]

    assert "parsed_facts_need_review" not in codes
    assert "parsed_facts_review_hint" in codes
    assert "parsed_facts_review_hint" in NON_BLOCKING_ISSUE_CODES


def test_review_flag_without_identity_still_blocks():
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.resolution.resolver import NON_BLOCKING_ISSUE_CODES, MediaResolver

    evidence = SourceEvidence(
        evidence_id="ev-none",
        scan_id="scan-b3",
        root_id="root-b3",
        source_key="quark:/动画/SP01.mkv",
        relative_path="SP01.mkv",
        entry_kind="video",
        provider="quark",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-none",
        evidence_id="ev-none",
        parser_version="fixture",
        work_title="",
        title_candidates=(),
        media_type="tv",
        group_type="special",
        series_group="",
        confidence="low",
        needs_review=True,
    )

    graph = MediaResolver().resolve([(evidence, facts)])
    codes = [issue.code for issue in graph.issues]

    assert "parsed_facts_need_review" in codes
    assert "parsed_facts_need_review" not in NON_BLOCKING_ISSUE_CODES
