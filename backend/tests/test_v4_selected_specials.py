"""明确小数集/第零集与季度内 OVA/OAD 的离线准入回归。"""
from pathlib import PurePosixPath

import pytest

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.parser import V4Parser


def parse(path):
    evidence = SourceEvidence(
        evidence_id=path, scan_id="special-fixture", root_id="offline-root",
        source_key=path, relative_path=path, entry_kind="video", provider="local",
        playback_locator=f"fixture://{path}",
    )
    return evidence, V4Parser().parse(evidence)


@pytest.mark.parametrize("name", [
    "Show - 09.5.mkv", "Show.S01E14.5.mkv", "Show [14.5][1080p].mkv",
    "Show.S01E00.mkv", "Show - 00.mkv", "Show 第0集.mkv",
    "Show OVA01.mkv", "Show OAD02.mkv", "Show [14(OVA)].mkv", "Show OVA.mkv",
])
def test_explicit_selected_special_is_playable_with_original_name(name):
    _, facts = parse(f"Show/Season 1/{name}")
    assert facts.is_importable and not facts.is_auxiliary
    assert facts.group_type == "special"
    assert facts.episode_candidate is None
    assert facts.absolute_episode_candidate is None
    assert facts.episode_title == PurePosixPath(name).stem


@pytest.mark.parametrize("path", [
    "Show/Season 1/Show S01E01 [AAC 5.1][23.976fps].mkv",
    "Show/Season 1/Show S01E02 v1.5.mkv",
    "Show/Season 1/Show S01E03 [H264.50fps].mkv",
])
def test_technical_decimal_does_not_become_special(path):
    _, facts = parse(path)
    assert facts.group_type == "season"
    assert facts.is_importable


@pytest.mark.parametrize("path", [
    "Show/Season 1/Show NCOP 09.5.mkv", "Show/Season 1/Show SP01.mkv",
    "Show/Season 1/Show [14.5][PV].mkv", "Show/Specials/Show - 花絮.mkv",
])
def test_other_excluded_material_stays_excluded(path):
    assert not parse(path)[1].is_importable


def test_selected_specials_confirm_materialize_and_return_original_titles(tmp_path, monkeypatch):
    from app.api import library_v4
    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    paths = ["Show/Season 1/Show.S01E01.mkv", "Show/Season 2/Show.S02E01.mkv",
             "Show/Season 1/Show - 09.5.mkv", "Show/Season 2/Show - 09.5.mkv",
             "Show/Season 1/Show.S01E00.mkv", "Show/Season 1/Show OVA01.mkv",
             "Show/Season 1/Show - 14.5.mkv", "Show/Season 1/Show OAD01.mkv",
             "Show/Season 1/Show OVA.mkv", "Show/Season 1/Show - 14.05.mkv"]
    entries = [parse(path) for path in paths]
    database = V4Database(tmp_path / "specials.db")
    database.initialize()
    service = V4RevisionService(database)
    draft = service.create_draft("special-fixture", entries)
    assert len(draft.works) == 1
    assert len(draft.episodes) == 10
    specials = [e for e in draft.episodes if e.season_kind == "special"]
    assert len(specials) == 8
    assert len({e.special_number for e in specials}) == 8
    service.confirm("special-fixture")
    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works").fetchone()[0]
        job_id = conn.execute("SELECT job_id FROM jobs WHERE job_type='materialize_mirror'").fetchone()[0]
    result = V4MirrorMaterializer(database).process(job_id, tmp_path / "mirror")
    assert result.status == "succeeded"
    assert len(list((tmp_path / "mirror").rglob("*.strm"))) == 10
    monkeypatch.setattr(library_v4, "get_database", lambda: database)
    detail = library_v4.get_work_detail(work_id)
    special_rows = [e for e in detail["episodes"] if e["kind"] == "special"]
    assert all(e["title"] for e in special_rows)
    assert {e["original_filename"] for e in special_rows} == {PurePosixPath(p).name for p in paths[2:]}
