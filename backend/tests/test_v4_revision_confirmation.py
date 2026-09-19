"""V4 Revision 草稿/确认状态机。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest


def _entry(title: str = "Show", evidence_id: str = "ev-revision"):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-revision",
        root_id="root-revision",
        source_key=evidence_id,
        relative_path=f"{title}/{evidence_id}.mkv" if title else f"unknown/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=title,
        title_candidates=(title,) if title else (),
        media_type="tv" if title else "",
        group_type="season" if title else "unknown",
        season_candidate=1 if title else None,
        episode_candidate=1 if title else None,
    )
    return evidence, facts


def test_confirmation_atomically_publishes_revision_and_outbox_jobs(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "revision.db")
    database.initialize()
    service = V4RevisionService(database)

    service.create_draft("rev-1", [_entry()])
    service.confirm("rev-1")

    assert service.get_status("rev-1") == "confirmed"
    jobs = service.list_jobs("rev-1")
    assert {job["job_type"] for job in jobs} == {
        "materialize_mirror",
        "scrape_work",
        "refresh_projection",
    }
    assert len({job["idempotency_key"] for job in jobs}) == len(jobs)


def test_confirmation_is_idempotent_and_does_not_duplicate_jobs(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "idempotent.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-1", [_entry()])

    service.confirm("rev-1")
    service.confirm("rev-1")

    assert len(service.list_jobs("rev-1")) == 3


def test_reused_provider_identity_keeps_existing_preferred_title_and_records_new_structure_as_alias(tmp_path):
    """较晚的目录标题不得重命名已确认 Work。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "title-provenance.db")
    database.initialize()
    service = V4RevisionService(database)

    first_evidence, first_facts = _entry("Yuru Camp", "ev-title-first")
    first_facts = replace(first_facts, tmdb_hint_id=76075, tmdb_hint_type="tv")
    service.create_draft("rev-title-first", [(first_evidence, first_facts)])
    service.confirm("rev-title-first")

    later_evidence, later_facts = _entry("Yuru Camp Movie", "ev-title-later")
    later_facts = replace(later_facts, tmdb_hint_id=76075, tmdb_hint_type="tv")
    service.create_draft("rev-title-later", [(later_evidence, later_facts)])
    service.confirm("rev-title-later")

    with database.connect() as conn:
        work = conn.execute("SELECT work_id, preferred_title FROM works").fetchone()
        aliases = {
            row["normalized_title"]
            for row in conn.execute(
                "SELECT normalized_title FROM work_aliases WHERE work_id = ?",
                (work["work_id"],),
            ).fetchall()
        }

    assert work["preferred_title"] == "Yuru Camp"
    assert "yuru camp movie" in aliases


def test_confirmation_does_not_reuse_bindings_created_earlier_in_same_revision(tmp_path):
    """同一批内新写入的季度目录绑定不能反向吞并另一个 Work。"""

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    def pair(
        evidence_id: str,
        relative_path: str,
        work_title: str,
        *,
        group_type: str,
        series_group: str = "",
        special: bool = False,
        season: int | None = None,
    ):
        evidence = SourceEvidence(
            evidence_id=evidence_id,
            scan_id="scan-binding-order",
            root_id="root-binding-order",
            source_key=relative_path,
            relative_path=relative_path,
            entry_kind="video",
            provider="baidu",
            ingest_method="directory_tree",
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{evidence_id}",
            evidence_id=evidence_id,
            parser_version="fixture",
            media_type="tv",
            group_type=group_type,
            work_title=work_title,
            series_group=series_group,
            card_type="main_series",
            title_candidates=(work_title,),
            season_candidate=season,
            episode_candidate=None if special else 1,
            special_candidate=special,
            special_number=1 if special else None,
        )
        return evidence, facts

    entries = {
        "main-special": pair(
            "ev-binding-main-special",
            "动画/Yuru Camp Season 2/SPs/Yuru Camp SP01.mkv",
            "Yuru Camp",
            group_type="special",
            series_group="Yuru Camp",
            special=True,
        ),
        # 该事实已经由 Parser 归入主系列，但它仍使用第二季目录，正是会
        # 被旧版“最近目录 binding”误写成主系列边界的场景。
        "season-two-special": pair(
            "ev-binding-season-special",
            "动画/Yuru Camp Season 2/SPs/Yuru Camp Season 2 SP01.mkv",
            "Yuru Camp",
            group_type="special",
            series_group="Yuru Camp",
            special=True,
        ),
        "season-two-episode": pair(
            "ev-binding-season",
            "动画/Yuru Camp Season 2/Yuru Camp Season 2 - 01.mkv",
            "Yuru Camp Season 2",
            group_type="season",
            season=2,
        ),
    }

    for order_index, order in enumerate(
        (
            ("main-special", "season-two-special", "season-two-episode"),
            ("season-two-special", "season-two-episode", "main-special"),
            ("season-two-episode", "main-special", "season-two-special"),
        )
    ):
        database = V4Database(tmp_path / f"binding-order-{order_index}.db")
        database.initialize()
        service = V4RevisionService(database)
        service.create_draft(
            f"rev-binding-order-{order_index}",
            [entries[label] for label in order],
            candidate_search=lambda *_args: [],
        )
        service.confirm(f"rev-binding-order-{order_index}")

        with database.connect() as conn:
            works = conn.execute(
                "SELECT preferred_title FROM works ORDER BY preferred_title"
            ).fetchall()

        assert [str(row["preferred_title"]) for row in works] == [
            "Yuru Camp",
            "Yuru Camp Season 2",
        ]


