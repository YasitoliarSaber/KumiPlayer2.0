"""V4 本地季度/集号/Provider 映射分离合同。"""

from __future__ import annotations


def _fact(*, evidence_id: str, season: int | None, episode: int | None, absolute: int | None = None, special: bool = False, special_number: int | None = None, tmdb_id: int = 42):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-season",
        root_id="root-season",
        source_key=evidence_id,
        relative_path=f"Show/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        year_candidate=2024,
        media_type="tv",
        group_type="special" if special else "season",
        season_candidate=season,
        episode_candidate=episode,
        absolute_episode_candidate=absolute,
        special_candidate=special,
        special_number=special_number,
        tmdb_hint_id=tmdb_id,
        tmdb_hint_type="tv",
    )
    return evidence, facts


def test_absolute_and_local_episode_numbers_are_kept_as_separate_facts():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(
        [_fact(evidence_id="ev-abs", season=2, episode=1, absolute=13)]
    )

    episode = graph.episodes[0]
    assert episode.local_season_number == 2
    assert episode.local_episode_number == 1
    assert episode.absolute_episode_number == 13
    assert episode.provider_season_number is None
    assert episode.provider_episode_number is None


def test_special_episode_is_not_simulated_by_clearing_all_episode_identity():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(
        [_fact(evidence_id="ev-sp", season=None, episode=None, special=True, special_number=1)]
    )

    episode = graph.episodes[0]
    assert episode.season_kind == "special"
    assert episode.local_season_number == 0
    assert episode.special_number == 1
    assert episode.episode_kind == "special"


def test_conflicting_absolute_numbers_do_not_split_a_local_episode():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(
        [
            _fact(evidence_id="ev-abs-a", season=2, episode=1, absolute=13),
            _fact(evidence_id="ev-abs-b", season=2, episode=1, absolute=14),
        ]
    )

    assert len(graph.episodes) == 1
    assert graph.episodes[0].asset_evidence_ids == ("ev-abs-a", "ev-abs-b")
    assert any(issue.code == "absolute_episode_conflict" for issue in graph.issues)
