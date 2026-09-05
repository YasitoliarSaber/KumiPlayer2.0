"""V4 导入收口回归：离线批量扫描、进度合同、取消响应与系列关系。

这些测试锁定本轮收口的用户可见合同：
- 扫描预览阶段完全不发起普通 provider 在线搜索；
- 大型目录树在近似 O(n) 时间内完成解析与批量持久化；
- 扫描阶段读数单调前进且刷新可恢复；
- 取消能在来源读取的批次边界生效，而不是等整棵目录读完。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest


def _synthetic_tree_text(works: int = 105, episodes_per_season: int = 24) -> str:
    """构造 5000 条级别的合成目录树（纯文本，不触碰真实媒体）。"""

    lines: list[str] = ["动画库"]
    total = 0
    for index in range(1, works + 1):
        work = f"Sample Series {index:03d}"
        lines.append(f"├── {work}")
        for season in (1, 2):
            lines.append(f"│   ├── {work} Season {season}")
            for episode in range(1, episodes_per_season + 1):
                lines.append(
                    f"│   │   └── {work} - S{season:02d}E{episode:02d}.mkv"
                )
                total += 1
    assert total >= 5000
    return "\n".join(lines)


def _parse_tree(text: str, root_id: str, scan_id: str):
    from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
    from app.media_v4.sources.scanner import build_directory_tree_evidence

    _actual_scan_id, evidence = build_directory_tree_evidence(
        text, root_id=root_id, scan_id=scan_id, provider="baidu",
    )
    parser = V4Parser()
    parsed = [(item, parser.parse(item)) for item in evidence]
    return normalize_batch_parsed_facts(parsed)


def test_five_thousand_entry_tree_parses_offline_and_persists_in_batches(tmp_path, monkeypatch):
    """5000 条合成目录树离线解析；SQLite 连接数随批次而非条目增长。"""

    import contextlib

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.resolution import candidates as candidate_service
    from app.media_v4.resolution.resolver import MediaResolver

    text = _synthetic_tree_text(works=105, episodes_per_season=24)
    total = text.count(".mkv")
    assert total >= 5000

    def forbidden_online_search(*_args, **_kwargs):
        raise AssertionError("扫描预览阶段不得发起普通 provider 在线搜索")

    monkeypatch.setattr(
        candidate_service, "default_candidate_search", forbidden_online_search
    )

    database = V4Database(tmp_path / "bulk-scan.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-bulk-scan', 'baidu', 'directory_tree', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-bulk-scan', 'root-bulk-scan', 1, 'running', 'now')"
        )

    # 计数 wrapper 必须真实替换 database.connect；仓库层全部经
    # self.database.connect() 打开事务，这里统计每一次真实连接。
    connect_counter = {"count": 0}
    original_connect = database.connect

    @contextlib.contextmanager
    def counting_connect(*args, **kwargs):
        connect_counter["count"] += 1
        with original_connect(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(database, "connect", counting_connect)

    repository = V4Repository(database)
    batch_size = 128
    expected_batches = -(-total // batch_size)

    started = time.monotonic()
    parsed = _parse_tree(text, root_id="root-bulk-scan", scan_id="scan-bulk-scan")
    graph = MediaResolver().resolve(parsed)
    repository.save_scan_evidence_bulk([evidence for evidence, _facts in parsed])
    for offset in range(0, len(parsed), batch_size):
        repository.save_parsed_facts_bulk(
            [facts for _evidence, facts in parsed[offset : offset + batch_size]]
        )
    elapsed = time.monotonic() - started

    assert len(parsed) == total
    assert graph.issues == () or all(
        "重复" not in issue.get("message", "") for issue in graph.issues
    )
    with database.connect() as conn:
        persisted = conn.execute("SELECT COUNT(*) FROM parsed_facts").fetchone()[0]
        evidence_rows = conn.execute("SELECT COUNT(*) FROM source_evidence").fetchone()[0]
    assert persisted == total
    assert evidence_rows == total

    # 计数必须真实生效：1 次证据批量 + facts 批次 + 轮询回读都走同一
    # 计数连接；若 wrapper 未安装或实现退化为逐条事务，双向断言都会失败。
    assert connect_counter["count"] >= expected_batches, (
        f"计数连接仅 {connect_counter['count']} 次，低于批次数 {expected_batches}，wrapper 未生效"
    )
    assert connect_counter["count"] <= expected_batches + 8, (
        f"持久化打开了 {connect_counter['count']} 次连接，疑似逐条事务"
    )
    # 近似 O(n)：5000 条级别在固定预算内完成（实测约 1.2s，上限 15s 阻止回退）。
    assert elapsed < 15, f"5000 条解析耗时 {elapsed:.1f}s，超出预算"

    first = repository.get_parsed_facts(parsed[0][1].parsed_fact_id)
    assert first == parsed[0][1]


def test_durable_scan_cancel_interrupts_source_read_at_batch_boundary(tmp_path):
    """取消信号必须在来源读取的批次边界生效，而不是等整棵目录读完。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import create_durable_scan, get_durable_scan

    database = V4Database(tmp_path / "cancel-read.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-cancel-read', 'local', 'local_scan', 'now', 'now')"
        )

    def slow_scan(should_cancel=None):
        """模拟逐步读取大目录：每批检查取消信号。"""

        evidence = []
        for batch in range(50):
            if should_cancel is not None and should_cancel():
                from app.media_v4.sources.scanner import SourceScanCancelled

                raise SourceScanCancelled()
            time.sleep(0.02)
            evidence.append(_fake_evidence("scan-cancel-read", f"Show/E{batch:02d}.mkv"))
        return "scan-cancel-read", evidence

    create_durable_scan(
        database,
        scan_id="scan-cancel-read",
        root_id="root-cancel-read",
        kind="full",
        scan_fn=slow_scan,
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and get_durable_scan(database, "scan-cancel-read")["status"] != "running":
        time.sleep(0.01)

    from app.media_v4.sources.durable_scan import cancel_durable_scan

    assert cancel_durable_scan("scan-cancel-read") is True
    deadline = time.monotonic() + 2
    status = "running"
    while time.monotonic() < deadline:
        status = get_durable_scan(database, "scan-cancel-read")["status"]
        if status == "cancelled":
            break
        time.sleep(0.02)
    assert status == "cancelled", "取消未在读批次边界生效"

    with database.connect() as conn:
        persisted = conn.execute(
            "SELECT COUNT(*) FROM source_evidence WHERE scan_id = 'scan-cancel-read'"
        ).fetchone()[0]
    assert persisted == 0, "已取消扫描不得留下证据"


def _fake_evidence(scan_id: str, relative: str):
    from app.media_v4.domain.models import SourceEvidence

    return SourceEvidence(
        evidence_id=f"ev-{scan_id}-{relative}",
        scan_id=scan_id,
        root_id="root-cancel-read",
        source_key=relative,
        relative_path=relative,
        entry_kind="video",
        provider="local",
    )


def test_scan_stage_read_model_is_monotonic_and_recoverable(tmp_path):
    """阶段读数只能前进：reading → recognizing → preparing → ready；刷新可恢复。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "stage-progress.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-stage', 'local', 'local_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-stage', 'root-stage', 1, 'running', 'now')"
        )

    order = {"reading_source": 0, "recognizing": 1, "preparing_preview": 2, "ready": 3}
    seen: list[tuple[str, int]] = []

    def observe() -> str:
        status = get_durable_scan(database, "scan-stage", include_entries=False)
        stage = status["stage"]
        value = (stage, int(status["processed_count"]))
        if not seen or order[stage] >= order[seen[-1][0]]:
            if not seen or value != seen[-1]:
                seen.append(value)
        return status["status"]

    assert observe() == "running"
    assert seen[-1][0] == "reading_source"

    with database.connect() as conn:
        for index in range(4):
            conn.execute(
                "INSERT INTO source_evidence(evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind) "
                f"VALUES ('ev-stage-{index}', 'scan-stage', 'root-stage', 'local', 'E{index}.mkv', 'E{index}.mkv', 'video')"
            )
    assert observe() == "running"
    assert seen[-1][0] == "recognizing" and seen[-1][1] == 0

    with database.connect() as conn:
        for index in range(4):
            conn.execute(
                "INSERT INTO parsed_facts(parsed_fact_id, evidence_id, parser_version, resource_type, media_type) "
                f"VALUES ('facts-stage-{index}', 'ev-stage-{index}', 'v4', 'video', 'tv')"
            )
    assert observe() == "running"
    assert seen[-1][0] == "preparing_preview" and seen[-1][1] == 4

    with database.connect() as conn:
        conn.execute(
            "UPDATE source_scans SET status = 'completed', finished_at = 'now' WHERE scan_id = 'scan-stage'"
        )
    assert observe() == "completed"
    assert seen[-1][0] == "ready" and seen[-1][1] == 4

    stages = [stage for stage, _count in seen]
    assert stages == sorted(stages, key=lambda item: order[item])
    counts = [count for _stage, count in seen]
    assert counts == sorted(counts)


def test_openlist_full_scan_only_reads_selected_remote_root_offline(tmp_path, monkeypatch):
    """OpenList 入口只允许列取用户选中的远端目录，且全程不搜索元数据。"""

    from types import SimpleNamespace

    from app.media_v4.resolution import candidates as candidate_service
    from app.media_v4.resolution.resolver import MediaResolver
    from app.media_v4.sources.scanner import scan_openlist_directory

    def forbidden_online_search(*_args, **_kwargs):
        raise AssertionError("OpenList 扫描阶段不得发起元数据搜索")

    monkeypatch.setattr(
        candidate_service, "default_candidate_search", forbidden_online_search
    )

    class FakeEntry:
        def __init__(self, name: str, is_dir: bool, remote_path: str):
            self.name = name
            self.is_dir = is_dir
            self.remote_path = remote_path
            self.size = 1024
            self.modified = 0.0

    class FakeOpenListClient:
        def __init__(self):
            self.listed: list[str] = []

        def list_dir(self, directory: str, *, page: int, per_page: int, refresh: bool):
            self.listed.append(directory)
            if directory == "/动画":
                entries = [FakeEntry("Yuru Camp", True, "/动画/Yuru Camp")]
            elif directory == "/动画/Yuru Camp":
                entries = [
                    FakeEntry(
                        f"Yuru Camp S01E{number:02d}.mkv",
                        False,
                        f"/动画/Yuru Camp/Yuru Camp S01E{number:02d}.mkv",
                    )
                    for number in range(1, 4)
                ]
            elif directory == "/电影":
                entries = [FakeEntry("Movie.mkv", False, "/电影/Movie.mkv")]
            else:
                entries = []
            return SimpleNamespace(entries=entries, total=len(entries))

    client = FakeOpenListClient()
    _scan_id, evidence = scan_openlist_directory(
        client,
        remote_root="/动画",
        mapping_root="/动画",
        mount_root="",
        root_id="root-openlist-offline",
        default_provider="pan115",
    )

    assert set(client.listed) == {"/动画", "/动画/Yuru Camp"}, (
        f"扫描读取了未选择的目录: {sorted(client.listed)}"
    )
    assert {item.relative_path for item in evidence} == {
        "Yuru Camp/Yuru Camp S01E01.mkv",
        "Yuru Camp/Yuru Camp S01E02.mkv",
        "Yuru Camp/Yuru Camp S01E03.mkv",
    }

    from app.media_v4.parsing.parser import V4Parser

    parser = V4Parser()
    parsed = [(item, parser.parse(item)) for item in evidence]
    graph = MediaResolver().resolve(parsed)
    assert graph.issues == ()
    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "Yuru Camp"


def test_spinoff_and_movie_are_separate_works_linked_by_relations():
    from app.media_v4.parsing.parser import V4Parser
    from app.media_v4.resolution.resolver import MediaResolver
    from app.media_v4.sources.scanner import build_directory_tree_evidence

    paths = [
        "[VCB-Studio] Yuru Camp/Yuru Camp/Yuru Camp [01].mkv",
        "[VCB-Studio] Yuru Camp/Yuru Camp Season 2/Yuru Camp Season 2 [01].mkv",
        "[VCB-Studio] Yuru Camp/Heya Camp△/Heya Camp△ [01].mkv",
        "[VCB-Studio] Yuru Camp/Yuru Camp Movie (2022)/Yuru Camp Movie.mkv",
    ]
    text = "root\n" + "\n".join(f"│   └── {path}" for path in paths)
    _scan, evidence = build_directory_tree_evidence(
        text, root_id="root-relations", scan_id="scan-relations", provider="baidu",
    )
    parser = V4Parser()
    parsed = [(item, parser.parse(item)) for item in evidence]
    graph = MediaResolver().resolve(parsed)

    works_by_title = {work.preferred_title: work for work in graph.works}
    assert set(works_by_title) == {"Yuru Camp", "Heya Camp△", "Yuru Camp Movie"}

    main_key = works_by_title["Yuru Camp"].work_key
    season2_episodes = [
        episode for episode in graph.episodes
        if episode.work_key == main_key and episode.local_season_number == 2
    ]
    assert len(season2_episodes) == 1, "第二季必须归属主系列 Work"

    relation_pairs = {
        (relation.parent_work_key, relation.child_work_key, relation.relation_type)
        for relation in graph.relations
    }
    heya_key = works_by_title["Heya Camp△"].work_key
    movie_key = works_by_title["Yuru Camp Movie"].work_key
    assert heya_key != main_key and movie_key != main_key, "外传/电影必须保持独立 Work"
    assert (main_key, heya_key, "spin_off") in relation_pairs, (
        f"外传缺少与主系列的关系: {relation_pairs}"
    )


def test_cancel_during_fact_persistence_keeps_audit_evidence_but_never_creates_draft(tmp_path):
    """取消合同 B：解析/落盘期间取消保留不可变审计事实，但绝不生成草稿。"""

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.durable_scan import get_durable_scan
    from app.media_v4.sources.scanner import SourceScanCancelled

    database = V4Database(tmp_path / "cancel-finalize.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-cancel-finalize', 'baidu', 'directory_tree', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-cancel-finalize', 'root-cancel-finalize', 1, 'running', 'now')"
        )

    evidence = [
        to_source_evidence(SourceEntry(
            root_id="root-cancel-finalize",
            scan_id="scan-cancel-finalize",
            provider="baidu",
            ingest_method="directory_tree",
            relative_path=f"动画/Show{index // 120:02d}/S01E{index % 120 + 1:02d}.mkv",
            source_key=f"动画/Show{index // 120:02d}/S01E{index % 120 + 1:02d}.mkv",
        ))
        for index in range(300)
    ]
    V4Repository(database).save_scan_evidence_bulk(evidence)

    request = type("Req", (), {"revision_id": "rev-cancel-finalize", "source_display_name": "测试"})()
    finalizer = media_v4._durable_draft_finalizer(
        database,
        request,
        root_id="root-cancel-finalize",
        scan_id="scan-cancel-finalize",
    )

    calls = {"count": 0}

    def cancel_from_second_batch():
        # 调用序：finalizer 起点检查(1) → 批1 边界(2) → 批2 边界(3)。
        calls["count"] += 1
        return calls["count"] > 2

    with pytest.raises(SourceScanCancelled):
        finalizer(evidence, should_cancel=cancel_from_second_batch)

    with database.connect() as conn:
        facts = conn.execute("SELECT COUNT(*) FROM parsed_facts").fetchone()[0]
        revisions = conn.execute(
            "SELECT COUNT(*) FROM import_revisions WHERE revision_id = 'rev-cancel-finalize'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE source_scans SET status = 'cancelled', finished_at = 'now', error = '用户已取消扫描' "
            "WHERE scan_id = 'scan-cancel-finalize'"
        )

    # 合同 B：审计事实可以保留，但草稿绝不允许存在。
    assert 0 < facts < len(evidence), f"取消应保留部分审计事实，实际 {facts}"
    assert revisions == 0, "cancelled scan 不得创建 draft revision"
    cards = list_source_cards(database)
    assert len(cards) == 1, "扫描开始时建立的来源卡必须在取消后保留恢复入口"
    assert cards[0]["root_id"] == "root-cancel-finalize"
    assert cards[0]["phase"] == "scan"
    assert cards[0]["overall_status"] == "cancelled"
    assert cards[0]["can_resume"] is True
    assert get_durable_scan(database, "scan-cancel-finalize", include_entries=False)["status"] == "cancelled"


def test_draft_finalizer_reports_progress_before_first_persistence_batch(tmp_path):
    """大目录首批 128 条尚未完成时，也必须让界面看到解析正在推进。"""

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    database = V4Database(tmp_path / "parse-progress.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-parse-progress', 'baidu', 'directory_tree', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-parse-progress', 'root-parse-progress', 1, 'running', 'now')"
        )

    evidence = [
        to_source_evidence(SourceEntry(
            root_id="root-parse-progress",
            scan_id="scan-parse-progress",
            provider="baidu",
            ingest_method="directory_tree",
            relative_path=f"动画/Show/S01E{index:03d}.mkv",
            source_key=f"动画/Show/S01E{index:03d}.mkv",
        ))
        for index in range(1, 130)
    ]
    V4Repository(database).save_scan_evidence_bulk(evidence)
    request = type("Req", (), {"revision_id": "rev-parse-progress", "source_display_name": "测试"})()
    finalizer = media_v4._durable_draft_finalizer(
        database,
        request,
        root_id="root-parse-progress",
        scan_id="scan-parse-progress",
    )
    progress: list[tuple[str, int, int]] = []

    finalizer(
        evidence,
        on_progress=lambda **item: progress.append(
            (str(item["stage"]), int(item["processed_count"]), int(item["total_count"]))
        ),
    )

    assert ("parsing", 16, 129) in progress
    assert ("parsing", 128, 129) in progress
    assert progress[-1] == ("preparing_preview", 129, 129)


def test_cancelled_scan_cannot_enter_preview_even_with_tree_validation(tmp_path, monkeypatch):
    """cancelled scan 的验证行不能绕过状态门；preview 必须 409。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    database = V4Database(tmp_path / "cancel-preview.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)

    root_id = "root-cancel-preview"
    scan_id = "scan-cancel-preview"
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, "
            "root_container, created_at, updated_at) VALUES (?, 'baidu', 'directory_tree', 'X:/动画', 'X:/动画', '动画', 'now', 'now')",
            (root_id,),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES (?, ?, 1, 'cancelled', 'now')",
            (scan_id, root_id),
        )
        conn.execute(
            "INSERT INTO tree_scan_validation(scan_id, root_id, effective_root, ok, hits, total, validated_at) "
            "VALUES (?, ?, 'X:/动画', 1, 1, 1, 'now')",
            (scan_id, root_id),
        )
    evidence = to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id=scan_id,
        provider="baidu",
        ingest_method="directory_tree",
        relative_path="动画/Show/Show.S01E01.mkv",
        source_key="动画/Show/Show.S01E01.mkv",
    ))
    V4Repository(database).save_scan_evidence_bulk([evidence])

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    response = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-cancel-preview",
        "root_id": root_id,
        "scan_id": scan_id,
        "source": "tree",
        "provider": "baidu",
        "source_mode": "tree_snapshot",
        "source_display_name": "测试",
    })

    assert response.status_code == 409, response.text
    assert "重新扫描" in response.json()["detail"]
    with database.connect() as conn:
        drafts = conn.execute(
            "SELECT COUNT(*) FROM import_revisions WHERE revision_id = 'rev-cancel-preview'"
        ).fetchone()[0]
    assert drafts == 0


def test_newer_running_scan_never_pairs_with_an_older_draft_on_source_card(tmp_path):
    """来源卡必须只展示同一 scan 代次的识别草稿，避免用户确认过期结果。"""

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.source_libraries import list_source_cards

    database = V4Database(tmp_path / "source-card-generation.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, display_name, created_at, updated_at) "
            "VALUES ('root-generation', 'baidu', 'openlist_scan', '/百度网盘/01动画/刮削好的动画', '百度动画', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-old', 'root-generation', 1, 'completed', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at, stage, heartbeat_at) "
            "VALUES ('scan-new', 'root-generation', 2, 'running', 'now', 'reading_source', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at) "
            "VALUES ('rev-old-draft', 'root-generation', 'scan-old', 'v4', 'draft', '', 'now')"
        )

    card = list_source_cards(database)[0]

    assert card["phase"] == "scan"
    assert card["revision_id"] == ""
    assert card["revision_state"] == "running"
    assert card["display_path"] == "01动画 › 刮削好的动画"
    assert card["active_task"] == {
        "kind": "scan",
        "revision_id": "",
        "status": "running",
        "stage": "reading_source",
        "label": "读取媒体来源",
        "percent": None,
        "can_cancel": True,
        "cancel_requested": False,
    }