def test_ambiguous_existing_structural_bindings_block_confirmation(tmp_path):
    """同一稳定边界已属于多个旧 Work 时必须显式阻断确认。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "ambiguous-structural-binding.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO source_roots(
                root_id, provider, ingest_method, created_at, updated_at
            ) VALUES ('root-revision', 'local', 'local_scan', 'now', 'now')
            """
        )
        for work_id, identity_key in (
            ("old-work-a", "series:old-a:tv"),
            ("old-work-b", "series:old-b:tv"),
        ):
            conn.execute(
                """
                INSERT INTO works(
                    work_id, identity_key, work_type, preferred_title, created_at, updated_at
                ) VALUES (?, ?, 'series', ?, 'now', 'now')
                """,
                (work_id, identity_key, identity_key),
            )
            conn.execute(
                """
                INSERT INTO work_source_bindings(
                    work_id, root_id, structural_key, confidence, binding_source
                ) VALUES (?, 'root-revision', 'series:show:tv', 'medium', 'resolver')
                """,
                (work_id,),
            )

    evidence, facts = _entry("Show", "ev-ambiguous-structure")
    facts = replace(
        facts,
        card_type="main_series",
        relation_type="main",
        series_group="Show",
    )
    service = V4RevisionService(database)
    graph = service.create_draft(
        "rev-ambiguous-structure",
        [(evidence, facts)],
        candidate_search=lambda *_args: [],
    )

    # 复用歧义**不再阻断确认**：复用只是优化，歧义时改为不复用、照常建立新作品。
    assert not any(issue.code == "structural_identity_ambiguous" for issue in graph.issues)
    service.confirm("rev-ambiguous-structure")
    with database.connect() as conn:
        bound = {
            str(row["work_id"])
            for row in conn.execute(
                "SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id = 'rev-ambiguous-structure'"
            )
        }
    assert bound.isdisjoint({"old-work-a", "old-work-b"}), "歧义时不得复用旧的同名作品"


