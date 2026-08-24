"""P-003 V4 只读执行进度投影合同。

投影必须从 authoritative tables（import_revisions/jobs/works/revision_bindings/
scrape_bindings）读取，禁止从 preview、日志或 Library Projection 反推。
原始 job_id 只作为重试命令参数，普通 UI 不展示。
"""

from __future__ import annotations


def _seed_revision(database, revision_id: str = "rev-progress", status: str = "confirmed") -> None:
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, created_at, updated_at) "
            "VALUES ('root-progress', 'pan115', 'openlist_scan', '/Anime', 'K:\\\\Anime', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status) VALUES ('scan-progress', 'root-progress', 1, 'completed')"
        )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at, confirmed_at) "
            "VALUES (?, 'root-progress', 'scan-progress', 'v4', ?, '', 'now', 'now')",
            (revision_id, status),
        )


def _seed_work(
    database,
    revision_id: str,
    *,
    work_id: str,
    title: str,
    episode_ids: list[str],
    asset_count: int,
    jobs: list[tuple[str, str, int, str]],
    scrape_status: str | None = None,
) -> None:
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) VALUES (?, ?, 'series', ?, 'now', 'now')",
            (work_id, f"ik-{work_id}", title),
        )
        if episode_ids:
            conn.execute(
                "INSERT INTO seasons(season_id, work_id, local_season_number, season_kind) VALUES (?, ?, 1, 'regular')",
                (f"season-{work_id}", work_id),
            )
            for episode_id in episode_ids:
                conn.execute(
                    "INSERT INTO episodes(episode_id, work_id, season_id, local_episode_number, episode_kind) VALUES (?, ?, ?, ?, 'regular')",
                    (episode_id, work_id, f"season-{work_id}", episode_ids.index(episode_id) + 1),
                )
        for index in range(asset_count):
            evidence_id = f"ev-{work_id}-{index}"
            asset_id = f"asset-{work_id}-{index}"
            conn.execute(
                "INSERT INTO source_evidence(evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind, source_locator, playback_locator, ingest_method, observed_at) "
                "VALUES (?, 'scan-progress', 'root-progress', 'pan115', ?, ?, 'video', ?, ?, 'openlist_api', 'now')",
                (evidence_id, f"rel-{work_id}-{index}", f"rel-{work_id}-{index}", f"loc-{work_id}-{index}", f"play-{work_id}-{index}"),
            )
            conn.execute(
                "INSERT INTO assets(asset_id, evidence_id, root_id, source_locator, playback_locator) "
                "VALUES (?, ?, 'root-progress', ?, ?)",
                (asset_id, evidence_id, f"loc-{work_id}-{index}", f"play-{work_id}-{index}"),
            )
            episode_id = episode_ids[index % len(episode_ids)] if episode_ids else None
            conn.execute(
                "INSERT INTO revision_bindings(binding_id, revision_id, evidence_id, work_id, episode_id, asset_id, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, 'high')",
                (f"binding-{work_id}-{index}", revision_id, evidence_id, work_id, episode_id, asset_id),
            )
        for index, (job_type, job_status, attempts, last_error) in enumerate(jobs):
            conn.execute(
                "INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, status, attempts, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'now', 'now')",
                (f"job-{work_id}-{index}", job_type, revision_id, work_id, f"ik-{work_id}-{index}", job_status, attempts, last_error),
            )
        if scrape_status is not None:
            conn.execute(
                "INSERT INTO scrape_bindings(binding_id, revision_id, work_id, provider, provider_id, metadata_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pan115', '1', '{}', ?, 'now', 'now')",
                (f"scrape-{work_id}", revision_id, work_id, scrape_status),
            )


def _seed_projection_job(database, revision_id: str, status: str = "queued") -> None:
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id, job_type, revision_id, work_id, idempotency_key, status, attempts, last_error, created_at, updated_at) "
            "VALUES ('job-projection', 'refresh_projection', ?, '', 'ik-projection', ?, 0, '', 'now', 'now')",
            (revision_id, status),
        )


def _progress(database, revision_id: str = "rev-progress"):
    from app.media_v4.revisions.service import V4RevisionService

    return V4RevisionService(database).get_execution_progress(revision_id)


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "progress.db")
    database.initialize()
    return database


def test_progress_shape_and_completed_state(tmp_path):
    database = _fresh_database(tmp_path)
    _seed_revision(database)
    _seed_work(database, "rev-progress", work_id="w1", title="摇曳露营", episode_ids=["ep-w1-1", "ep-w1-2"], asset_count=3, jobs=[
        ("materialize_mirror", "succeeded", 1, ""),
        ("scrape_work", "succeeded", 1, ""),
    ], scrape_status="confirmed")
    _seed_projection_job(database, "rev-progress", "succeeded")

    progress = _progress(database)
    assert progress["revision_id"] == "rev-progress"
    assert progress["revision_status"] == "confirmed"
    assert progress["overall_status"] == "completed"
    assert progress["stage_summary"]["mirror"]["total"] == 1
    assert progress["stage_summary"]["mirror"]["succeeded"] == 1
    assert progress["stage_summary"]["metadata"]["succeeded"] == 1
    assert progress["stage_summary"]["projection"]["succeeded"] == 1
    assert progress["stage_summary"]["projection"]["status"] == "succeeded"

    assert len(progress["work_units"]) == 1
    unit = progress["work_units"][0]
    assert unit["work_id"] == "w1"
    assert unit["title"] == "摇曳露营"  # 后端 works.preferred_title，不依赖前端内存
    assert unit["episode_count"] == 2  # 去重：3 个 asset 只绑 2 个 episode
    assert unit["asset_count"] == 3
    assert unit["overall_status"] == "completed"
    assert unit["mirror"]["status"] == "succeeded"
    assert unit["metadata"]["status"] == "succeeded"
    assert unit["metadata"]["job_id"] == "job-w1-1"


