import pytest

from app.media_v4.parsing.parser import V4Parser
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


def parse(path):
    evidence = to_source_evidence(SourceEntry(root_id="r", scan_id="s", provider="quark", ingest_method="openlist_api", relative_path=path, source_key=path))
    return V4Parser().parse(evidence)


@pytest.mark.parametrize("path", [
    "灵能百分百/第二季/[Ygm] Mob Psycho 100 II [14(OVA)][Ma10p_2160p][x265_flac_ass].mkv",
    "赛马娘/第一季/[Ygm] Uma Musume Pretty Derby [14(OVA01)][Ma10p_2160p].mkv",
    "命运石之门/第一季/[MAI] Steins;Gate [25 SP][Ma10p_2160p].mkv",
    "Love Live/第一季/Fan Disc/[Group] Love Live! Fan Disc [School Map #02].mkv",
    "Love Live/第一季/μ's Best Album Best Live! Collection/[Ygm] Song [Wonderful Rush].mkv",
])
def test_explicit_attached_bonus_is_not_regular_episode(path):
    facts = parse(path)
    if "OVA" in path:
        assert facts.is_importable
        assert facts.content_class == "playable_special"
        assert facts.episode_candidate is None
    else:
        assert not facts.is_importable
        assert facts.content_class in {"auxiliary", "attached_special"}


def test_malformed_episode_bracket_does_not_drop_clear_episode_number():
    facts = parse("冰海战记/第二季/[TUDO&Ygm] Vinland Saga S2 [06[Ma10p_2160p][x265_flac_ass].mkv")
    assert facts.episode_candidate == 6
    assert facts.season_candidate == 2


@pytest.mark.parametrize("path", ["Independent OVA/[Group] Independent OVA [01].mkv", "Independent OVA/[Group] Independent OVA [01(OVA01)].mkv", "电影/Other Movie [01(OVA)].mkv", "Other Movie/Other Movie.mkv", "Another Spin-off/Another Spin-off.S01E01.mkv"])
def test_independent_release_forms_remain_importable(path):
    assert parse(path).is_importable