def test_override_rechecks_structural_identity_before_confirmation(tmp_path):
    """人工修正不能通过删除旧 issue 把结构歧义带入发布。"""

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "override-structural-identity.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-override-structure', 'local', 'local_scan', 'now', 'now')"
        )
        for work_id, identity_key in (
            ("old-work-a", "series:old-a:tv"),
            ("old-work-b", "series:old-b:tv"),
        ):
            conn.execute(
                "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
                "VALUES (?, ?, 'series', ?, 'now', 'now')",
                (work_id, identity_key, identity_key),
            )
            conn.execute(
                "INSERT INTO work_source_bindings(work_id, root_id, structural_key, confidence, binding_source) "
                "VALUES (?, 'root-override-structure', 'series:show:tv', 'medium', 'resolver')",
                (work_id,),
            )

    evidence = SourceEvidence(
        evidence_id="ev-override-structure",
        scan_id="scan-override-structure",
        root_id="root-override-structure",
        source_key="Show/Show.S01E01.mkv",
        relative_path="Show/Show.S01E01.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-override-structure",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        series_group="Show",
        card_type="main_series",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    service = V4RevisionService(database)
    service.create_draft(
        "rev-override-structure",
        [(evidence, facts)],
        candidate_search=lambda *_args: [],
    )

    graph = service.apply_override(
        "rev-override-structure",
        evidence.evidence_id,
        {"episode_candidate": 2},
    )

    # 人工修正后同样不因复用歧义阻断；歧义即不复用。
    assert not any(issue.code == "structural_identity_ambiguous" for issue in graph.issues)
    service.confirm("rev-override-structure")
    with database.connect() as conn:
        bound = {
            str(row["work_id"])
            for row in conn.execute(
                "SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id = 'rev-override-structure'"
            )
        }
    assert bound.isdisjoint({"old-work-a", "old-work-b"}), "歧义时不得复用旧的同名作品"


def test_preview_blocks_provider_rebinding_before_confirm_can_hit_unique_constraint(tmp_path):
    """同一结构 Work 改指向已属于另一 Work 的 Provider 身份必须留在第 2 步。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "provider-rebinding.db")
    database.initialize()
    service = V4RevisionService(database)

    first_evidence, first_facts = _entry("Show One", "ev-provider-one")
    service.create_draft(
        "rev-provider-one",
        [(first_evidence, replace(first_facts, tmdb_hint_id=101, tmdb_hint_type="tv"))],
    )
    service.confirm("rev-provider-one")

    second_evidence, second_facts = _entry("Show Two", "ev-provider-two")
    service.create_draft(
        "rev-provider-two",
        [(second_evidence, replace(second_facts, tmdb_hint_id=202, tmdb_hint_type="tv"))],
    )
    service.confirm("rev-provider-two")

    conflicting_evidence, conflicting_facts = _entry("Show One", "ev-provider-conflict")
    graph = service.create_draft(
        "rev-provider-conflict",
        [(conflicting_evidence, replace(conflicting_facts, tmdb_hint_id=202, tmdb_hint_type="tv"))],
    )

    assert any(issue.code == "provider_identity_conflict" for issue in graph.issues)
    # 不再拦截：身份冲突时静默跳过本次绑定，作品照常入库（用户要求永不拦截）。
    service.confirm("rev-provider-conflict")

    with database.connect() as conn:
        bindings = {
            (row["provider_id"], row["preferred_title"])
            for row in conn.execute(
                """
                SELECT pb.provider_id, w.preferred_title
                FROM provider_bindings pb
                JOIN works w ON w.work_id = pb.work_id
                ORDER BY pb.provider_id
                """
            ).fetchall()
        }
    assert bindings == {("101", "Show One"), ("202", "Show Two")}


def test_review_issues_do_not_block_confirmation(tmp_path):
    """提示类 issue 只作提示：确认照常进行（用户要求导入永不拦截）。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "blocked.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-1", [_entry(title="", evidence_id="ev-blocked")])

    service.confirm("rev-1")

    assert service.get_status("rev-1") == "confirmed"


def test_draft_does_not_publish_authoritative_media_graph(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "draft-isolation.db")
    database.initialize()
    service = V4RevisionService(database)

    service.create_draft("rev-draft", [_entry()])

    with database.connect() as conn:
        counts = {
            "works": conn.execute("SELECT COUNT(*) FROM works").fetchone()[0],
            "seasons": conn.execute("SELECT COUNT(*) FROM seasons").fetchone()[0],
            "episodes": conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "editions": conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0],
            "assets": conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            "revision_bindings": conn.execute("SELECT COUNT(*) FROM revision_bindings").fetchone()[0],
        }

    assert counts == {table: 0 for table in counts}


def test_unconfirmed_draft_cannot_change_confirmed_library_projection(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "draft-projection-isolation.db")
    database.initialize()
    service = V4RevisionService(database)

    service.create_draft("rev-confirmed", [_entry(evidence_id="episode-1")])
    service.confirm("rev-confirmed")
    before = V4LibraryProjection(database).rebuild()

    evidence = SourceEvidence(
        evidence_id="episode-2",
        scan_id="scan-draft-2",
        root_id="root-revision",
        source_key="episode-2",
        relative_path="Show/Show.S01E02.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-episode-2",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=2,
    )
    service.create_draft("rev-draft", [(evidence, facts)])
    after = V4LibraryProjection(database).rebuild()

    assert after.cards == before.cards


