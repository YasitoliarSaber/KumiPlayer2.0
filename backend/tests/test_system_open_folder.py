"""Folder actions must keep V4 playback and mirror locations distinct."""

from __future__ import annotations

from pathlib import Path

from app.api import system
from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.jobs.paths import work_directory_name
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService


def _confirmed_asset(database: V4Database, tmp_path) -> tuple[str, str, Path]:
    playback_file = tmp_path / "mounted-media" / "Show.S01E01.mkv"
    playback_file.parent.mkdir()
    playback_file.write_bytes(b"fixture")
    evidence = SourceEvidence(
        evidence_id="evidence-open-folder",
        scan_id="scan-open-folder",
        root_id="root-open-folder",
        source_key=playback_file.name,
        relative_path=playback_file.name,
        entry_kind="video",
        provider="local",
        source_locator=str(playback_file),
        playback_locator=str(playback_file),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-open-folder",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    revisions = V4RevisionService(database)
    revisions.create_draft("revision-open-folder", [(evidence, facts)])
    revisions.confirm("revision-open-folder")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings LIMIT 1"
        ).fetchone()
    return str(row["work_id"]), str(row["episode_id"]), playback_file


def test_open_mirror_folder_uses_v4_work_mirror_directory(tmp_path, monkeypatch):
    database = V4Database(tmp_path / "library.db")
    database.initialize()
    work_id, episode_id, playback_file = _confirmed_asset(database, tmp_path)
    mirror_root = tmp_path / "mirror"
    mirror_folder = mirror_root / work_directory_name(work_id)
    mirror_folder.mkdir(parents=True)

    monkeypatch.setattr(system, "get_database", lambda: database)
    monkeypatch.setattr(system, "get_mirror_root", lambda: mirror_root, raising=False)
    opened: list[Path] = []
    monkeypatch.setattr(system, "_open_folder", opened.append)

    result = system.open_folder(system.OpenFolderRequest(
        work_id=work_id,
        episode_id=episode_id,
        folder_type="mirror",
        open=True,
    ))

    assert result["exists"] is True
    assert result["folder_path"] == str(mirror_folder)
    assert result["folder_path"] != str(playback_file.parent)
    assert opened == [mirror_folder]


def test_open_video_folder_keeps_using_asset_playback_directory(tmp_path, monkeypatch):
    database = V4Database(tmp_path / "library.db")
    database.initialize()
    work_id, episode_id, playback_file = _confirmed_asset(database, tmp_path)
    mirror_root = tmp_path / "mirror"
    (mirror_root / work_directory_name(work_id)).mkdir(parents=True)

    monkeypatch.setattr(system, "get_database", lambda: database)
    monkeypatch.setattr(system, "get_mirror_root", lambda: mirror_root, raising=False)

    result = system.open_folder(system.OpenFolderRequest(
        work_id=work_id,
        episode_id=episode_id,
        folder_type="video",
        open=False,
    ))

    assert result["exists"] is True
    assert result["folder_path"] == str(playback_file.parent)
