"""V4 Work Resolver 的身份聚合合同。"""

from __future__ import annotations


def _pair(evidence_id: str, *, provider: str, title: str, year: int | None = 2024, tmdb_id: int | None = None):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id=f"scan-{provider}",
        root_id=f"root-{provider}",
        source_key=f"{provider}:{evidence_id}",
        relative_path=f"{title}/{title}.S01E01.mkv",
        entry_kind="video",
        provider=provider,
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=title,
        title_candidates=(title,),
        year_candidate=year,
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        tmdb_hint_id=tmdb_id,
        tmdb_hint_type="tv" if tmdb_id else "",
        confidence="high",
    )
    return evidence, facts


def test_same_series_from_multiple_sources_resolves_to_one_work_card():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(
        [
            _pair("ev-115", provider="pan115", title="Show", tmdb_id=42),
            _pair("ev-local", provider="local", title="Show", tmdb_id=42),
        ]
    )

    assert len(graph.works) == 1
    assert graph.works[0].source_evidence_ids == ("ev-115", "ev-local")


def test_same_title_different_year_is_not_silently_merged():
    from app.media_v4.resolution.resolver import MediaResolver

    graph = MediaResolver().resolve(
        [
            _pair("ev-old", provider="local", title="Show", year=2010),
            _pair("ev-new", provider="local", title="Show", year=2024),
        ]
    )

    assert len(graph.works) == 2


def test_missing_identity_becomes_review_issue_instead_of_path_hash_identity():
    from app.media_v4.resolution.resolver import MediaResolver

    evidence, facts = _pair("ev-unknown", provider="local", title="")
    graph = MediaResolver().resolve([(evidence, facts)])

    assert graph.works == ()
    assert any(issue.code == "work_identity_missing" for issue in graph.issues)


def test_generic_category_directory_does_not_merge_different_works_on_confirmation(tmp_path):
    from dataclasses import replace

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "category.db")
    database.initialize()
    first_evidence, first_facts = _pair(
        "ev-a", provider="pan115", title="Show A", year=2024
    )
    second_evidence, second_facts = _pair(
        "ev-b", provider="pan115", title="Show B", year=2024
    )
    first_evidence = replace(
        first_evidence,
        root_id="shared-root",
        scan_id="shared-scan",
        relative_path="动画/Show A/Show A.S01E01.mkv",
    )
    second_evidence = replace(
        second_evidence,
        root_id="shared-root",
        scan_id="shared-scan",
        relative_path="动画/Show B/Show B.S01E01.mkv",
    )

    service = V4RevisionService(database)
    service.create_draft(
        "rev-category",
        [(first_evidence, first_facts), (second_evidence, second_facts)],
    )
    service.confirm("rev-category")

    with database.connect() as conn:
        works = conn.execute("SELECT preferred_title FROM works ORDER BY preferred_title").fetchall()
    assert [row["preferred_title"] for row in works] == ["Show A", "Show B"]
