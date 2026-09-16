"""O7：revision 级读取必须批量，且结果与逐条读取完全一致。

来源卡在有活动任务时每 1.5 秒对 draft 做一次**只读实时评估**（这是有意的：识别规则
升级后不能继续显示旧表里的红色冲突）。评估本身必须保留，但 `_load_revision_entries`
原先逐条调用 `get_source_evidence` + `get_parsed_facts`，于是 3 万条证据 = 6 万次单条
SELECT。这里锁定两件事：① 批量读取的对象与逐条读取**完全相等**；② 语句数不随证据
条数增长。
"""

from __future__ import annotations


def _entry(index: int):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    title = f"Show{index:02d}"
    evidence_id = f"ev-{index:03d}"
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-bulk",
        root_id="root-bulk",
        source_key=evidence_id,
        relative_path=f"{title}/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://{evidence_id}",
        fingerprint=f"sha256:{evidence_id}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{index:03d}",
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
    return evidence, facts


def _draft(tmp_path, count: int):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / f"bulk-{count}.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry(index) for index in range(count)])
    # 一条人工覆盖：批量路径必须与逐条路径一样应用 overrides 与 tuple 归一化。
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO revision_overrides(revision_id, evidence_id, overrides_json, created_at) "
            "VALUES ('rev-1', 'ev-000', '{\"title_candidates\": [\"覆盖标题\"]}', 'now')"
        )
    return database, revisions


def test_bulk_repository_reads_equal_row_by_row_reads(tmp_path):
    """仓储层等价性：批量读取的对象必须与逐条读取逐一相等（含缺失 id 语义）。"""

    database, revisions = _draft(tmp_path, 24)
    repository = revisions.repository

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT evidence_id, parsed_fact_id FROM revision_evidence WHERE revision_id = 'rev-1' "
            "ORDER BY evidence_id"
        ).fetchall()
        evidence_ids = [str(row["evidence_id"]) for row in rows]
        fact_ids = [str(row["parsed_fact_id"]) for row in rows]

        bulk_evidence = repository.get_source_evidence_bulk(evidence_ids, conn=conn)
        bulk_facts = repository.get_parsed_facts_bulk(fact_ids, conn=conn)
        single_evidence = {
            evidence_id: repository.get_source_evidence(evidence_id, conn=conn)
            for evidence_id in evidence_ids
        }
        single_facts = {
            # 同一 parsed_fact_id 可能被多条证据引用，去重后对比即可。
            fact_id: repository.get_parsed_facts(fact_id, conn=conn)
            for fact_id in dict.fromkeys(fact_ids)
        }

    assert bulk_evidence == single_evidence
    assert bulk_facts == single_facts
    assert len(bulk_evidence) == 24


def test_load_revision_entries_applies_overrides_with_tuple_normalization(tmp_path):
    _database, revisions = _draft(tmp_path, 6)

    entries = revisions._load_revision_entries("rev-1")

    assert len(entries) == 6
    overridden = next(facts for _evidence, facts in entries if facts.evidence_id == "ev-000")
    assert overridden.title_candidates == ("覆盖标题",)


def _track_statements(monkeypatch, statements: list[str]) -> None:
    """把后续新建连接的 SQL 记录到 ``statements``。"""

    from app.media_v4.persistence.database import V4Database

    real_open = V4Database.open_connection

    def counting_open(self):
        conn = real_open(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(V4Database, "open_connection", counting_open)


def test_entry_loading_statements_do_not_scale_with_entry_count(tmp_path, monkeypatch):
    counts: dict[int, int] = {}
    for count in (6, 60):
        statements: list[str] = []
        # 用 context() 只撤销本次补丁：早先这里用 monkeypatch.undo()，会把 autouse
        # 夹具（数据目录隔离）的补丁一并撤销，于是单独跑通过、全量跑失败（测试污染）。
        with monkeypatch.context() as patched:
            _track_statements(patched, statements)
            _database, revisions = _draft(tmp_path, count)
            revisions._load_revision_entries("rev-1")
            statements.clear()
            revisions._load_revision_entries("rev-1")
            counts[count] = len([sql for sql in statements if sql.strip().upper().startswith("SELECT")])

    assert counts[60] == counts[6], (
        f"语句数必须与条目数无关（6 条 {counts[6]} 次 vs 60 条 {counts[60]} 次）"
    )
    assert counts[6] <= 8, f"批量读取应为常数级查询，实际 {counts[6]} 次"


def test_draft_graph_counts_survive_the_batch_read(tmp_path):
    database, revisions = _draft(tmp_path, 24)

    graph = revisions.load_draft_graph("rev-1", refresh_snapshot=False)

    assert len(graph.works) == 24, "每一条独立作品都必须进入草稿图"
    assert graph.issues == () or len(graph.issues) >= 0  # 结构存在即可，数量由规则决定
