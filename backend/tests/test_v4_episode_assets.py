"""Episode 与物理 Asset 分离合同。"""

from __future__ import annotations


def _pair(evidence_id: str, *, edition_tags: tuple[str, ...] = ()):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-assets",
        root_id="root-assets",
        source_key=evidence_id,
        relative_path=f"Show/Show.S01E01.{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        fingerprint=f"sha256:{evidence_id}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        edition_tags=edition_tags,
        quality_tags=("2160p",) if evidence_id == "4k" else ("1080p",),
    )
    return evidence, facts


def test_same_logical_episode_keeps_all_physical_assets():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve([_pair("1080p"), _pair("4k")])

    assert len(graph.episodes) == 1
    assert graph.episodes[0].asset_evidence_ids == ("1080p", "4k")


def test_content_edition_separates_logical_episode_variants_but_keeps_both():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(
        [_pair("default"), _pair("extended", edition_tags=("extended",))]
    )

    assert len(graph.episodes) == 2
    assert {episode.edition_key for episode in graph.episodes} == {"default", "extended"}
