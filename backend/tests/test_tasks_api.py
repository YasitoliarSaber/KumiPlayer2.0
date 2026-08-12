# -*- coding: utf-8 -*-
"""Unified task API tests."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_tasks_api_lists_and_cancels_all_task_types(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    record = manager.submit(
        "library_rescan",
        "local",
        lambda: {"ok": True},
        message="测试任务",
    )

    client = TestClient(app)
    response = client.get("/api/tasks?limit=20")
    assert response.status_code == 200
    data = response.json()
    assert any(item["task_id"] == record.task_id for item in data["tasks"])

    response = client.post(f"/api/tasks/{record.task_id}/cancel")
    assert response.status_code == 200
    assert response.json()["task_id"] == record.task_id


def test_tasks_api_can_filter_by_prefix(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    manager.submit("mirror_generate", "pan115", lambda: {"ok": True}, message="镜像")

    client = TestClient(app)
    response = client.get("/api/tasks?type_prefix=mirror_")
    assert response.status_code == 200
    assert response.json()["tasks"]
    assert all(item["task_type"].startswith("mirror_") for item in response.json()["tasks"])


def test_tasks_api_lists_openlist_import_task(monkeypatch):
    """OpenList 导入任务运行时，来源与任务类型过滤能返回任务（恢复用）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    record = manager.submit(
        "openlist_import",
        "openlist",
        lambda: {"video_count": 1248, "work_count": 86},
        message="扫描 OpenList 目录",
    )

    client = TestClient(app)
    response = client.get("/api/tasks?source=openlist&task_type=openlist_import&limit=20")
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert any(item["task_id"] == record.task_id for item in tasks)
    assert all(item["task_type"] == "openlist_import" for item in tasks)
    assert all(item["source"] == "openlist" for item in tasks)


def test_restart_marked_failed_does_not_block_new_openlist_task(monkeypatch):
    """后端重启后：数据库遗留 running 任务收口为失败，新扫描不再被并发保护拒绝。"""
    import threading
    import time

    from app.db.database import init_db
    from app.db.tasks import mark_interrupted_tasks_failed, save_task
    from app.tasks.registry import get_task_manager

    init_db()

    # 模拟上次进程遗留的 running 任务（当前 TaskManager 没有对应线程）
    save_task({
        "task_id": "stale_openlist_scan",
        "task_type": "openlist_import",
        "source": "openlist",
        "status": "running",
        "progress": 40,
        "message": "正在扫描",
        "created_at": "2026-08-01T10:00:00+08:00",
    })
    marked = mark_interrupted_tasks_failed()
    assert marked >= 1  # 至少收口了本次遗留任务（其他并行测试的任务可能同时被收口）

    # 收口后的失败任务不应阻塞同一来源的新任务
    manager = get_task_manager()
    event = threading.Event()

    def slow_work(*, progress_callback=None):
        event.wait(timeout=5)
        return {"video_count": 1, "work_count": 1}

    record = manager.submit("openlist_import", "openlist", slow_work, message="重新扫描")
    assert record.status in {"pending", "running"}
    manager.cancel_task(record.task_id)
    event.set()
    time.sleep(0.2)
    # 关闭并重建单例：不能留下已 shutdown 的线程池污染后续测试
    from app.tasks.registry import reset_task_manager
    reset_task_manager()


# ---- 模块3 C3：durable job 前端任务门面（/api/tasks 双轨） ----


@pytest.fixture
def durable_jobs_db(tmp_path, monkeypatch):
    """隔离 jobs SQLite 库：避免测试写入真实 data/，且互不污染。"""
    import app.db.database as db_mod
    from app.db.database import close_connection, init_db

    monkeypatch.setattr(db_mod, "_db_path", tmp_path / "jobs.db")
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _create_durable_job(job_type="mirror_revision", source="pan115", **payload_extra):
    from app.jobs import store as job_store

    payload = {"source": source}
    payload.update(payload_extra)
    return job_store.create_job(job_type=job_type, payload=payload)