def test_confirmed_range_keeps_one_asset_bound_to_each_episode(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "range-confirm.db")
    database.initialize()
    service = V4RevisionService(database)
    evidence = SourceEvidence(
        evidence_id="range",
        scan_id="scan-range",
        root_id="root-range",
        source_key="range",
        relative_path="Show/Show.S01E01-E03.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-range",
        evidence_id="range",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
        episode_range=(1, 3),
    )

    service.create_draft("rev-range", [(evidence, facts)])
    service.confirm("rev-range")

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM episode_assets").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM revision_bindings WHERE revision_id = 'rev-range'"
        ).fetchone()[0] == 3


def test_draft_override_resolves_issue_without_mutating_parsed_facts(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "override.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-override", [_entry(title="", evidence_id="needs-fix")])

    with pytest.raises(ValueError, match="必须是字符串"):
        service.apply_override("rev-override", "needs-fix", {"work_title": 123})

    service.apply_override(
        "rev-override",
        "needs-fix",
        {
            "work_title": "Fixed Show",
            "title_candidates": ["Fixed Show"],
            "media_type": "tv",
        },
    )
    graph = service.apply_override(
        "rev-override",
        "needs-fix",
        {
            "group_type": "season",
            "season_candidate": 1,
            "episode_candidate": 1,
            "needs_review": False,
        },
    )
    assert graph.issues == ()
    service.confirm("rev-override")

    with database.connect() as conn:
        parsed = conn.execute(
            "SELECT work_title FROM parsed_facts WHERE evidence_id = 'needs-fix'"
        ).fetchone()
        work = conn.execute("SELECT preferred_title FROM works").fetchone()
        override = conn.execute(
            "SELECT overrides_json FROM revision_overrides WHERE revision_id = 'rev-override'"
        ).fetchone()
        binding = conn.execute(
            "SELECT decision_source, override_json FROM revision_bindings WHERE revision_id = 'rev-override'"
        ).fetchone()
    assert parsed["work_title"] == ""
    assert work["preferred_title"] == "Fixed Show"
    assert json.loads(override["overrides_json"])["season_candidate"] == 1
    assert binding["decision_source"] == "manual_override"
    assert json.loads(binding["override_json"])["work_title"] == "Fixed Show"

    with database.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE revision_overrides SET overrides_json = '{}' WHERE revision_id = 'rev-override'"
        )


def test_graph_digest_covers_asset_membership(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "digest.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-one", [_entry(evidence_id="asset-one")])
    service.create_draft(
        "rev-two",
        [_entry(evidence_id="asset-one"), _entry(evidence_id="asset-two")],
    )

    with database.connect() as conn:
        digests = {
            row["revision_id"]: row["graph_digest"]
            for row in conn.execute(
                "SELECT revision_id, graph_digest FROM import_revisions ORDER BY revision_id"
            )
        }
    assert digests["rev-one"] != digests["rev-two"]


def test_new_confirmed_revision_supersedes_same_root_without_unlocking_snapshot(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "supersede.db")
    database.initialize()
    service = V4RevisionService(database)
    first = _entry(evidence_id="old-asset")
    service.create_draft("rev-old", [first])
    service.confirm("rev-old")

    evidence, facts = _entry(evidence_id="new-asset")
    second = (evidence, replace(facts, episode_candidate=2))
    service.create_draft("rev-new", [second])
    service.confirm("rev-new")

    with database.connect() as conn:
        statuses = {
            row["revision_id"]: row["status"]
            for row in conn.execute("SELECT revision_id, status FROM import_revisions")
        }
        old_binding = conn.execute(
            "SELECT binding_id FROM revision_bindings WHERE revision_id = 'rev-old'"
        ).fetchone()[0]
    assert statuses == {"rev-old": "superseded", "rev-new": "confirmed"}
    card = V4LibraryProjection(database).rebuild().cards[0]
    assert card["episode_count"] == 1
    assert card["asset_count"] == 1

    captured = []
    scrape_job = next(
        job for job in service.list_jobs("rev-new") if job["job_type"] == "scrape_work"
    )
    from app.media_v4.jobs.scrape import V4ScrapeService

    V4ScrapeService(database).process(
        scrape_job["job_id"],
        lambda target: captured.append(target)
        or {"provider": "local", "provider_id": target["work_id"], "title": "Show"},
    )
    assert [episode["local_episode_number"] for episode in captured[0]["episodes"]] == [2]
    assert all(job["status"] == "cancelled" for job in service.list_jobs("rev-old"))

    with database.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE revision_bindings SET confidence = 'low' WHERE binding_id = ?",
            (old_binding,),
        )
    with database.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE import_revisions SET status = 'draft' WHERE revision_id = 'rev-old'")


