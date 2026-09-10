"""所有导入入口都必须先创建 durable SourceScan，不能占用 HTTP 请求扫描大库。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "durable-entrypoints.db")
    database.initialize()
    return database


def test_local_durable_entrypoint_returns_before_the_directory_scan_finishes(tmp_path, monkeypatch):
    """本地大目录扫描在后台进行，创建任务接口不得等待文件枚举。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    started = threading.Event()
    release = threading.Event()
    expected_root_id, _locator = media_v4._local_root_identity("D:/Media")

    def slow_local_scan(_root_path, **_kwargs):
        started.set()
        assert release.wait(3)
        evidence = to_source_evidence(SourceEntry(
            root_id=expected_root_id, scan_id=_kwargs["scan_id"], provider="local", ingest_method="local_scan",
            relative_path="Show/Show.S01E01.mkv", source_key="Show/Show.S01E01.mkv",
            source_locator="D:/Media/Show/Show.S01E01.mkv", playback_locator="D:/Media/Show/Show.S01E01.mkv",
        ))
        return expected_root_id, _kwargs["scan_id"], [evidence]

    monkeypatch.setattr(media_v4, "scan_local_directory", slow_local_scan)
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        openlist_server_url="https://openlist.example.test", openlist_remote_root="/",
        pan115_root="", baidu_root="", openlist_mount_root="X:/OpenList", openlist_routes=[],
    ))
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    started_at = time.monotonic()
    response = client.post("/api/v4/sources/scans", json={"source": "local", "root_path": "D:/Media"})
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200, response.text
    scan_id = response.json()["scan_id"]
    assert response.json()["status"] == "queued"
    assert elapsed < 0.5
    assert started.wait(1)
    cards = client.get("/api/v4/sources/libraries").json()["cards"]
    assert len(cards) == 1
    assert cards[0]["scan"]["scan_id"] == scan_id
    assert cards[0]["scan"]["status"] in {"queued", "running"}
    assert cards[0]["overall_status"] in {"queued", "running"}
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if get_durable_scan(database, scan_id)["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    completed = get_durable_scan(database, scan_id)
    assert completed["status"] == "completed"
    assert [entry["relative_path"] for entry in completed["entries"]] == ["Show/Show.S01E01.mkv"]

    # durable scan 完成后，preview 必须只消费持久化的证据；前端不应再携带
    # 可被篡改、也可能导致重复写入的完整 entries 快照。
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-durable-local",
        "root_id": expected_root_id,
        "scan_id": scan_id,
        "entries": [],
        "source_display_name": "本地媒体库",
    })
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["works"]) == 1


