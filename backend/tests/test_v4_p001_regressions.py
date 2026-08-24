"""P-001 阶段 1：真实失败回归，先于生产代码修改。

这些用例直接锁定 7.2/7.3 中确认的真实数据缺陷：Season 1 通用容器 Work、
外传被父系列吸收、Episode 跨作品 Asset 串挂、local_only 冒充成功、
投影无完整性门控、季度统计缺失、来源根上下文未进入解析器。
"""

from __future__ import annotations

from dataclasses import replace

import pytest


def _pair(
    evidence_id: str,
    *,
    work_title: str = "Show",
    series_group: str = "",
    relation_type: str = "",
    card_type: str = "",
    year: int | None = 2024,
    tmdb_id: int | None = None,
    season: int = 1,
    episode: int = 1,
    relative_path: str = "",
    provider: str = "local",
):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    relative = relative_path or f"{work_title}/Season 1/{work_title}.S01E{episode:02d}.mkv"
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id=f"scan-{evidence_id}",
        root_id=f"root-{evidence_id}",
        source_key=evidence_id,
        relative_path=relative,
        entry_kind="video",
        provider=provider,
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=work_title,
        title_candidates=(work_title, series_group) if series_group else (work_title,),
        series_group=series_group,
        relation_type=relation_type,
        card_type=card_type,
        year_candidate=year,
        media_type="tv",
        group_type="season",
        season_candidate=season,
        episode_candidate=episode,
        tmdb_hint_id=tmdb_id,
        tmdb_hint_type="tv" if tmdb_id else "",
        confidence="high",
    )
    return evidence, facts


def _resolve(entries):
    from app.media_v4.resolution.resolver import MediaResolver

    return MediaResolver().resolve(entries)


# ---------------------------------------------------------------------------
# 7.2.3 / 7.3.B：通用容器标题不得成为 Work，必须生成 review issue。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["Season 1", "S01", "Specials", "SPs", "12", "E01-E24", "01-24", "动画", "新番"])
def test_generic_container_title_must_generate_review_issue(title):
    entries = [
        _pair(
            "ev-generic",
            work_title=title,
            series_group="",
            relative_path=f"{title}/Show.S01E01.mkv",
        )
    ]

    graph = _resolve(entries)

    assert graph.works == ()
    assert any(issue.code == "generic_container_title" for issue in graph.issues)


def test_generic_container_with_real_filename_recovers_stable_title_without_issue():
    """Season 1 是路径容器但文件名能提供稳定标题时，应自动恢复，不制造人工项。"""

    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.parsing.parser import V4Parser

    evidence = SourceEvidence(
        evidence_id="ev-recover",
        scan_id="scan-recover",
        root_id="root-recover",
        source_key="ev-recover",
        relative_path="Season 1/古诺希亚.S01E01.mkv",
        entry_kind="video",
        provider="pan115",
    )
    facts = V4Parser().parse(evidence)

    assert facts.work_title == "古诺希亚"
    assert facts.needs_review is False


# ---------------------------------------------------------------------------
# 7.2.5 / 7.3.C：独立外传不得被父系列吸收，Episode 不得跨作品串挂 Asset。
# ---------------------------------------------------------------------------


def test_spinoff_work_title_is_not_absorbed_by_series_group():
    yuru = _pair("ev-yuru", work_title="Yuru Camp", series_group="Yuru Camp", season=1, episode=1)
    heya = _pair(
        "ev-heya",
        work_title="Heya Camp",
        series_group="Yuru Camp",
        relation_type="spin_off",
        card_type="standalone",
        season=1,
        episode=1,
        relative_path="Heya Camp/Season 1/Heya Camp.S01E01.mkv",
    )

    graph = _resolve([yuru, heya])

    assert {work.preferred_title for work in graph.works} == {"Yuru Camp", "Heya Camp"}
    yuru_episode = next(
        ep for ep in graph.episodes if ep.work_key.startswith("title:yuru camp")
    )
    assert "ev-heya" not in yuru_episode.asset_evidence_ids


def test_same_provider_identity_merges_cross_language_titles_into_one_work():
    entries = [
        _pair("ev-zh", work_title="摇曳露营", tmdb_id=42, season=1, episode=1),
        _pair("ev-en", work_title="Yuru Camp", tmdb_id=42, season=2, episode=1),
    ]

    graph = _resolve(entries)

    assert len(graph.works) == 1
    assert len({ep.local_season_number for ep in graph.episodes}) == 2


def test_different_works_with_same_episode_numbers_do_not_share_an_episode():
    """两个独立作品即使共享 series_group，也不能出现在同一 Episode 的 Asset 列表。"""

    entries = [
        _pair("ev-a", work_title="Show A", series_group="Show", season=1, episode=1),
        _pair("ev-b", work_title="Show B", series_group="Show", season=1, episode=1),
    ]

    graph = _resolve(entries)

    assert len(graph.works) == 2
    assert all(len(episode.asset_evidence_ids) == 1 for episode in graph.episodes)


