# -*- coding: utf-8 -*-
"""数据库模块测试"""

import json
import time
import pytest
from pathlib import Path

from app.db.database import init_db, get_connection, close_connection


@pytest.fixture
def db(tmp_path, monkeypatch):
    """使用临时数据库"""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    # 重置线程本地连接
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield db_path
    close_connection()


# ============================================================
# 初始化测试
# ============================================================

class TestInitDb:
    """测试数据库初始化"""

    def test_creates_tables(self, db):
        """应创建所有表"""
        conn = get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}

        assert "tasks" in table_names
        assert "playback_history" in table_names
        assert "scrape_candidate_cache" in table_names
        assert "scrape_review_queue" in table_names
        assert "failed_cases" in table_names

    def test_idempotent(self, db):
        """重复初始化不应报错"""
        init_db()
        init_db()

        conn = get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(tables) >= 5


# ============================================================
# Tasks 测试
# ============================================================

class TestTasks:
    """测试任务存储"""

    def test_save_and_get_task(self, db):
        """保存和获取任务"""
        from app.db.tasks import save_task, get_task

        task = {
            "task_id": "task_001",
            "task_type": "mirror_generate",
            "source": "pan115",
            "status": "pending",
            "progress": 0,
            "message": "等待中",
            "created_at": "2026-06-15T10:00:00+08:00",
        }
        save_task(task)

        result = get_task("task_001")
        assert result is not None
        assert result["task_id"] == "task_001"
        assert result["status"] == "pending"

    def test_update_task_status(self, db):
        """更新任务状态"""
        from app.db.tasks import save_task, get_task, update_task_status

        save_task({
            "task_id": "task_002",
            "task_type": "scrape",
            "source": "pan115",
            "created_at": "2026-06-15T10:00:00+08:00",
        })

        update_task_status("task_002", "running", progress=50, message="处理中")
        result = get_task("task_002")
        assert result["status"] == "running"
        assert result["progress"] == 50

    def test_list_tasks(self, db):
        """列出任务"""
        from app.db.tasks import save_task, list_tasks

        for i in range(5):
            save_task({
                "task_id": f"task_{i:03d}",
                "task_type": "test",
                "source": "pan115",
                "created_at": f"2026-06-15T10:{i:02d}:00+08:00",
            })

        tasks = list_tasks()
        assert len(tasks) == 5

    def test_list_tasks_filter(self, db):
        """按类型筛选任务"""
        from app.db.tasks import save_task, list_tasks

        save_task({"task_id": "t1", "task_type": "mirror", "source": "pan115", "created_at": "2026-06-15T10:00:00+08:00"})
        save_task({"task_id": "t2", "task_type": "scrape", "source": "pan115", "created_at": "2026-06-15T10:01:00+08:00"})

        tasks = list_tasks(task_type="mirror")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "t1"

    def test_task_manager_double_writes_sqlite(self, db):
        """TaskManager 状态变化应双写 SQLite"""
        from app.db.tasks import get_task
        from app.tasks.manager import TaskManager

        manager = TaskManager(max_workers=1)
        record = manager.submit("scrape_auto", "pan115", lambda: {"ok": True})

        saved = None
        for _ in range(30):
            saved = get_task(record.task_id)
            if saved and saved["status"] == "succeeded":
                break
            time.sleep(0.05)

        manager.shutdown()
        assert saved is not None
        assert saved["status"] == "succeeded"
        assert saved["result"] == {"ok": True}

    def test_mark_interrupted_tasks_failed(self, db):
        """后端重启后，旧 pending/running 任务不能继续显示为进行中。"""
        from app.db.tasks import get_task, mark_interrupted_tasks_failed, save_task

        for task_id, status in (("stale_pending", "pending"), ("stale_running", "running")):
            save_task({
                "task_id": task_id,
                "task_type": "scrape_auto",
                "source": "pan115",
                "status": status,
                "created_at": "2026-07-05T10:00:00+08:00",
            })

        assert mark_interrupted_tasks_failed() == 2
        for task_id in ("stale_pending", "stale_running"):
            task = get_task(task_id)
            assert task["status"] == "failed"
            assert task["progress"] == 100
            assert task["message"] == "后端已重启，扫描未完成。请重新扫描该文件夹。"


# ============================================================
# Playback History 测试
# ============================================================

