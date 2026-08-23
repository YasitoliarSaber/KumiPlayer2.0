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
    scrape.process(jobs[0]["job_id"], lambda _work: {"provider": "tmdb", "provider_id": "42", "season": 9, "episode": 99})

    with database.connect() as conn:
        row = conn.execute("SELECT local_episode_number FROM episodes").fetchone()
    assert row["local_episode_number"] == 1
