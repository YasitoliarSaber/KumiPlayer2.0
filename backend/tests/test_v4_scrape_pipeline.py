"""V4 刮削任务合同。"""

from __future__ import annotations


def _entry(evidence_id: str):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-scrape",
        root_id="root-scrape",
        source_key=evidence_id,
        relative_path=f"Show/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://{evidence_id}",
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
        tmdb_hint_id=42,
        tmdb_hint_type="tv",
    )
    return evidence, facts


def test_scrape_targets_are_grouped_by_work_and_do_not_reparse(tmp_path, monkeypatch):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "scrape.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-scrape", [_entry("a"), _entry("b")])
    revision_service.confirm("rev-scrape")
    scrape = V4ScrapeService(database)
    jobs = scrape.enqueue_for_revision("rev-scrape")

    assert len(jobs) == 1
    monkeypatch.setattr(
        "app.recognition.media.recognize_media",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scrape 不得重新识别")),
    )
    scrape.process(jobs[0]["job_id"], lambda _work: {"provider": "tmdb", "provider_id": "42", "title": "Show"})

    bindings = scrape.list_bindings("rev-scrape")
    assert len(bindings) == 1
    assert bindings[0]["provider_id"] == "42"


def test_scrape_metadata_does_not_overwrite_local_episode_number(tmp_path):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "scrape-fields.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-scrape", [_entry("a")])
    revision_service.confirm("rev-scrape")
    scrape = V4ScrapeService(database)
    jobs = scrape.enqueue_for_revision("rev-scrape")
    with database.connect() as conn:
        episode_id = conn.execute("SELECT episode_id FROM episodes").fetchone()[0]
        season_id = conn.execute("SELECT season_id FROM seasons").fetchone()[0]
    scrape.process(
        jobs[0]["job_id"],
        lambda _work: {
            "provider": "tmdb",
            "provider_id": "42",
            "episode_mappings": [{
                "episode_id": episode_id,
                "provider_season_number": 9,
                "provider_episode_number": 99,
            }],
            "season_mappings": [{
                "season_id": season_id,
                "provider_season_number": 9,
            }],
        },
    )

    with database.connect() as conn:
        row = conn.execute("SELECT local_episode_number FROM episodes").fetchone()
        episode_mapping = conn.execute(
            "SELECT provider_season_number, provider_episode_number FROM episode_provider_mappings"
        ).fetchone()
        season_mapping = conn.execute(
            "SELECT provider_season_number FROM season_provider_mappings"
        ).fetchone()
    assert row["local_episode_number"] == 1
    assert tuple(episode_mapping) == (9, 99)
    assert season_mapping["provider_season_number"] == 9


def test_invalid_scrape_mapping_is_rejected_before_metadata_files_are_published(tmp_path):
    import pytest

    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "invalid-scrape.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-invalid-scrape", [_entry("a")])
    revisions.confirm("rev-invalid-scrape")
    job = next(item for item in revisions.list_jobs("rev-invalid-scrape") if item["job_type"] == "scrape_work")
    mirror_root = tmp_path / "mirror"

    with pytest.raises(ValueError, match="不属于当前 revision"):
        V4ScrapeService(database).process(
            job["job_id"],
            lambda _target: {
                "provider": "tmdb",
                "provider_id": "42",
                "title": "Show",
                "episode_mappings": [{"episode_id": "foreign-episode"}],
            },
            mirror_root=mirror_root,
        )

    assert not list(mirror_root.rglob("*.nfo"))
    with database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE revision_id = 'rev-invalid-scrape'"
        ).fetchone()[0] == 0


def test_scrape_target_contains_provider_hint_and_episode_mapping(tmp_path):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "scrape-target.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-scrape", [_entry("a")])
    revision_service.confirm("rev-scrape")
    job = next(item for item in revision_service.list_jobs("rev-scrape") if item["job_type"] == "scrape_work")
    captured = []

    V4ScrapeService(database).process(
        job["job_id"],
        lambda target: captured.append(target) or {
            "provider": "tmdb",
            "provider_id": "42",
            "title": "Show",
        },
    )

    assert captured[0]["provider_bindings"] == [
        {"provider": "tmdb", "media_type": "tv", "provider_id": "42"}
    ]
    assert captured[0]["episodes"][0]["local_season_number"] == 1
    assert captured[0]["episodes"][0]["local_episode_number"] == 1
    with database.connect() as conn:
        binding = conn.execute(
            "SELECT media_type FROM provider_bindings WHERE provider = 'tmdb'"
        ).fetchone()
        attempts = conn.execute(
            "SELECT attempts FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()[0]
    assert binding["media_type"] == "tv"
    assert attempts == 1


def test_metadata_search_requires_unique_exact_title_and_year_match():
    from app.media_v4.jobs.metadata import _select_search_result

    results = [
        {"id": 1, "name": "Show", "first_air_date": "2020-01-01"},
        {"id": 2, "name": "Show", "first_air_date": "2024-01-01"},
        {"id": 3, "name": "Different Show", "first_air_date": "2024-01-01"},
    ]

    assert _select_search_result(results, {"preferred_title": "Show", "year": 2024}, "tv")["id"] == 2
    assert _select_search_result(results, {"preferred_title": "Show"}, "tv") is None
    assert _select_search_result(results, {"preferred_title": "Missing", "year": 2024}, "tv") is None
