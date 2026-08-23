"""V4 纯解析器黄金样本。"""

from __future__ import annotations


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
