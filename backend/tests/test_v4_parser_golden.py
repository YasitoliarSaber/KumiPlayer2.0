"""V4 纯解析器黄金样本。"""

from __future__ import annotations

import pytest


def _evidence(relative_path: str, *, provider: str = "local"):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return to_source_evidence(
        SourceEntry(
            root_id="root-parser",
            scan_id="scan-parser",
            provider=provider,
            ingest_method="local_scan",
            relative_path=relative_path,
        )
    )


def test_parser_preserves_raw_tokens_and_returns_structured_season_episode_facts():
    from app.media_v4.parsing.parser import V4Parser

    facts = V4Parser().parse(_evidence("Show/Show.S01E03.Title.2160p.mkv"))

    assert facts.resource_type == "video"
    assert facts.group_type == "season"
    assert facts.title_candidates
    assert facts.season_token_raw == "S01"
    assert facts.episode_token_raw == "E03"
    assert facts.season_candidate == 1
    assert facts.episode_candidate == 3
    assert facts.absolute_episode_candidate is None
    assert facts.parser_version == V4Parser.VERSION


def test_parser_keeps_special_and_auxiliary_as_facts_without_destroying_episode_fields():
    from app.media_v4.parsing.parser import V4Parser

    special = V4Parser().parse(_evidence("Show/Specials/Show [SP01].mkv"))
    auxiliary = V4Parser().parse(_evidence("Show/OPED/NCOP01.mkv"))

    assert special.group_type == "special"
    assert special.special_candidate is True
    assert auxiliary.group_type in {"auxiliary", "ignored"}
    assert auxiliary.is_importable is False


@pytest.mark.parametrize(
    "filename",
    (
        "Show [OPED Recording 01].mkv",
        "Show [BGM Recording 04].mkv",
        "Show [Location Hunting 12].mkv",
        "Show [Event02].mkv",
        "Show [24(NC Ver.)].mkv",
        "Show [Preview].mkv",
        "Show [Preview Collection].mkv",
        "Show [Digest].mkv",
        "Show [S3 Announcement].mkv",
        "Show [Program].mkv",
        "Show [Movie manner].mkv",
    ),
)
def test_parser_keeps_production_and_credit_extras_out_of_specials(filename: str):
    """SPs 目录不能把更具体的制作素材、预告或无字幕版升级成特别篇。"""

    from app.media_v4.parsing.parser import V4Parser

    facts = V4Parser().parse(_evidence(f"Show/SPs/{filename}"))

    assert facts.group_type in {"auxiliary", "ignored"}
    assert facts.is_importable is False


def test_parser_preserves_full_compound_special_token():
    from app.media_v4.parsing.parser import V4Parser

    facts = V4Parser().parse(
        _evidence("Show/Season 1/SPs/Show [SP01_13][Ma10p_2160p].mkv")
    )

    assert facts.group_type == "special"
    assert facts.special_number == 1
    assert facts.episode_token_raw == "SP01_13"


def test_parser_never_emits_or_changes_a_work_identity():
    from app.media_v4.parsing.parser import V4Parser

    facts = V4Parser().parse(_evidence("Show/Show.S01E01.mkv"))

    assert not hasattr(facts, "work_id")
    assert not hasattr(facts, "canonical_work_id")


def test_parser_extracts_quality_release_group_and_content_edition_facts():
    from app.media_v4.parsing.parser import V4Parser

    facts = V4Parser().parse(
        _evidence("Show/Show.S01E01.2160p.Extended-NoxiaAI.mkv")
    )

    assert "2160p" in facts.quality_tags
    assert "extended" in facts.edition_tags
    assert facts.release_group == "NoxiaAI"


def test_parser_does_not_mistake_year_or_explicit_local_episode_for_absolute_number():
    from app.media_v4.parsing.parser import V4Parser

    explicit = V4Parser().parse(_evidence("Show/Show.S02E03.2024.1080p.mkv"))
    uncertain = V4Parser().parse(_evidence("Show/Show.2024.1080p.mkv"))
    bare = V4Parser().parse(_evidence("Show/Show - 13.mkv"))

    assert explicit.absolute_episode_candidate is None
    assert uncertain.absolute_episode_candidate is None
    assert bare.absolute_episode_candidate == 13
    assert bare.episode_token_raw == "- 13"


def test_episode_range_end_requires_token_boundary_so_title_numbers_stay_in_title():
    """集标题里的数字不得成为范围终点（真实样本：200万年/100% 安全的水）。"""

    from app.media_v4.parsing.parser import V4Parser

    cjk_counter = V4Parser().parse(_evidence("Show/Season 1/Show - S01E15 - 200万年的结晶.mkv"))
    percent = V4Parser().parse(_evidence("Show/S01/Show - S01E06 - 100%安全的水.mkv"))
    explicit_range = V4Parser().parse(_evidence("Show/Season 1/Show - S01E01-E12.mkv"))
    plain_range = V4Parser().parse(_evidence("Show/Season 1/Show - S01E01-12.mkv"))
    cjk_suffix_range = V4Parser().parse(_evidence("Show/Season 1/Show - S01E01-12集.mkv"))
    jp_suffix_range = V4Parser().parse(_evidence("Show/Season 1/Show - S01E01-12話.mkv"))
    magnitude_suffix = V4Parser().parse(_evidence("Show/Season 1/Show - S01E01-12万回.mkv"))
    letter_suffix = V4Parser().parse(_evidence("Show/Season 1/Show - S01E01-12bit.mkv"))

    assert cjk_counter.episode_candidate == 15
    assert cjk_counter.episode_range is None
    assert cjk_counter.episode_title == "200万年的结晶"
    assert percent.episode_candidate == 6
    assert percent.episode_range is None
    assert percent.episode_title == "100%安全的水"
    assert explicit_range.episode_range == (1, 12)
    assert plain_range.episode_range == (1, 12)
    # 中文量词"集/話/话"是合法的范围终点后缀；数量词与字母不是。
    assert cjk_suffix_range.episode_range == (1, 12)
    assert jp_suffix_range.episode_range == (1, 12)
    assert magnitude_suffix.episode_range is None
    assert letter_suffix.episode_range is None


def test_sidecar_nfo_with_dtd_or_entity_declarations_is_rejected(tmp_path):
    """sidecar NFO 只读 uniqueid 事实；DTD/实体声明一律拒绝解析。"""


    from app.media_v4.parsing.parser import V4Parser

    parser = V4Parser()
    nfo = tmp_path / "show.nfo"
    nfo.write_text(
        "<?xml version='1.0'?>\n<!DOCTYPE tvshow [<!ENTITY xxe SYSTEM 'file:///C:/Windows/win.ini'>]>"
        "\n<tvshow><uniqueid type='tmdb' default='true'>&xxe;</uniqueid></tvshow>\n",
        encoding="utf-8",
    )
    evidence = _evidence(f"Show/{nfo.name}")
    evidence = type(evidence)(
        **{**{field: getattr(evidence, field) for field in evidence.__dataclass_fields__},
           "source_locator": str(nfo), "playback_locator": str(nfo), "entry_kind": "metadata"},
    )

    facts = parser.parse(evidence)

    assert facts.tmdb_hint_id is None
