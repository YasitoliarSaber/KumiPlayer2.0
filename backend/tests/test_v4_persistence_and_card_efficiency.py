"""O16：批量持久化与来源卡的读取效率（审查 C#11 / C#9）。

- `save_parsed_facts_bulk` 原先插入后把**全部** facts 回读并逐行重建对象（每行 6 次
  `json.loads`）来校验不可变性；插入的新行必然等于入参，回读它们纯属浪费。
- 来源卡原先全表扫描 `source_scans`（每次扫描 +1 行且从不清理）再在 Python 端分组，
  并为"没有 revision"的 root 另开连接做 `COUNT(*)`。
"""

from __future__ import annotations


def _facts(index: int, *, evidence_id: str, title: str = "Show"):
    from app.media_v4.domain.models import ParsedFacts

    return ParsedFacts(
        parsed_fact_id=f"facts-{index}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=title,
        title_candidates=(title,),
        year_candidate=2024,
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=index + 1,
    )


def _evidence(index: int):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return to_source_evidence(SourceEntry(
        root_id="root-eff",
        scan_id="scan-eff",
        provider="baidu",
        ingest_method="directory_tree",
        relative_path=f"Show/F{index}.mkv",
        source_key=f"Show/F{index}.mkv",
    ))


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "efficiency.db")
    database.initialize()
    return database


def _seed_scan_row(database, *, root_id: str = "root-eff", scan_id: str = "scan-eff") -> None:
    """source_evidence.scan_id 有外键，证据行必须挂在已登记的扫描下。"""

    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'baidu', 'directory_tree', 'now', 'now')",
            (root_id,),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES (?, ?, 1, 'completed', 'ready', 'now', 'now')",
            (scan_id, root_id),
        )


def _seed_evidence(database, count: int) -> list[str]:
    """先落证据行（parsed_facts.evidence_id 有外键），返回真实的证据 id 列表。

    注意 evidence_id 是由 adapters 派生的，不能自己编一个——第一次写这组用例时
    就是自编 id 导致外键失败。
    """

    from app.media_v4.persistence.repositories import V4Repository

    _seed_scan_row(database)
    evidence = [_evidence(index) for index in range(count)]
    V4Repository(database).save_scan_evidence_bulk(evidence)
    return [item.evidence_id for item in evidence]


def test_fresh_parsed_facts_import_does_not_reread_rows(tmp_path, monkeypatch):
    from app.media_v4.persistence.repositories import V4Repository

    database = _database(tmp_path)
    repository = V4Repository(database)
    evidence_ids = _seed_evidence(database, 300)
    reads: list[str] = []
    real = V4Repository._row_to_parsed_facts
    monkeypatch.setattr(
        V4Repository, "_row_to_parsed_facts", staticmethod(lambda row: reads.append(str(row["parsed_fact_id"])) or real(row))
    )

    repository.save_parsed_facts_bulk([
        _facts(index, evidence_id=evidence_ids[index]) for index in range(300)
    ])

    assert reads == [], f"全新导入不应回读并重建对象，实际回读 {len(reads)} 行"
    with database.connect() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM parsed_facts").fetchone()[0]) == 300


def test_resaving_the_same_facts_validates_only_existing_rows(tmp_path, monkeypatch):
    from app.media_v4.persistence.repositories import V4Repository

    database = _database(tmp_path)
    repository = V4Repository(database)
    evidence_ids = _seed_evidence(database, 50)
    facts = [_facts(index, evidence_id=evidence_ids[index]) for index in range(50)]
    repository.save_parsed_facts_bulk(facts)

    reads: list[str] = []
    real = V4Repository._row_to_parsed_facts
    monkeypatch.setattr(
        V4Repository, "_row_to_parsed_facts", staticmethod(lambda row: reads.append(str(row["parsed_fact_id"])) or real(row))
    )

    repository.save_parsed_facts_bulk(list(facts))  # 幂等重放

    assert len(reads) == 50, "只有已存在的 id 需要回读校验"


def test_changed_payload_for_an_existing_id_still_raises(tmp_path):
    import pytest
    from app.media_v4.persistence.repositories import V4Repository

    database = _database(tmp_path)
    repository = V4Repository(database)
    evidence_ids = _seed_evidence(database, 1)
    repository.save_parsed_facts_bulk([_facts(0, evidence_id=evidence_ids[0])])

    with pytest.raises(ValueError, match="不可变 ParsedFacts 冲突"):
        repository.save_parsed_facts_bulk([_facts(0, evidence_id=evidence_ids[0], title="被改过的标题")])