# ---------------------------------------------------------------------------
# 7.2.2 / 7.3.E：local_only 不得成为 ready/confirmed 刮削绑定。
# ---------------------------------------------------------------------------


def test_local_only_metadata_must_not_become_confirmed_binding_or_succeeded_job(tmp_path):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "p001-scrape.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-p001", [_pair("ev-p001", work_title="Show")])
    revision_service.confirm("rev-p001")
    scrape = V4ScrapeService(database)
    job = scrape.enqueue_for_revision("rev-p001")[0]

    with pytest.raises(RuntimeError, match="元数据尚未就绪"):
        scrape.process(
            job["job_id"],
            lambda _work: {
                "provider": "local",
                "provider_id": "work-local",
                "metadata_state": "local_only",
            },
        )

    with database.connect() as conn:
        binding = conn.execute(
            "SELECT provider, provider_id, status FROM scrape_bindings WHERE revision_id = 'rev-p001'"
        ).fetchone()
        job_status = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job["job_id"],)).fetchone()
        provider = conn.execute("SELECT COUNT(*) FROM provider_bindings WHERE work_id = (SELECT work_id FROM works LIMIT 1)").fetchone()[0]
    assert binding is None
    assert provider == 0
    assert job_status["status"] == "failed"


# ---------------------------------------------------------------------------
# 7.2.2 / 7.3.F：投影必须只发布 ready 作品。
# ---------------------------------------------------------------------------


def test_projection_excludes_local_only_works_from_formal_library(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "p001-proj.db")
    database.initialize()
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-p001-proj", [_pair("ev-proj", work_title="Show")])
    revision_service.confirm("rev-p001-proj")
    runner = V4JobRunner(database)
    mirror_job = next(
        job for job in revision_service.list_jobs("rev-p001-proj")
        if job["job_type"] == "materialize_mirror"
    )
    runner.process_job(mirror_job["job_id"], mirror_root=tmp_path / "mirror")
    scrape_job = next(
        job for job in revision_service.list_jobs("rev-p001-proj")
        if job["job_type"] == "scrape_work"
    )
    runner.scrape.process(
        scrape_job["job_id"],
        lambda _work: {"provider": "local", "provider_id": "local", "metadata_state": "local_only"},
        mirror_root=tmp_path / "mirror",
    )

    snapshot = V4LibraryProjection(database).rebuild()

    assert all(card["metadata"].get("metadata_state") != "local_only" for card in snapshot.cards)


# ---------------------------------------------------------------------------
# 7.2.8 / 7.3.G：多季作品必须报告真实常规季数，S00 不计入。
# ---------------------------------------------------------------------------


def test_projection_reports_regular_season_count_excluding_specials(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "p001-season.db")
    database.initialize()
    entries = []
    for season, episode in ((1, 1), (2, 1)):
        evidence, facts = _pair(
            f"ev-s{season}",
            work_title="Show",
            season=season,
            episode=episode,
            relative_path=f"Show/Season {season}/Show.S0{season}E0{episode}.mkv",
        )
        evidence = replace(evidence, root_id="root-season", scan_id="scan-season")
        entries.append((evidence, facts))
    special_evidence = SourceEvidence(
        evidence_id="ev-sp",
        scan_id="scan-season",
        root_id="root-season",
        source_key="ev-sp",
        relative_path="Show/Specials/Show.SP01.mkv",
        entry_kind="video",
        provider="local",
    )
    special_facts = ParsedFacts(
        parsed_fact_id="facts-sp",
        evidence_id="ev-sp",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="special",
        season_candidate=0,
        special_candidate=True,
        special_number=1,
    )
    entries.append((special_evidence, special_facts))
    revision_service = V4RevisionService(database)
    revision_service.create_draft("rev-p001-season", entries)
    revision_service.confirm("rev-p001-season")

    snapshot = V4LibraryProjection(database).rebuild()

    card = snapshot.cards[0]
    assert card["regular_season_count"] == 2
    assert card["special_season_count"] == 1


# ---------------------------------------------------------------------------
# 7.2.4 / 7.3.A：来源根上下文必须进入解析器（OpenList 选中作品目录）。
# ---------------------------------------------------------------------------


def test_parser_uses_root_container_to_avoid_season_directory_becoming_work():
    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.parsing.parser import V4Parser

    evidence = SourceEvidence(
        evidence_id="ev-root",
        scan_id="scan-root",
        root_id="root-root",
        source_key="ev-root",
        relative_path="Season 1/古诺希亚.S01E01.mkv",
        entry_kind="video",
        provider="pan115",
    )
    facts = V4Parser().parse(evidence, root_container="古诺希亚")

    assert facts.work_title == "古诺希亚"
    assert facts.series_group in {"", "古诺希亚"}