def test_confirming_a_newer_revision_requests_stop_for_old_running_jobs(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "supersede-running-jobs.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-old", [_entry(evidence_id="old-running")])
    service.confirm("rev-old")
    with database.connect() as conn:
        conn.execute(
            """
            UPDATE jobs SET status = 'running'
            WHERE revision_id = 'rev-old' AND job_type = 'materialize_mirror'
            """
        )

    evidence, facts = _entry(evidence_id="new-running")
    service.create_draft("rev-new", [(evidence, replace(facts, episode_candidate=2))])
    service.confirm("rev-new")

    with database.connect() as conn:
        jobs = {
            row["job_type"]: dict(row)
            for row in conn.execute(
                "SELECT job_type, status, cancel_requested FROM jobs WHERE revision_id = 'rev-old'"
            )
        }
    assert jobs["materialize_mirror"] == {"job_type": "materialize_mirror", "status": "running", "cancel_requested": 1}
    assert jobs["scrape_work"]["status"] == "cancelled"
    assert jobs["refresh_projection"]["status"] == "cancelled"


def test_loading_draft_replaces_stale_persisted_issues_with_current_evaluation(tmp_path):
    """升级识别规则后，旧草稿的红色问题状态必须随预览一并刷新。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "stale-draft.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-stale", [_entry("Show")])
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO revision_issues(revision_id, issue_id, code, evidence_id, message)
            VALUES ('rev-stale', 'stale-issue', 'work_identity_conflict', 'ev-revision', 'old issue')
            """
        )

    from app.media_v4.projection.source_libraries import list_source_cards

    card = list_source_cards(database)[0]
    assert card["attention_count"] == 0
    assert card["work_count"] == 1
    with database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM revision_issues WHERE revision_id = 'rev-stale'"
        ).fetchone()[0] == 1  # 来源卡只读统计，真正的预览操作才刷新快照。

    graph = service.load_draft_graph("rev-stale")

    assert graph.issues == ()
    with database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM revision_issues WHERE revision_id = 'rev-stale'"
        ).fetchone()[0] == 0


def test_preview_refresh_excludes_concurrent_confirmation_writes(tmp_path, monkeypatch):
    """预览读取与回写之间，另一连接不能抢先确认并发布候选。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    path = tmp_path / "preview-race.db"
    database = V4Database(path)
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-race", [_entry()])
    evaluate = service._evaluate_draft

    def competing_confirmation(*args, **kwargs):
        other = sqlite3.connect(path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute(
                    "UPDATE import_revisions SET status = 'confirmed' WHERE revision_id = 'rev-race'"
                )
        finally:
            other.rollback()
            other.close()
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(service, "_evaluate_draft", competing_confirmation)
    service.load_draft_graph("rev-race")
    monkeypatch.setattr(service, "_evaluate_draft", evaluate)
    service.confirm("rev-race")
    with database.connect() as conn:
        before = tuple(conn.execute(
            "SELECT status, graph_digest FROM import_revisions WHERE revision_id = 'rev-race'"
        ).fetchone())
    with pytest.raises(RuntimeError, match="draft"):
        service.load_draft_graph("rev-race")
    with database.connect() as conn:
        assert tuple(conn.execute(
            "SELECT status, graph_digest FROM import_revisions WHERE revision_id = 'rev-race'"
        ).fetchone()) == before