def test_local_durable_entrypoint_keeps_online_candidate_search_out_of_scan_entirely(
    tmp_path,
    monkeypatch,
):
    """来源扫描只做离线识别；联网候选只能在确认后的元数据任务中执行。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.resolution import candidates as candidate_service
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    expected_root_id, _locator = media_v4._local_root_identity("D:/Media")
    search_calls: list[str] = []

    def local_scan(_root_path, **_kwargs):
        evidence = to_source_evidence(SourceEntry(
            root_id=expected_root_id,
            scan_id=_kwargs["scan_id"],
            provider="local",
            ingest_method="local_scan",
            relative_path="Show/Show.S01E01.mkv",
            source_key="Show/Show.S01E01.mkv",
            source_locator="D:/Media/Show/Show.S01E01.mkv",
            playback_locator="D:/Media/Show/Show.S01E01.mkv",
        ))
        return expected_root_id, _kwargs["scan_id"], [evidence]

    def forbidden_candidate_search(work_key, *_args, **_kwargs):
        search_calls.append(work_key)
        raise AssertionError("扫描阶段不得联网搜索候选")

    monkeypatch.setattr(media_v4, "scan_local_directory", local_scan)
    monkeypatch.setattr(candidate_service, "default_candidate_search", forbidden_candidate_search)
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        pan115_root="",
        baidu_root="",
        openlist_mount_root="X:/OpenList",
        openlist_routes=[],
    ))
    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    started_at = time.monotonic()
    response = client.post("/api/v4/sources/scans", json={
        "source": "local",
        "root_path": "D:/Media",
        "revision_id": "rev-background-recognition",
        "source_display_name": "本地媒体库",
    })
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200, response.text
    scan_id = response.json()["scan_id"]
    assert elapsed < 0.5
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and get_durable_scan(database, scan_id)["status"] in {"queued", "running"}:
        time.sleep(0.02)
    assert get_durable_scan(database, scan_id)["status"] == "completed"

    compact_status = client.get(
        f"/api/v4/sources/scans/{scan_id}?include_entries=false"
    )
    assert compact_status.status_code == 200
    assert compact_status.json()["evidence_count"] == 1
    assert compact_status.json()["entries"] == []

    preview_started_at = time.monotonic()
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-background-recognition",
        "root_id": expected_root_id,
        "scan_id": scan_id,
        "entries": [],
        "source_display_name": "本地媒体库",
    })
    preview_elapsed = time.monotonic() - preview_started_at

    assert preview.status_code == 200, preview.text
    assert preview_elapsed < 0.5
    assert len(preview.json()["works"]) == 1
    assert search_calls == []


def test_running_durable_scan_reports_persisted_stage_and_evidence_count(tmp_path):
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "scan-progress.db")
    database.initialize()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-progress', 'local', 'local_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, started_at) "
            "VALUES ('scan-progress', 'root-progress', 1, 'running', 'now')"
        )
        conn.execute(
            "INSERT INTO source_evidence(evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind) "
            "VALUES ('evidence-progress', 'scan-progress', 'root-progress', 'local', 'Show/E01.mkv', 'Show/E01.mkv', 'video')"
        )

    status = get_durable_scan(database, "scan-progress", include_entries=False)

    assert status["stage"] == "recognizing"
    assert status["stage_label"] == "离线识别与整理"
    assert status["evidence_count"] == 1
    assert status["processed_count"] == 0
    assert status["total_count"] == 1


def test_durable_scan_persists_first_evidence_batch_before_source_returns(tmp_path):
    """扫描函数尚未返回时，轮询也必须能看到已发现的首批证据。"""

    from app.media_v4.sources.durable_scan import create_durable_scan, get_durable_scan

    database = _fresh_database(tmp_path)
    root_id = "root-stream-progress"
    scan_id = "scan-stream-progress"
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'local', 'local_scan', 'now', 'now')",
            (root_id,),
        )
    first_persisted = threading.Event()
    release = threading.Event()
    first = _stream_evidence(root_id, scan_id, "Show/Show.S01E01.mkv")
    second = _stream_evidence(root_id, scan_id, "Show/Show.S01E02.mkv")

    def streaming_scan(should_cancel=None, on_evidence_batch=None, on_progress=None):
        assert on_evidence_batch is not None
        assert on_progress is not None
        on_progress(stage="reading_source", processed_count=0, total_count=0)
        on_evidence_batch([first])
        first_persisted.set()
        assert release.wait(3)
        on_evidence_batch([second])
        return scan_id, [first, second]

    create_durable_scan(
        database,
        scan_id=scan_id,
        root_id=root_id,
        kind="full",
        scan_fn=streaming_scan,
    )
    assert first_persisted.wait(1)

    in_flight = get_durable_scan(database, scan_id, include_entries=False)
    assert in_flight["status"] == "running"
    assert in_flight["stage"] == "reading_source"
    assert in_flight["evidence_count"] == 1
    assert in_flight["processed_count"] == 1

    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and get_durable_scan(database, scan_id)["status"] in {"queued", "running"}:
        time.sleep(0.02)
    completed = get_durable_scan(database, scan_id)
    assert completed["status"] == "completed"
    assert completed["evidence_count"] == 2


def _stream_evidence(root_id: str, scan_id: str, relative: str):
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id=scan_id,
        provider="local",
        ingest_method="local_scan",
        relative_path=relative,
        source_key=relative,
    ))


def test_durable_finalizer_persists_parsed_facts_before_rebuilding_draft(tmp_path, monkeypatch):
    """大库扫描在构图前分批落盘，轮询才有真实的识别进度可读。"""

    from app.api import media_v4
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.revisions.service import V4RevisionService
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    database = _fresh_database(tmp_path)
    root_id = "root-finalizer-progress"
    scan_id = "scan-finalizer-progress"
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'local', 'local_scan', 'now', 'now')",
            (root_id,),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) "
            "VALUES (?, ?, 1, 'running')",
            (scan_id, root_id),
        )
    evidence = to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id=scan_id,
        provider="local",
        ingest_method="local_scan",
        relative_path="Show/Show.S01E01.mkv",
        source_key="Show/Show.S01E01.mkv",
    ))
    request = SimpleNamespace(revision_id="rev-finalizer-progress", source_display_name="本地测试")
    finalizer = media_v4._durable_draft_finalizer(
        database,
        request,
        root_id=root_id,
        scan_id=scan_id,
    )
    assert finalizer is not None
    V4Repository(database).save_scan_evidence_bulk([evidence])

    def rebuild(self, _revision_id, entries, **kwargs):
        assert V4Repository(database).get_parsed_facts(entries[0][1].parsed_fact_id) == entries[0][1]
        assert kwargs["_facts_already_persisted"] is True

    monkeypatch.setattr(V4RevisionService, "create_draft", rebuild)

    progress: list[dict] = []
    finalizer([evidence], on_progress=lambda **item: progress.append(item))
    assert progress
    assert progress[-1]["stage"] == "preparing_preview"
    assert progress[-1]["processed_count"] == progress[-1]["total_count"] == 1


def test_durable_finalizer_normalizes_one_season_across_persistence_batches(tmp_path):
    """128 条持久化批次不能成为季度编号归一化的语义边界。"""

    from app.api import media_v4
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    database = _fresh_database(tmp_path)
    root_id = "root-finalizer-season"
    scan_id = "scan-finalizer-season"
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES (?, 'local', 'local_scan', 'now', 'now')",
            (root_id,),
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) "
            "VALUES (?, ?, 1, 'running')",
            (scan_id, root_id),
        )
    evidence = [
        to_source_evidence(SourceEntry(
            root_id=root_id,
            scan_id=scan_id,
            provider="local",
            ingest_method="local_scan",
            relative_path=f"Show/Show [S2]/Show.S02E{number:03d}.mkv",
            source_key=f"Show/Show [S2]/Show.S02E{number:03d}.mkv",
        ))
        for number in range(12, 141)
    ]
    V4Repository(database).save_scan_evidence_bulk(evidence)
    request = SimpleNamespace(revision_id="rev-finalizer-season", source_display_name="季度测试")
    finalizer = media_v4._durable_draft_finalizer(
        database,
        request,
        root_id=root_id,
        scan_id=scan_id,
    )
    assert finalizer is not None

    finalizer(evidence)

    with database.connect() as conn:
        last = conn.execute(
            "SELECT season_candidate, episode_candidate, absolute_episode_candidate "
            "FROM parsed_facts WHERE evidence_id = ?",
            (evidence[-1].evidence_id,),
        ).fetchone()
    assert tuple(last) == (2, 129, 140)


def test_tree_durable_entrypoint_resolves_identity_before_task_creation(tmp_path, monkeypatch):
    """TXT 身份解析必须在任务创建前完成；大证据构建仍留在后台。

    同步与 durable 入口共享同一「解析后身份根」：durable 请求内先读取 TXT
    并完成纯词法解析（只读用户选定的 TXT），再创建 root/scan 身份并立即
    返回；逐条证据构建与落库仍在后台线程进行，不占用 HTTP 请求。
    """

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", database)
    mounted_root = tmp_path / "mounted"
    video = mounted_root / "Anime" / "Show" / "Show.S01E01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    tree_file = tmp_path / "Anime.txt"
    tree_file.write_text("Anime\n└── Show\n    └── Show.S01E01.mkv\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    real_build = media_v4.build_directory_tree_evidence

    def slow_evidence_build(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(media_v4, "build_directory_tree_evidence", slow_evidence_build)
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="", baidu_root=str(mounted_root), openlist_mount_root="", openlist_routes=[],
    ))
    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    response = client.post("/api/v4/sources/scans", json={
        "source": "tree", "tree_file": str(tree_file), "provider": "baidu",
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    scan_id = payload["scan_id"]
    assert payload["status"] == "queued"
    # 身份已在请求内解析完成：响应携带与同步入口一致的解析后播放根。
    assert payload["effective_playback_root"] == str(mounted_root)
    assert payload["path_validation"]["ok"] is True
    assert started.wait(1)
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and get_durable_scan(database, scan_id)["status"] in {"queued", "running"}:
        time.sleep(0.02)
    result = get_durable_scan(database, scan_id)
    assert result["status"] == "completed"
    assert [entry["relative_path"] for entry in result["entries"]] == ["Show/Show.S01E01.mkv"]
