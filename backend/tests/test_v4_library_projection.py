"""V4 Library Projection 合同。"""

from __future__ import annotations


def _entry(evidence_id: str):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-projection",
        root_id="root-projection",
        source_key=evidence_id,
        relative_path=f"Show/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://{evidence_id}",
        fingerprint=f"sha256:{evidence_id}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        year_candidate=2024,
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    return evidence, facts


def test_projection_has_one_card_and_counts_all_assets(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "projection.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("1080p"), _entry("2160p")])
    revisions.confirm("rev-1")

    snapshot = V4LibraryProjection(database).rebuild()

    assert len(snapshot.cards) == 1
    assert snapshot.cards[0]["title"] == "Show"
    assert snapshot.cards[0]["media_type"] == "tv"
    assert snapshot.cards[0]["episode_count"] == 1
    assert snapshot.cards[0]["asset_count"] == 2


def test_projection_can_be_deleted_and_rebuilt_from_sqlite_authority(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "projection-rebuild.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("a")])
    revisions.confirm("rev-1")
    projection = V4LibraryProjection(database)
    first = projection.rebuild()
    with database.connect() as conn:
        conn.execute("DELETE FROM library_cards")
        conn.execute("DELETE FROM library_generations")
        conn.execute("DELETE FROM v4_meta WHERE key = 'current_library_generation'")
    second = projection.rebuild()

    assert second.digest == first.digest
    assert second.cards == first.cards


def test_same_work_across_revisions_reuses_work_episode_and_asset_identity(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "stable-identities.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("first")])
    revisions.confirm("rev-1")
    revisions.create_draft("rev-2", [_entry("second")])
    revisions.confirm("rev-2")

    with database.connect() as conn:
        works = conn.execute("SELECT work_id FROM works").fetchall()
        episodes = conn.execute("SELECT episode_id FROM episodes").fetchall()
        assets = conn.execute("SELECT asset_id FROM assets").fetchall()
        card = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM revision_bindings rb
            JOIN works w ON w.work_id = rb.work_id
            WHERE rb.revision_id IN ('rev-1', 'rev-2')
            """
        ).fetchone()

    assert len(works) == 1
    assert len(episodes) == 1
    assert len(assets) == 2
    assert card["count"] == 2


def test_title_change_on_same_source_lineage_keeps_work_identity(tmp_path):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "title-change.db")
    database.initialize()
    revisions = V4RevisionService(database)

    def entry(revision: str, title: str):
        evidence = SourceEvidence(
            evidence_id=f"evidence-{revision}",
            scan_id=f"scan-{revision}",
            root_id="root-title-change",
            source_key=f"Show/{revision}.mkv",
            relative_path=f"Show/{revision}.mkv",
            entry_kind="video",
            provider="local",
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{revision}",
            evidence_id=evidence.evidence_id,
            parser_version="fixture",
            work_title=title,
            title_candidates=(title,),
            year_candidate=2024,
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=1,
        )
        return evidence, facts

    revisions.create_draft("rev-title-1", [entry("one", "Old Title")])
    revisions.confirm("rev-title-1")
    revisions.create_draft("rev-title-2", [entry("two", "New Title")])
    revisions.confirm("rev-title-2")

    with database.connect() as conn:
        works = conn.execute("SELECT work_id, preferred_title FROM works").fetchall()

    assert len(works) == 1
    assert works[0]["preferred_title"] == "New Title"


def test_projection_uses_latest_scrape_metadata_and_real_sources(tmp_path):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "projection-metadata.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-metadata", [_entry("ev-meta")])
    service.confirm("rev-metadata")
    scrape_job = next(job for job in service.list_jobs("rev-metadata") if job["job_type"] == "scrape_work")
    V4ScrapeService(database).process(
        scrape_job["job_id"],
        lambda _target: {
            "provider": "tmdb",
            "provider_id": "42",
            "title": "Online Title",
            "original_title": "Original",
            "plot": "Plot",
            "rating": 8.4,
            "genres": ["Animation"],
            "poster_url": "https://image.tmdb.org/t/p/w780/poster.jpg",
        },
    )

    card = V4LibraryProjection(database).rebuild().cards[0]
    assert card["title"] == "Online Title"
    assert card["metadata"]["original_title"] == "Original"
    assert card["metadata"]["sources"] == ["local"]


def test_projection_checks_completeness_against_selected_scrape_revision(tmp_path, monkeypatch):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "projection-revision-pair.db")
    database.initialize()
    revisions = V4RevisionService(database)

    def entry(root_id: str, scan_id: str, suffix: str):
        evidence = SourceEvidence(
            evidence_id=f"ev-{suffix}",
            scan_id=scan_id,
            root_id=root_id,
            source_key=f"Show/{suffix}.mkv",
            relative_path=f"Show/{suffix}.mkv",
            entry_kind="video",
            provider="local",
            source_locator=f"local://{suffix}",
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{suffix}",
            evidence_id=evidence.evidence_id,
            parser_version="fixture",
            work_title="Show",
            title_candidates=("Show",),
            year_candidate=2024,
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=1,
        )
        return evidence, facts

    revisions.create_draft("rev-a", [entry("root-a", "scan-a", "a")])
    revisions.confirm("rev-a")
    revisions.create_draft("rev-b", [entry("root-b", "scan-b", "b")])
    revisions.confirm("rev-b")
    with database.connect() as conn:
        work_id = str(conn.execute("SELECT work_id FROM works").fetchone()[0])
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'tmdb', ?, ?, 'confirmed', ?, ?)
            """,
            (
                "binding-a",
                "rev-a",
                work_id,
                "1",
                '{"title":"A","metadata_state":"ready"}',
                "2026-09-09T00:00:00+00:00",
                "2026-09-09T02:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'tmdb', ?, ?, 'failed', ?, ?)
            """,
            (
                "binding-b",
                "rev-b",
                work_id,
                "2",
                '{"title":"B","metadata_state":"failed"}',
                "2026-09-09T01:00:00+00:00",
                "2026-09-09T01:00:00+00:00",
            ),
        )

    checked: list[str] = []

    def capture(_database, *, revision_id, work_id, metadata):
        del work_id, metadata
        checked.append(revision_id)
        return True, []

    monkeypatch.setattr(
        "app.media_v4.jobs.completeness.assess_persisted_completeness",
        capture,
    )

    card = V4LibraryProjection(database).rebuild().cards[0]

    assert card["metadata"]["title"] == "A"
    assert checked == ["rev-a"]


def test_current_reads_generation_and_cards_from_one_snapshot(tmp_path, monkeypatch):
    """读 generation 与读 cards 之间发生并发 rebuild 时，不能返回空媒体墙。

    并发 rebuild 的收尾是 `DELETE FROM library_generations WHERE generation_id != ?`，
    而 `library_cards` 是 ON DELETE CASCADE。没有读事务时（sqlite3 默认 autocommit，
    每条 SELECT 各看一次最新提交），cards 查询会落在旧 generation 已被删除之后，
    返回空集 —— `/api/library` 的 works 直接来自 cards，于是媒体墙在一切正常的情况下
    瞬间清空，而脏标记已被对方清掉，`ensure_current()` 也不会补重建。
    """

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.library import V4LibraryProjection
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "snapshot.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("1080p")])
    revisions.confirm("rev-1")
    projection = V4LibraryProjection(database)
    assert projection.rebuild().cards, "前置条件：至少有一张已发布的卡"
    assert projection.current().cards

    real_open_connection = V4Database.open_connection
    state = {"armed": True}

    class _RacingConnection:
        """读到 cards 之前先让另一个连接提交一次 rebuild（模拟并发收尾）。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if state["armed"] and "FROM library_cards" in str(sql):
                state["armed"] = False
                V4LibraryProjection(database).rebuild()
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(
        V4Database,
        "open_connection",
        lambda self: _RacingConnection(real_open_connection(self)),
    )

    snapshot = projection.current()

    assert snapshot is not None
    assert snapshot.cards, "读事务必须保证 generation 与 cards 来自同一快照"

    # 反证：同样的竞态下**不**包读事务会返回空 cards——说明本用例确实在检验该修复，
    # 而不是靠运气通过。
    state["armed"] = True
    with database.connect() as conn:
        bare = V4LibraryProjection._read_current(conn)

    assert bare is not None
    assert bare.cards == ()
