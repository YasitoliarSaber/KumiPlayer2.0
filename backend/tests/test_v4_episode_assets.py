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


def test_content_edition_keeps_one_logical_episode_identity_with_distinct_variants():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(
        [_pair("default"), _pair("extended", edition_tags=("extended",))]
    )

    assert len(graph.episodes) == 2
    assert len({episode.episode_key for episode in graph.episodes}) == 1
    assert {episode.edition_key for episode in graph.episodes} == {"default", "extended"}


def test_confirmed_content_edition_is_not_persisted_as_a_second_episode(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "editions.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft(
        "rev-editions",
        [_pair("default"), _pair("extended", edition_tags=("extended",))],
    )
    service.confirm("rev-editions")

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM episode_assets").fetchone()[0] == 2
        edition_ids = [row[0] for row in conn.execute("SELECT edition_id FROM episode_assets")]
    assert edition_ids.count(None) == 1


def test_non_importable_auxiliary_is_not_promoted_to_media_graph():
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.resolution.resolver import MediaResolver

    evidence = SourceEvidence(
        evidence_id="aux",
        scan_id="scan-aux",
        root_id="root-aux",
        source_key="aux",
        relative_path="Show/OPED/NCOP01.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-aux",
        evidence_id="aux",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="auxiliary",
        is_importable=False,
        is_auxiliary=True,
    )

    graph = MediaResolver().resolve([(evidence, facts)])

    assert graph.works == ()
    assert graph.episodes == ()


def test_parser_review_flag_becomes_a_resolution_issue():
    from app.media_v4.domain.models import ParsedFacts
    from app.media_v4.resolution.resolver import MediaResolver

    evidence, facts = _pair("review")
    facts = ParsedFacts(
        **{
            field: getattr(facts, field)
            for field in facts.__dataclass_fields__
            if field != "needs_review"
        },
        needs_review=True,
    )

    graph = MediaResolver().resolve([(evidence, facts)])

    assert any(issue.code == "parsed_facts_need_review" for issue in graph.issues)


def test_movie_assets_bind_directly_to_work_without_fake_episode(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.resolution.resolver import MediaResolver
    from app.media_v4.revisions.service import V4RevisionService

    evidence = SourceEvidence(
        evidence_id="movie",
        scan_id="scan-movie",
        root_id="root-movie",
        source_key="Movie.mkv",
        relative_path="Movie (2024)/Movie.mkv",
        entry_kind="video",
        provider="local",
        source_locator="Movie.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-movie",
        evidence_id="movie",
        parser_version="fixture",
        work_title="Movie",
        title_candidates=("Movie",),
        year_candidate=2024,
        media_type="movie",
        group_type="movie",
    )
    graph = MediaResolver().resolve([(evidence, facts)])
    assert graph.episodes == ()
    assert graph.work_assets[0].asset_evidence_ids == ("movie",)

    database = V4Database(tmp_path / "movie.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-movie", [(evidence, facts)])
    service.confirm("rev-movie")
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_assets").fetchone()[0] == 1
