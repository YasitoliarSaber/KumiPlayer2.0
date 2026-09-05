"""V4 刮削任务合同。"""

from __future__ import annotations

import json
from pathlib import Path


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



def _patch_scrape_env(tmp_path, monkeypatch):
    """完整性必经：测试内显式给出镜像根与 remote artwork 配置，避免触碰真实 data/。"""

    from types import SimpleNamespace

    from app.media_v4.jobs import completeness as completeness_module
    from app.media_v4.jobs import metadata_artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module, "load_config", lambda: SimpleNamespace(
        artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    monkeypatch.setattr(completeness_module, "load_config", lambda: SimpleNamespace(
        artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    return tmp_path / "mirror"


def _ready_metadata(provider_id="42"):
    return {
        "provider": "tmdb",
        "provider_id": provider_id,
        "media_type": "tv",
        "title": "Show",
        "metadata_state": "ready",
        "poster_url": "https://image.tmdb.org/t/p/w780/p.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/original/f.jpg",
    }

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
    mirror_root = _patch_scrape_env(tmp_path, monkeypatch)
    scrape.process(jobs[0]["job_id"], lambda _work: _ready_metadata("42"), mirror_root=mirror_root)

    bindings = scrape.list_bindings("rev-scrape")
    assert len(bindings) == 1
    assert bindings[0]["provider_id"] == "42"


def test_scrape_metadata_does_not_overwrite_local_episode_number(tmp_path, monkeypatch):
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
            **_ready_metadata(),
        },
        mirror_root=_patch_scrape_env(tmp_path, monkeypatch),
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


def test_scrape_provider_identity_conflict_becomes_recoverable_review_state(tmp_path, monkeypatch):
    """第三步的重复 Provider 身份不能泄漏 SQLite 唯一索引或写坏既有作品。"""

    from dataclasses import replace

    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "scrape-provider-conflict.db")
    database.initialize()
    revisions = V4RevisionService(database)

    owner_evidence, owner_facts = _entry("owner")
    owner_evidence = replace(
        owner_evidence,
        relative_path="Owner Show/S01E01.mkv",
        source_key="Owner Show/S01E01.mkv",
    )
    owner_facts = replace(
        owner_facts,
        work_title="Owner Show",
        title_candidates=("Owner Show",),
        tmdb_hint_id=None,
        tmdb_hint_type="",
    )
    revisions.create_draft("rev-owner-scrape", [(owner_evidence, owner_facts)])
    revisions.confirm("rev-owner-scrape")
    owner_job = V4ScrapeService(database).enqueue_for_revision("rev-owner-scrape")[0]
    mirror_root = _patch_scrape_env(tmp_path, monkeypatch)
    V4ScrapeService(database).process(
        owner_job["job_id"],
        lambda _target: {**_ready_metadata("99"), "title": "Owner Show"},
        mirror_root=mirror_root,
    )

    target_evidence, target_facts = _entry("target")
    target_evidence = replace(
        target_evidence,
        root_id="root-target",
        scan_id="scan-target",
        relative_path="Target Show/S01E01.mkv",
        source_key="Target Show/S01E01.mkv",
    )
    target_facts = replace(
        target_facts,
        work_title="Target Show",
        title_candidates=("Target Show",),
        tmdb_hint_id=None,
        tmdb_hint_type="",
    )
    revisions.create_draft("rev-target-scrape", [(target_evidence, target_facts)])
    revisions.confirm("rev-target-scrape")
    target_job = V4ScrapeService(database).enqueue_for_revision("rev-target-scrape")[0]

    V4ScrapeService(database).process(
        target_job["job_id"],
        lambda _target: {**_ready_metadata("99"), "title": "Target Show"},
        mirror_root=mirror_root,
    )

    with database.connect() as conn:
        job = conn.execute("SELECT status, last_error FROM jobs WHERE job_id = ?", (target_job["job_id"],)).fetchone()
        binding = conn.execute(
            "SELECT provider, status, metadata_json FROM scrape_bindings "
            "WHERE revision_id = 'rev-target-scrape'"
        ).fetchone()
        owners = conn.execute(
            "SELECT work_id FROM provider_bindings WHERE provider = 'tmdb' AND media_type = 'tv' AND provider_id = '99'"
        ).fetchall()

    assert job["status"] == "succeeded"
    assert job["last_error"] == ""
    assert binding["provider"] == "local"
    assert binding["status"] == "waiting_review"
    assert "已关联到另一部作品" in json.loads(binding["metadata_json"])["reason"]
    assert len(owners) == 1


def test_local_artwork_mode_materializes_episode_stills_as_v4_artifacts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.media_v4.jobs import completeness as completeness_module
    from app.media_v4.jobs import metadata_artifacts as artifacts_module
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "episode-artwork.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-episode-artwork", [_entry("a")])
    revisions.confirm("rev-episode-artwork")
    scrape = V4ScrapeService(database)
    job = scrape.enqueue_for_revision("rev-episode-artwork")[0]
    with database.connect() as conn:
        episode_id = conn.execute("SELECT episode_id FROM episodes").fetchone()[0]

    local_config = SimpleNamespace(artwork_storage_mode="local", tmdb_timeout=5, proxy_url=None)
    monkeypatch.setattr(artifacts_module, "load_config", lambda: local_config)
    monkeypatch.setattr(completeness_module, "load_config", lambda: local_config)

    downloaded: list[tuple[str, str]] = []
    download_clients: list[object] = []

    def fake_download(url, path, *, client=None):
        assert client is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
        downloaded.append((url, str(path)))
        download_clients.append(client)
        return "image-digest"

    monkeypatch.setattr(artifacts_module, "_download_artwork", fake_download)
    mirror_root = tmp_path / "mirror"
    scrape.process(
        job["job_id"],
        lambda _target: {
            **_ready_metadata(),
            "episode_mappings": [{
                "episode_id": episode_id,
                "still_url": "https://image.tmdb.org/t/p/w500/still.jpg",
            }],
        },
        mirror_root=mirror_root,
    )

    with database.connect() as conn:
        binding = conn.execute(
            "SELECT metadata_json FROM scrape_bindings WHERE revision_id = 'rev-episode-artwork'"
        ).fetchone()
        episode_thumb = conn.execute(
            "SELECT target_path FROM artifacts WHERE revision_id = 'rev-episode-artwork' "
            "AND artifact_type = 'episode_thumb'"
        ).fetchone()

    metadata = json.loads(binding["metadata_json"])
    local_thumb = metadata["episode_mappings"][0]["local_thumb_path"]
    assert episode_thumb is not None
    assert episode_thumb["target_path"] == local_thumb
    assert local_thumb.endswith("Season 01\\S01E01-thumb.jpg")
    assert ("https://image.tmdb.org/t/p/w500/still.jpg", local_thumb) in downloaded
    assert len({id(client) for client in download_clients}) == 1
    assert Path(local_thumb).read_bytes() == b"image"


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