def test_mirror_running_and_failed_are_not_hidden(tmp_path):
    database = _fresh_database(tmp_path)
    _seed_revision(database)
    _seed_work(database, "rev-progress", work_id="w-running", title="运行中的作品", episode_ids=["ep-a"], asset_count=1, jobs=[
        ("materialize_mirror", "running", 2, ""),
        ("scrape_work", "queued", 0, ""),
    ])
    _seed_work(database, "rev-progress", work_id="w-failed", title="镜像失败的作品", episode_ids=["ep-b"], asset_count=1, jobs=[
        ("materialize_mirror", "failed", 1, "磁盘写入失败"),
        ("scrape_work", "queued", 0, ""),
    ])

    progress = _progress(database)
    by_id = {unit["work_id"]: unit for unit in progress["work_units"]}
    assert by_id["w-running"]["overall_status"] == "running_mirror"
    assert by_id["w-running"]["mirror"]["attempts"] == 2
    assert by_id["w-failed"]["overall_status"] == "failed"
    assert by_id["w-failed"]["mirror"]["last_error"] == "磁盘写入失败"
    # 优先级：running > needs_attention > queued > completed
    assert progress["overall_status"] == "running"
    assert progress["stage_summary"]["mirror"]["status"] == "running"
    assert progress["stage_summary"]["mirror"]["failed"] == 1


def test_metadata_stage_state_mapping(tmp_path):
    database = _fresh_database(tmp_path)
    _seed_revision(database)
    _seed_work(database, "rev-progress", work_id="w-wait", title="等待刮削", episode_ids=["ep-a"], asset_count=1, jobs=[
        ("materialize_mirror", "succeeded", 1, ""),
        ("scrape_work", "queued", 0, ""),
    ])
    _seed_work(database, "rev-progress", work_id="w-scraping", title="正在刮削", episode_ids=["ep-b"], asset_count=1, jobs=[
        ("materialize_mirror", "succeeded", 1, ""),
        ("scrape_work", "running", 1, ""),
    ])
    _seed_work(database, "rev-progress", work_id="w-cancelled", title="已取消", episode_ids=["ep-c"], asset_count=1, jobs=[
        ("materialize_mirror", "succeeded", 1, ""),
        ("scrape_work", "cancelled", 0, ""),
    ])

    progress = _progress(database)
    by_id = {unit["work_id"]: unit for unit in progress["work_units"]}
    assert by_id["w-wait"]["overall_status"] == "waiting_metadata"
    assert by_id["w-scraping"]["overall_status"] == "running_metadata"
    assert by_id["w-cancelled"]["overall_status"] == "cancelled"
    assert progress["overall_status"] == "running"


def test_scrape_needs_attention_is_not_completed(tmp_path):
    database = _fresh_database(tmp_path)
    _seed_revision(database)
    _seed_work(database, "rev-progress", work_id="w-review", title="需要人工确认", episode_ids=["ep-a"], asset_count=1, jobs=[
        ("materialize_mirror", "succeeded", 1, ""),
        ("scrape_work", "succeeded", 1, ""),
    ], scrape_status="waiting_review")

    progress = _progress(database)
    unit = progress["work_units"][0]
    assert unit["overall_status"] == "needs_attention"
    assert progress["overall_status"] == "needs_attention"


def test_overall_priority_queued_over_completed(tmp_path):
    database = _fresh_database(tmp_path)
    _seed_revision(database)
    _seed_work(database, "rev-progress", work_id="w-done", title="已完成", episode_ids=["ep-a"], asset_count=1, jobs=[
        ("materialize_mirror", "succeeded", 1, ""),
        ("scrape_work", "succeeded", 1, ""),
    ], scrape_status="confirmed")
    _seed_work(database, "rev-progress", work_id="w-queued", title="排队中", episode_ids=["ep-b"], asset_count=1, jobs=[
        ("materialize_mirror", "queued", 0, ""),
        ("scrape_work", "queued", 0, ""),
    ])

    progress = _progress(database)
    assert progress["overall_status"] == "queued"
    assert {unit["work_id"] for unit in progress["work_units"]} == {"w-done", "w-queued"}


def test_execution_progress_api_contract(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "api-progress.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    _seed_revision(database)
    _seed_work(database, "rev-progress", work_id="w1", title="API 作品", episode_ids=["ep-a"], asset_count=1, jobs=[
        ("materialize_mirror", "succeeded", 1, ""),
        ("scrape_work", "succeeded", 1, ""),
    ], scrape_status="confirmed")

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)
    response = client.get("/api/v4/imports/rev-progress")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["progress"]["overall_status"] == "completed"
    assert body["progress"]["work_units"][0]["title"] == "API 作品"
    # 原始 jobs 仍保留给重试等内部消费，但普通 UI 主列表不使用 UUID 文本。
    assert body["jobs"][0]["job_id"].startswith("job-")


def test_unknown_revision_returns_404(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4

    _database = _fresh_database(tmp_path)
    monkeypatch.setattr(media_v4, "_database", _database)
    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)
    assert client.get("/api/v4/imports/rev-missing").status_code == 404