def test_tasks_api_get_durable_job_via_facade(durable_jobs_db):
    """durable job id 通过统一任务门面返回 TaskRecord 兼容形状。

    jobs 表内部 queued 在门面出口适配为前端契约的 pending；内部状态机不动。
    """
    from fastapi.testclient import TestClient

    from app.jobs import store as job_store
    from app.main import app

    job = _create_durable_job()
    client = TestClient(app)
    response = client.get(f"/api/tasks/{job.job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == job.job_id
    assert data["task_type"] == "mirror_revision"
    assert data["source"] == "pan115"
    assert data["status"] == "pending"  # 前端契约：queued → pending
    assert data["created_at"] == job.created_at
    assert "started_at" in data and "finished_at" in data and "result" in data
    # 内部状态机不动：DB 仍是 queued（queued 是 jobs 表的内部事实）
    assert job_store.get_job(job.job_id).status == "queued"


def test_tasks_api_get_legacy_task_via_facade(durable_jobs_db):
    """legacy task id 走原有 TaskRecord 路径（与 durable 同一 URL）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    record = manager.submit("library_rescan", "local", lambda: {"ok": True}, message="测试")
    client = TestClient(app)
    response = client.get(f"/api/tasks/{record.task_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == record.task_id
    assert data["task_type"] == "library_rescan"
    assert data["source"] == "local"


def test_tasks_api_get_unknown_task_404(durable_jobs_db):
    """不存在的 id：GET 与 cancel 都返回 404。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/api/tasks/definitely_not_a_task_id").status_code == 404
    assert client.post("/api/tasks/definitely_not_a_task_id/cancel").status_code == 404


def test_tasks_api_cancel_durable_queued_job(durable_jobs_db):
    """queued durable job cancel → 直接终态 cancelled。"""
    from fastapi.testclient import TestClient
    from app.jobs import store as job_store
    from app.main import app

    job = _create_durable_job()
    client = TestClient(app)
    response = client.post(f"/api/tasks/{job.job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["task_id"] == job.job_id
    assert response.json()["status"] == "cancelled"
    assert job_store.get_job(job.job_id).status == "cancelled"


def test_tasks_api_cancel_durable_running_requests_cancel(durable_jobs_db):
    """running durable job cancel → 协作式 cancel_requested=True（状态不变）。"""
    from fastapi.testclient import TestClient
    from app.jobs import store as job_store
    from app.main import app

    job = _create_durable_job()
    claimed = job_store.claim_jobs("test-worker")
    assert claimed and claimed[0].job_id == job.job_id
    client = TestClient(app)
    response = client.post(f"/api/tasks/{job.job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    reloaded = job_store.get_job(job.job_id)
    assert reloaded is not None
    assert reloaded.cancel_requested is True


def test_tasks_api_cancel_terminal_durable_job_keeps_result(durable_jobs_db):
    """terminal durable job 的 cancel 请求不篡改历史结果。"""
    from fastapi.testclient import TestClient
    from app.jobs import store as job_store
    from app.jobs.models import SUCCEEDED
    from app.main import app

    job = _create_durable_job()
    claimed = job_store.claim_jobs("test-worker")
    job_store.finish_job(job.job_id, "test-worker", SUCCEEDED, version=claimed[0].version)
    client = TestClient(app)
    response = client.post(f"/api/tasks/{job.job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert job_store.get_job(job.job_id).status == SUCCEEDED


def test_tasks_api_list_includes_durable_job(durable_jobs_db):
    """list 合并 durable job（Job.to_record_dict 形状，queued → pending）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    job = _create_durable_job(job_type="scrape_revision", source="tmdb")
    client = TestClient(app)
    response = client.get("/api/tasks?limit=50")
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    durable_items = [item for item in tasks if item["task_id"] == job.job_id]
    assert durable_items, f"durable job 未出现在列表中: {[t['task_id'] for t in tasks]}"
    item = durable_items[0]
    assert item["task_type"] == "scrape_revision"
    assert item["source"] == "tmdb"
    assert item["status"] == "pending"  # 列表出口同样适配 queued → pending


def test_tasks_api_list_filters_durable_job(durable_jobs_db):
    """source/task_type/type_prefix 过滤同样作用于 durable job。"""
    from fastapi.testclient import TestClient

    from app.main import app

    job = _create_durable_job(job_type="mirror_revision", source="pan115")
    client = TestClient(app)
    hit = client.get("/api/tasks?task_type=mirror_revision&source=pan115").json()["tasks"]
    assert any(item["task_id"] == job.job_id for item in hit)
    miss = client.get("/api/tasks?task_type=scrape_revision").json()["tasks"]
    assert not any(item["task_id"] == job.job_id for item in miss)
    prefixed = client.get("/api/tasks?type_prefix=mirror_").json()["tasks"]
    assert any(item["task_id"] == job.job_id for item in prefixed)
    other_source = client.get("/api/tasks?source=local").json()["tasks"]
    assert not any(item["task_id"] == job.job_id for item in other_source)


def test_tasks_api_durable_status_passthrough(durable_jobs_db):
    """running/succeeded/failed/cancelled 原样透传；仅 queued 适配为 pending。

    GET /api/tasks/{id}、列表、cancel 后返回都统一走同一适配（_durable_record_dict）。
    """
    from fastapi.testclient import TestClient

    from app.jobs import store as job_store
    from app.jobs.models import FAILED, RUNNING, SUCCEEDED
    from app.main import app

    client = TestClient(app)

    # running：领取后原样透传
    running_job = _create_durable_job(job_type="mirror_revision", source="pan115")
    claimed = job_store.claim_jobs("test-worker")
    assert claimed and claimed[0].job_id == running_job.job_id
    data = client.get(f"/api/tasks/{running_job.job_id}").json()
    assert data["status"] == RUNNING
    # 列表同样透传 running
    listed = client.get("/api/tasks?limit=100").json()["tasks"]
    listed_running = next(item for item in listed if item["task_id"] == running_job.job_id)
    assert listed_running["status"] == RUNNING

    # succeeded：终态原样透传
    ok_job = _create_durable_job(job_type="mirror_revision", source="pan115")
    claimed = job_store.claim_jobs("test-worker")
    assert claimed and claimed[0].job_id == ok_job.job_id
    assert job_store.finish_job(ok_job.job_id, "test-worker", SUCCEEDED, version=claimed[0].version)
    data = client.get(f"/api/tasks/{ok_job.job_id}").json()
    assert data["status"] == SUCCEEDED

    # failed：终态原样透传
    failed_job = _create_durable_job(job_type="mirror_revision", source="pan115")
    claimed = job_store.claim_jobs("test-worker")
    assert claimed and claimed[0].job_id == failed_job.job_id
    assert job_store.finish_job(
        failed_job.job_id, "test-worker", FAILED, version=claimed[0].version, error="boom"
    )
    data = client.get(f"/api/tasks/{failed_job.job_id}").json()
    assert data["status"] == FAILED

    # cancelled：cancel 响应透传 cancelled（内部也是 cancelled）
    cancelled_job = _create_durable_job(job_type="mirror_revision", source="pan115")
    data = client.post(f"/api/tasks/{cancelled_job.job_id}/cancel").json()
    assert data["status"] == "cancelled"
