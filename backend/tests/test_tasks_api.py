# -*- coding: utf-8 -*-
"""Unified task API tests."""

import sys
from pathlib import Path

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
