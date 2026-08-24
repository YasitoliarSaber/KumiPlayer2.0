"""P-001 第三次返工回归（7.10 结构性缺口）。

覆盖：provider primary/original/alias 候选事实与评分、sidecar NFO 内容
身份解析及不可达降级、NFO 不改变本地季/集号。
"""

from __future__ import annotations

from dataclasses import replace

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.resolution.candidates import WorkCandidate


def _entry(evidence_id: str, *, work_title: str, year: int = 2024, relative_path: str = ""):
    relative = relative_path or f"{work_title}/Season 1/{work_title}.S01E01.mkv"
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-rw3",
        root_id="root-rw3",
        source_key=evidence_id,
        relative_path=relative,
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=work_title,
        title_candidates=(work_title,),
        year_candidate=year,
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        confidence="high",
    )
    return evidence, facts


def _patch_database(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "p001-rw3.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    return database


def _search_returning(*candidates: WorkCandidate):
    def search(work_key, queries, year, media_type):
        return [
            replace(candidate, work_key=work_key, year=candidate.year or year)
            for candidate in candidates
        ]

    return search


# ---------------------------------------------------------------------------
# R11：本地英文标题只匹配 provider original title。
# ---------------------------------------------------------------------------


def test_local_english_title_matches_provider_original_title(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    search = _search_returning(WorkCandidate(
        work_key="x", provider="tmdb", provider_id="42", media_type="tv",
        title="摇曳露营", original_title="Yuru Camp", year=2024,
        evidence="online_search", confidence="medium", status="proposed",
    ))
    graph = service.create_draft("rev-en2", [_entry("en2", work_title="Yuru Camp")], candidate_search=search)

    assert len(graph.works) == 1
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider, provider_id, status, original_title FROM revision_work_candidates "
            "WHERE revision_id = 'rev-en2'"
        ).fetchall()
    assert any(
        row["provider"] == "tmdb" and row["provider_id"] == "42" and row["status"] == "confirmed"
        for row in rows
    )
    assert any(row["original_title"] == "Yuru Camp" for row in rows)


# ---------------------------------------------------------------------------
# R12：本地中文标题只匹配 provider alias。
# ---------------------------------------------------------------------------


def test_local_chinese_title_matches_provider_alias(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    service = V4RevisionService(database)
    search = _search_returning(WorkCandidate(
        work_key="x", provider="tmdb", provider_id="43", media_type="tv",
        title="Yuru Camp", original_title="Yuru Camp", aliases=("摇曳露营",),
        year=2024, evidence="online_search", confidence="medium", status="proposed",
    ))
    graph = service.create_draft("rev-zh2", [_entry("zh2", work_title="摇曳露营")], candidate_search=search)

    assert len(graph.works) == 1
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider, provider_id, status FROM revision_work_candidates "
            "WHERE revision_id = 'rev-zh2' AND provider_id = '43'"
        ).fetchall()
    assert rows and rows[0]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# R14：模糊视频名 + 可读 sidecar NFO TMDB ID → 确认前正确绑定，且不覆盖本地编号。
# ---------------------------------------------------------------------------


def test_reachable_sidecar_nfo_provider_id_binds_without_overriding_local_numbers(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    work_root = tmp_path / "Show (2024)"
    nfo_file = work_root / "tvshow.nfo"
    nfo_file.parent.mkdir(parents=True)
    nfo_file.write_text(
        '<?xml version="1.0" encoding="utf-8"?><tvshow>'
        "<title>摇曳露营</title><originaltitle>Yuru Camp</originaltitle>"
        '<uniqueid type="tmdb" default="true">42</uniqueid>'
        "</tvshow>",
        encoding="utf-8",
    )
    video_evidence, video_facts = _entry(
        "video-r14",
        work_title="Show",
        relative_path="Show (2024)/Show.S01E01.mkv",
    )
    nfo_evidence = SourceEvidence(
        evidence_id="nfo-r14",
        scan_id="scan-rw3",
        root_id="root-rw3",
        source_key="nfo-r14",
        relative_path="Show (2024)/tvshow.nfo",
        entry_kind="metadata",
        provider="local",
        source_locator=str(nfo_file),
        playback_locator=str(nfo_file),
    )
    nfo_facts = V4Parser().parse(nfo_evidence)
    assert nfo_facts.tmdb_hint_id == 42, "可达 NFO 必须解析出 provider ID"

    service = V4RevisionService(database)
    graph = service.create_draft(
        "rev-nfo2",
        [(video_evidence, video_facts), (nfo_evidence, nfo_facts)],
        candidate_search=lambda *a, **k: [],
    )
    assert len(graph.works) == 1
    # NFO 不改变本地季/集号。
    assert any(
        ep.local_season_number == 1 and ep.local_episode_number == 1 for ep in graph.episodes
    )
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider, provider_id, status, evidence FROM revision_work_candidates "
            "WHERE revision_id = 'rev-nfo2'"
        ).fetchall()
    assert any(
        row["provider"] == "tmdb" and row["provider_id"] == "42"
        and row["status"] == "confirmed" and "sidecar_nfo_provider" in row["evidence"]
        for row in rows
    )

    service.confirm("rev-nfo2")
    with database.connect() as conn:
        binding = conn.execute(
            "SELECT provider, provider_id FROM provider_bindings WHERE provider = 'tmdb'"
        ).fetchone()
        episodes = conn.execute(
            "SELECT s.local_season_number, e.local_episode_number FROM episodes e "
            "JOIN seasons s ON s.season_id = e.season_id"
        ).fetchall()
    assert binding is not None and binding["provider_id"] == "42"
    assert [(row["local_season_number"], row["local_episode_number"]) for row in episodes] == [(1, 1)]


# ---------------------------------------------------------------------------
# R15：不可达 NFO 不产生虚构 ID。
# ---------------------------------------------------------------------------


def test_unreachable_sidecar_nfo_does_not_fabricate_provider_id(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    database = _patch_database(tmp_path, monkeypatch)
    missing = tmp_path / "missing" / "tvshow.nfo"  # 不创建文件
    nfo_evidence = SourceEvidence(
        evidence_id="nfo-missing",
        scan_id="scan-rw3",
        root_id="root-rw3",
        source_key="nfo-missing",
        relative_path="Show (2024)/tvshow.nfo",
        entry_kind="metadata",
        provider="local",
        source_locator=str(missing),
        playback_locator=str(missing),
    )
    nfo_facts = V4Parser().parse(nfo_evidence)
    assert nfo_facts.tmdb_hint_id is None, "不可达 NFO 不得伪造 provider ID"
    assert "tvshow" in nfo_facts.title_candidates

    video_evidence, video_facts = _entry(
        "video-r15",
        work_title="Show",
        relative_path="Show (2024)/Show.S01E01.mkv",
    )
    service = V4RevisionService(database)
    service.create_draft(
        "rev-nfo3",
        [(video_evidence, video_facts), (nfo_evidence, nfo_facts)],
        candidate_search=lambda *a, **k: [],
    )
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT provider, provider_id FROM revision_work_candidates WHERE revision_id = 'rev-nfo3'"
        ).fetchall()
    assert all(row["provider_id"] != "42" for row in rows)
