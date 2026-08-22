"""任务 5 验收：流水并行、刮削状态收口与投影重建。

覆盖：scan/mirror/scrape 队列与互斥、A 刮削时 scanner 继续 B/C、
未 closure 不产生 scrape、背压水位、幂等 artifact 登记、重启恢复。
"""

import time
from unittest.mock import patch

import pytest

from app.db.database import close_connection, get_connection, init_db
from app.import_plan import revision_store
from app.jobs import store as job_store
from app.jobs.runner import JobRunner
from app.pipeline import orchestrator
from app.pipeline.handlers import register_pipeline_handlers


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    register_pipeline_handlers()
    yield
    close_connection()




def _set_current(unit_id: str, revision_id: str) -> None:
    """Module 5 execution fence：mirror/scrape 只消费 current revision。"""
    conn = get_connection()
    conn.execute(
        "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
        (revision_id, unit_id),
    )
    conn.commit()


def _ensure_unit(unit_id: str, boundary: str = "/动画/作品", root_id: str = "root-x") -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, closure_generation, current_revision_id, created_at, updated_at
        ) VALUES (?, '', ?, '', ?, 'w', 'discovered', 0, '', ?, ?)
        """,
        (unit_id, root_id, boundary, "2026-08-11T00:00:00+08:00", "2026-08-11T00:00:00+08:00"),
    )
    conn.commit()


def _make_items(paths):
    return [
        {
            "id": f"i-{index}", "source": "openlist", "provider_id": "quark",
            "relative_path": path, "real_path": path, "logical_locator": path,
            "resource_type": "video", "action": "generate_strm",
            "work_id": "w1", "work_title": "作品", "series_group": "作品",
            "group_type": "season", "season_number": 1, "episode_number": index + 1,
            "title": "", "target_dir": "", "target_strm_path": f"mirror/作品/S01/{path}",
            "confidence": "high", "needs_review": False, "availability": "available",
        }
        for index, path in enumerate(paths)
    ]


class TestPipelineJobs:
    def test_scan_and_scrape_jobs_parallel(self):
        """A 刮削（scrape running）时，B 的 scan job（不同 resource_key）仍可领取。"""
        job_store.create_job(
            job_type="discovery_scan", resource_key="scan:root-b",
            payload={"root_id": "root-b", "generation": 1},
        )
        job_store.create_job(
            job_type="scrape_revision", resource_key="scrape:global",
            payload={"revision_id": "rev-a", "source": "openlist"},
        )
        # 两个不同 worker 各自领取（不同 resource_key 不互斥）
        runner_a = JobRunner(worker_id="worker-a", poll_interval=0.01)
        runner_b = JobRunner(worker_id="worker-b", poll_interval=0.01)
        try:
            claimed_a = job_store.claim_jobs("worker-a", limit=5)
            claimed_b = job_store.claim_jobs("worker-b", limit=5)
            keys = {item.resource_key for item in claimed_a + claimed_b}
            assert keys == {"scan:root-b", "scrape:global"}
        finally:
            runner_a.stop()
            runner_b.stop()

    def test_mirror_handler_registers_artifacts(self, tmp_path):
        """mirror 成功后登记 artifact_records（幂等：重复执行不重复登记）。"""
        _ensure_unit("unit-1")
        revision = revision_store.create_revision(
            unit_id="unit-1", source_generation=1, items=_make_items(["a.mkv"]),
            status="confirmed",
        )
        _set_current("unit-1", revision["revision_id"])
        plan = revision_store.load_plan(revision["revision_id"])
        # Review Fix：artifact 登记以 MirrorItemResult.strm_path 为准且必须真实存在
        generated_strm = tmp_path / "mirror" / "作品" / "S01" / "a.strm"
        generated_strm.parent.mkdir(parents=True, exist_ok=True)
        generated_strm.write_text("K:/115动画/a.mkv", encoding="utf-8")

        from app.mirror.result import MirrorGenerateResult, MirrorItemResult

        fake_result = MirrorGenerateResult(
            plan_id=revision["revision_id"], source="openlist",
            mirror_root="K:/mirror", status="success",
            generated_count=1, items=[
                MirrorItemResult(
                    item_id="i-0", source="openlist", status="generated",
                    strm_path=str(generated_strm), real_path="a.mkv",
                ),
            ],
        )
        with patch("app.mirror.generator.generate_mirror", return_value=fake_result) as mock_mirror:
            job = job_store.create_job(
                job_type="mirror_revision", resource_key=f"mirror:{revision['revision_id']}",
                payload={"revision_id": revision["revision_id"], "unit_id": "unit-1"},
            )
            runner = JobRunner(worker_id="w1", poll_interval=0.02)
            runner.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if job_store.get_job(job.job_id).status in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            runner.stop()
            assert mock_mirror.called
            assert job_store.get_job(job.job_id).status == "succeeded"

        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM artifact_records WHERE revision_id = ?",
            (revision["revision_id"],),
        ).fetchone()[0]
        assert count == 1
        # 幂等：再次登记不重复
        from app.pipeline.handlers import _register_artifacts
        _register_artifacts(revision["revision_id"], plan, set())
        assert conn.execute(
            "SELECT COUNT(*) FROM artifact_records WHERE revision_id = ?",
            (revision["revision_id"],),
        ).fetchone()[0] == 1

    def test_closure_required_for_scrape(self):
        """未 closure（boundary 自身缺失或有 queued 目录）→ unit_is_closed=False。"""
        _ensure_unit("unit-open", boundary="/动画/作品", root_id="root-x")
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO source_directories (
                root_id, remote_path, parent_path, depth, state
            ) VALUES ('root-x', '/动画/作品', '/动画', 1, 'complete')
            """
        )
        conn.execute(
            """
            INSERT INTO source_directories (
                root_id, remote_path, parent_path, depth, state
            ) VALUES (
                'root-x',
                '/动画/作品/Season 1',
                '/动画/作品',
                2,
                'queued'
            )
            """
        )
        conn.commit()

        assert orchestrator.unit_is_closed("unit-open") is False

        conn.execute(
            """
            UPDATE source_directories
            SET state = 'complete'
            WHERE remote_path = '/动画/作品/Season 1'
            """
        )
        conn.commit()

        assert orchestrator.unit_is_closed("unit-open") is True

    def test_failed_dir_blocks_scrape_after_mirror(self):
        """同一作品下有 failed 目录（OVA 失败）→ mirror 成功后也不 enqueue scrape。"""
        _ensure_unit("unit-bad", boundary="/动画/作品", root_id="root-x")
        conn = get_connection()
        for path, state in (
            ("/动画/作品", "complete"),
            ("/动画/作品/Season 1", "complete"),
            ("/动画/作品/Season 2", "complete"),
            ("/动画/作品/OVA", "failed"),
        ):
            conn.execute(
                """
                INSERT INTO source_directories (
                    root_id, remote_path, parent_path, depth, state
                ) VALUES ('root-x', ?, '', 0, ?)
                """,
                (path, state),
            )
        conn.commit()
        assert orchestrator.unit_is_closed("unit-bad") is False

        revision = revision_store.create_revision(
            unit_id="unit-bad", source_generation=1, items=_make_items(["a.mkv"]),
            status="confirmed",
        )
        _set_current("unit-bad", revision["revision_id"])
        from app.mirror.result import MirrorGenerateResult

        fake_result = MirrorGenerateResult(
            plan_id=revision["revision_id"], source="openlist",
            mirror_root="K:/mirror", status="success",
            generated_count=1, items=[],
        )
        with patch("app.mirror.generator.generate_mirror", return_value=fake_result) as mock_mirror, \
                patch("app.pipeline.orchestrator.enqueue_scrape", return_value="job-scrape") as mock_scrape:
            job = job_store.create_job(
                job_type="mirror_revision", resource_key=f"mirror:{revision['revision_id']}",
                payload={"revision_id": revision["revision_id"], "unit_id": "unit-bad"},
            )
            runner = JobRunner(worker_id="w-bad", poll_interval=0.02)
            runner.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if job_store.get_job(job.job_id).status in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            runner.stop()
            assert mock_mirror.called
            assert job_store.get_job(job.job_id).status == "succeeded"
        # mirror 成功但不完整 → 不 enqueue scrape（同一作品存在 failed 目录）
        mock_scrape.assert_not_called()

    def test_complete_boundary_enqueues_scrape_after_mirror(self):
        """boundary 下所有目录 complete → mirror 成功后 enqueue scrape。"""
        _ensure_unit("unit-ok", boundary="/动画/作品", root_id="root-x")
        conn = get_connection()
        for path, state in (
            ("/动画/作品", "complete"),
            ("/动画/作品/Season 1", "complete"),
        ):
            conn.execute(
                """
                INSERT INTO source_directories (
                    root_id, remote_path, parent_path, depth, state
                ) VALUES ('root-x', ?, '', 0, ?)
                """,
                (path, state),
            )
        conn.commit()
        assert orchestrator.unit_is_closed("unit-ok") is True

        revision = revision_store.create_revision(
            unit_id="unit-ok", source_generation=1, items=_make_items(["a.mkv"]),
            status="confirmed",
        )
        _set_current("unit-ok", revision["revision_id"])
        from app.mirror.result import MirrorGenerateResult

        fake_result = MirrorGenerateResult(
            plan_id=revision["revision_id"], source="openlist",
            mirror_root="K:/mirror", status="success",
            generated_count=1, items=[],
        )
        with patch("app.mirror.generator.generate_mirror", return_value=fake_result) as mock_mirror, \
                patch("app.pipeline.orchestrator.enqueue_scrape", return_value="job-scrape") as mock_scrape:
            job = job_store.create_job(
                job_type="mirror_revision", resource_key=f"mirror:{revision['revision_id']}",
                payload={"revision_id": revision["revision_id"], "unit_id": "unit-ok"},
            )
            runner = JobRunner(worker_id="w-ok", poll_interval=0.02)
            runner.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if job_store.get_job(job.job_id).status in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            runner.stop()
            assert mock_mirror.called
            assert job_store.get_job(job.job_id).status == "succeeded"
        # 完整 → mirror 成功后 enqueue scrape（携带同一 revision_id / unit_id）
        mock_scrape.assert_called_once()
        call = mock_scrape.call_args
        assert call.args[0] == revision["revision_id"]
        assert call.kwargs.get("unit_id") == "unit-ok"

    def test_queue_backpressure(self):
        """待处理 mirror/scrape 达到 50 时 scanner 暂停。"""
        for index in range(49):
            job_store.create_job(job_type="mirror_revision", resource_key=f"mirror:r{index}", payload={"revision_id": f"r{index}"})
        assert orchestrator.should_backoff() is False
        job_store.create_job(job_type="scrape_revision", resource_key="scrape:global", payload={"revision_id": "r50"})
        assert orchestrator.queue_depth() >= 50
        assert orchestrator.should_backoff() is True

    def test_durable_job_resumes_after_restart(self):
        """模拟重启：pipeline job 由新 worker 恢复执行。"""
        _ensure_unit("unit-2")
        revision = revision_store.create_revision(
            unit_id="unit-2", source_generation=1, items=_make_items(["b.mkv"]),
            status="confirmed",
        )
        _set_current("unit-2", revision["revision_id"])
        job = job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:{revision['revision_id']}",
            payload={"revision_id": revision["revision_id"], "unit_id": "unit-2"},
        )
        job_store.claim_jobs("old-worker")
        # 租约过期（模拟重启；先记录旧 worker 领取后的版本）
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=120)).isoformat()
        get_connection().execute("UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, job.job_id))
        get_connection().commit()
        job_store.requeue_expired_leases()

        from app.mirror.result import MirrorGenerateResult

        fake_result = MirrorGenerateResult(
            plan_id=revision["revision_id"], source="openlist",
            mirror_root="K:/mirror", status="success", generated_count=1,
        )
        with patch("app.mirror.generator.generate_mirror", return_value=fake_result):
            runner = JobRunner(worker_id="new-worker", poll_interval=0.02)
            runner.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if job_store.get_job(job.job_id).status in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            runner.stop()
        assert job_store.get_job(job.job_id).status == "succeeded"