def test_source_card_does_not_open_extra_count_query_when_revision_has_evidence(tmp_path, monkeypatch):
    """revision 自己有证据行时用它的计数，绝不为一个数字再开连接做 COUNT(*)。"""

    from app.media_v4.projection import source_libraries as module

    database = _database(tmp_path)
    _seed_root_scan_revision(database, root_id="root-eff", scan_id="scan-eff", total_count=7)

    calls: list[str] = []
    monkeypatch.setattr(
        module, "_scan_evidence_count", lambda _database, scan_id: calls.append(scan_id) or 0
    )

    cards = module.list_source_cards(database)

    assert [card["root_id"] for card in cards] == ["root-eff"]
    assert cards[0]["evidence_count"] == 1, "以 revision 自己的证据行为准"
    assert calls == [], "不应为一个数字再开连接做 COUNT(*)"


def test_source_card_uses_authoritative_count_when_revision_has_no_evidence(tmp_path, monkeypatch):
    """revision 没有证据行时，数字仍必须来自权威计数（而不是扫描行的进度值）。

    扫描行的 processed_count/total_count 是进度值，读取期由内存计数推进、可能领先于
    真实落库行数；来源卡不能显示比实际更多的文件（既有用例
    test_zombie_scan_is_not_projected_as_active 等正是在断言这一点）。
    """

    from app.media_v4.projection import source_libraries as module

    database = _database(tmp_path)
    _seed_root_scan_revision(database, root_id="root-eff", scan_id="scan-eff", total_count=7)
    with database.connect() as conn:
        conn.execute("DELETE FROM revision_evidence WHERE revision_id = 'rev-a'")

    calls: list[str] = []
    monkeypatch.setattr(
        module, "_scan_evidence_count", lambda _database, scan_id: calls.append(scan_id) or 3
    )

    cards = module.list_source_cards(database)

    assert cards[0]["evidence_count"] == 3, "以库内权威计数为准"
    assert calls == ["scan-eff"], "没有 revision 证据时必须查一次权威计数"


def test_latest_scan_per_root_is_selected(tmp_path):
    from app.media_v4.projection import source_libraries as module

    database = _database(tmp_path)
    _seed_root_scan_revision(
        database, root_id="root-eff", scan_id="scan-old", total_count=1,
        generation=1, status="failed", created_at="2026-09-09T00:00:00+00:00",
    )
    _seed_root_scan_revision(
        database, root_id="root-eff", scan_id="scan-new", total_count=5,
        generation=2, status="completed", created_at="2026-09-09T05:00:00+00:00",
        revision_id="rev-b",
    )

    cards = module.list_source_cards(database)

    assert len(cards) == 1
    assert cards[0]["scan"]["scan_id"] == "scan-new", "每个 root 只取最新一次扫描"
    assert cards[0]["scan"]["status"] == "completed"


def _seed_root_scan_revision(
    database,
    *,
    root_id: str,
    scan_id: str,
    total_count: int,
    generation: int = 1,
    status: str = "completed",
    created_at: str = "2026-09-09T00:00:00+00:00",
    revision_id: str = "rev-a",
) -> None:
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.revisions.service import V4RevisionService

    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'baidu', 'directory_tree', ?, ?)",
            (root_id, created_at, created_at),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, total_count, "
            "processed_count, heartbeat_at, started_at) VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?)",
            (scan_id, root_id, generation, status, total_count, total_count, created_at, created_at),
        )
    if revision_id == "rev-a":
        service = V4RevisionService(database)
        evidence = SourceEvidence(
            evidence_id="ev-a",
            scan_id=scan_id,
            root_id=root_id,
            source_key="Show/a.mkv",
            relative_path="Show/a.mkv",
            entry_kind="video",
            provider="local",
            source_locator="local://a",
            fingerprint="sha256:a",
        )
        facts = ParsedFacts(
            parsed_fact_id="facts-a",
            evidence_id="ev-a",
            parser_version="fixture",
            work_title="Show",
            title_candidates=("Show",),
            year_candidate=2024,
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=1,
        )
        service.create_draft(revision_id, [(evidence, facts)])