class TestPlaybackHistory:
    """测试播放历史"""

    def test_save_and_get_history(self, db):
        """保存和获取播放历史"""
        from app.db.history import save_playback_history, get_playback_history

        save_playback_history({
            "history_id": "h001",
            "work_id": "w001",
            "work_title": "CLANNAD",
            "episode_id": "e001",
            "episode_title": "第1话",
            "strm_path": "/path/to/ep01.strm",
            "played_at": "2026-06-15T10:00:00+08:00",
        })

        history = get_playback_history()
        assert len(history) == 1
        assert history[0]["work_title"] == "CLANNAD"

    def test_get_continue_item(self, db):
        """获取继续播放条目"""
        from app.db.history import save_playback_history, get_continue_item

        save_playback_history({
            "history_id": "h001",
            "work_id": "w001",
            "episode_id": "e001",
            "played_at": "2026-06-15T10:00:00+08:00",
        })
        save_playback_history({
            "history_id": "h002",
            "work_id": "w001",
            "episode_id": "e002",
            "played_at": "2026-06-15T11:00:00+08:00",
        })

        item = get_continue_item("w001")
        assert item is not None
        assert item["episode_id"] == "e002"  # 最新的

    def test_filter_by_work_id(self, db):
        """按 work_id 筛选"""
        from app.db.history import save_playback_history, get_playback_history

        save_playback_history({"history_id": "h1", "work_id": "w1", "played_at": "2026-06-15T10:00:00+08:00"})
        save_playback_history({"history_id": "h2", "work_id": "w2", "played_at": "2026-06-15T10:01:00+08:00"})

        history = get_playback_history(work_id="w1")
        assert len(history) == 1
        assert history[0]["work_id"] == "w1"


# ============================================================
# Scrape SQLite 双写测试
# ============================================================

class TestScrapePersistence:
    """测试刮削相关 JSON 主流程的 SQLite 双写"""

    def test_candidate_cache_roundtrip(self, db):
        """候选缓存应可保存并读取"""
        from app.db.candidates import list_candidates, save_candidates
        from app.scrape.models import ScrapeCandidate

        save_candidates([
            ScrapeCandidate(
                candidate_id="c1",
                scrape_target_id="target_1",
                provider="tmdb",
                tmdb_id=12189,
                tmdb_type="tv",
                title="CLANNAD",
                original_title="CLANNAD",
                year=2007,
                overview="",
                poster_path="/poster.jpg",
                popularity=10,
                vote_average=8.0,
                score=90,
                reasons=["标题完全匹配"],
            )
        ])

        rows = list_candidates("target_1")
        assert len(rows) == 1
        assert rows[0]["tmdb_id"] == 12189
        assert rows[0]["score"] == 90

    def test_review_queue_double_writes_sqlite(self, db, tmp_path, monkeypatch):
        """review_queue.json 写入时应同步写 SQLite"""
        from app.db.database import get_connection
        from app.scrape.models import ScrapeCandidate, ScrapeTarget
        from app.scrape.review_queue import add_to_review_queue

        monkeypatch.setattr(
            "app.scrape.review_queue._get_queue_path",
            lambda: tmp_path / "review_queue.json",
        )
        target = ScrapeTarget(
            scrape_target_id="target_review",
            source="pan115",
            import_plan_id="plan_1",
            work_id="work_1",
            series_group="CLANNAD",
            local_title="CLANNAD",
            scrape_title="CLANNAD",
            scrape_year=2007,
            scrape_type="tv",
            local_season_number=1,
        )
        candidate = ScrapeCandidate(
            candidate_id="c1",
            scrape_target_id="target_review",
            provider="tmdb",
            tmdb_id=12189,
            tmdb_type="tv",
            title="CLANNAD",
            score=20,
        )

        add_to_review_queue(target, "分数不足", [candidate])

        row = get_connection().execute(
            "SELECT * FROM scrape_review_queue WHERE scrape_target_id = ?",
            ("target_review",),
        ).fetchone()
        assert row is not None
        assert row["reason"] == "分数不足"
        assert row["status"] == "pending"

    def test_failed_case_double_writes_sqlite(self, db, tmp_path, monkeypatch):
        """failed_cases.json 写入时应同步写 SQLite"""
        from app.db.database import get_connection
        from app.scrape.store import save_failed_case

        monkeypatch.setattr("app.scrape.store._get_scrape_dir", lambda: tmp_path)
        save_failed_case({
            "scrape_target_id": "target_failed",
            "tmdb_id": 12189,
            "tmdb_type": "tv",
            "error": "boom",
            "stage": "auto_execute",
            "timestamp": "2026-06-15T10:00:00+08:00",
        })

        row = get_connection().execute(
            "SELECT * FROM failed_cases WHERE scrape_target_id = ?",
            ("target_failed",),
        ).fetchone()
        assert row is not None
        assert row["error"] == "boom"
        assert row["stage"] == "auto_execute"
