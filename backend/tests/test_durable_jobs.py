"""任务 2 验收：可恢复持久任务队列。

覆盖：排队/领取、租约过期恢复、取消（queued/running 协作）、可重试重排、
依赖任务、版本 fence 阻止旧 worker 提交终态、模拟重启后继续、
取消不覆盖 succeeded、resource_key 互斥、payload 白名单。
"""

import time

import pytest

from app.db.database import close_connection, get_connection, init_db
from app.jobs import store
from app.jobs.models import (
    CANCELLED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    JobCancelledError,
)
from app.jobs.registry import register, unregister, validate_payload
from app.jobs.runner import JobRunner


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _expire_leases(*job_ids: str) -> None:
    """把任务租约改为已过期。"""
    from datetime import datetime, timedelta, timezone

    conn = get_connection()
    past = (datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=120)).isoformat()
    for job_id in job_ids:
        conn.execute(
            "UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, job_id)
        )
    conn.commit()


def _make_job(**kwargs):
    return store.create_job(
        job_type=kwargs.get("job_type", "test_job"),
        resource_key=kwargs.get("resource_key", ""),
        payload=kwargs.get("payload", {"source": "test"}),
        parent_job_id=kwargs.get("parent_job_id", ""),
        max_attempts=kwargs.get("max_attempts", 3),
    )


class TestQueueAndClaim:
    def test_create_and_claim(self, db):
        job = _make_job()
        assert store.get_job(job.job_id).status == QUEUED
        claimed = store.claim_jobs("w1")
        assert len(claimed) == 1
        assert claimed[0].job_id == job.job_id
        assert claimed[0].status == RUNNING
        assert claimed[0].lease_owner == "w1"
        assert claimed[0].version == 1

    def test_running_job_not_claimed_twice(self, db):
        _make_job()
        store.claim_jobs("w1")
        assert store.claim_jobs("w2") == []

    def test_resource_key_mutex(self, db):
        """同一 resource_key 只能有一个 running（互斥）。"""
        first = _make_job(resource_key="root-1")
        _make_job(resource_key="root-1")
        claimed = store.claim_jobs("w1", limit=5)
        assert len(claimed) == 1
        assert claimed[0].job_id == first.job_id
        # 完成后第二个才能领取
        store.finish_job(first.job_id, "w1", SUCCEEDED, version=claimed[0].version)
        claimed2 = store.claim_jobs("w1", limit=5)
        assert len(claimed2) == 1

    def test_dependency_parent_first(self, db):
        parent = _make_job()
        child = _make_job(parent_job_id=parent.job_id)
        claimed = store.claim_jobs("w1", limit=5)
        assert [item.job_id for item in claimed] == [parent.job_id]
        # 父未完成时子不可领取
        assert store.claim_jobs("w1", limit=5) == []
        store.finish_job(parent.job_id, "w1", SUCCEEDED, version=claimed[0].version)
        claimed2 = store.claim_jobs("w1", limit=5)
        assert [item.job_id for item in claimed2] == [child.job_id]


class TestLeaseAndRecovery:
    def test_expired_lease_requeued_not_failed(self, db):
        job = _make_job()
        store.claim_jobs("w1")
        # 租约过期
        from datetime import datetime, timedelta, timezone

        past = (datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=120)).isoformat()
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, job.job_id)
        )
        conn.commit()
        recovered = store.requeue_expired_leases()
        assert recovered == 1
        job_after = store.get_job(job.job_id)
        assert job_after.status == QUEUED
        assert job_after.lease_owner == ""

    def test_restart_continues_job(self, db):
        """模拟重启：新 worker 可领取同一任务并完成。"""
        job = _make_job()
        store.claim_jobs("old-worker")
        _expire_leases(job.job_id)
        store.requeue_expired_leases()  # 启动恢复（模拟时间已过期）
        claimed = store.claim_jobs("new-worker")
        assert claimed and claimed[0].job_id == job.job_id
        ok = store.finish_job(job.job_id, "new-worker", SUCCEEDED, version=claimed[0].version)
        assert ok is True
        assert store.get_job(job.job_id).status == SUCCEEDED


class TestVersionFence:
    def test_old_worker_cannot_finish_after_lease_moved(self, db):
        job = _make_job()
        claimed = store.claim_jobs("w1")
        # 租约被抢（新 worker 领取；旧 worker 仍持有旧 version）
        _expire_leases(job.job_id)
        store.requeue_expired_leases()
        store.claim_jobs("w2")
        # 旧 worker 用旧版本提交终态 → 拒绝
        ok = store.finish_job(job.job_id, "w1", SUCCEEDED, version=claimed[0].version)
        assert ok is False
        assert store.get_job(job.job_id).status != SUCCEEDED

    def test_cancel_does_not_override_succeeded(self, db):
        job = _make_job()
        claimed = store.claim_jobs("w1")
        store.finish_job(job.job_id, "w1", SUCCEEDED, version=claimed[0].version)
        assert store.cancel_job(job.job_id) is False
        assert store.get_job(job.job_id).status == SUCCEEDED


class TestCancelAndRetry:
    def test_cancel_queued_job(self, db):
        job = _make_job()
        assert store.cancel_job(job.job_id) is True
        assert store.get_job(job.job_id).status == CANCELLED

    def test_cancel_running_job_cooperative(self, db):
        job = _make_job()
        store.claim_jobs("w1")
        assert store.cancel_job(job.job_id) is True
        assert store.should_cancel(job.job_id) is True

    def test_retry_requeues_attempt(self, db):
        job = _make_job(max_attempts=3)
        store.claim_jobs("w1")
        ok = store.retry_job(job.job_id, "w1", error="网络抖动", error_type="network")
        assert ok is True
        job_after = store.get_job(job.job_id)
        assert job_after.status == QUEUED
        assert job_after.attempt == 1


class TestRunner:
    def test_runner_executes_handler(self, db):
        executed = {}

        def handler(payload, progress_callback=None, should_cancel=None):
            executed["source"] = payload.get("source")
            progress_callback(50, "进行中")
            return {"ok": True}

        register("test_handler", handler)
        try:
            job = store.create_job(job_type="test_handler", payload={"source": "local"})
            runner = JobRunner(poll_interval=0.02)
            runner.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if store.get_job(job.job_id).status == SUCCEEDED:
                    break
                time.sleep(0.05)
            runner.stop()
            final = store.get_job(job.job_id)
            assert final.status == SUCCEEDED
            assert executed.get("source") == "local"
            assert final.result.get("ok") is True
        finally:
            unregister("test_handler")

    def test_runner_cancelled_handler(self, db):
        def handler(payload, progress_callback=None, should_cancel=None):
            raise JobCancelledError("用户取消")

        register("test_cancel_handler", handler)
        try:
            job = store.create_job(job_type="test_cancel_handler")
            runner = JobRunner(poll_interval=0.02)
            runner.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if store.get_job(job.job_id).status == CANCELLED:
                    break
                time.sleep(0.05)
            runner.stop()
            assert store.get_job(job.job_id).status == CANCELLED
        finally:
            unregister("test_cancel_handler")


class TestPayloadWhitelist:
    def test_validate_payload_accepts_json_types(self):
        payload = {"a": 1, "b": "x", "c": [1, 2], "d": {"e": None}}
        assert validate_payload(payload) == payload


    def test_validate_payload_rejects_objects(self):
        with pytest.raises(ValueError, match="不允许的类型"):
            validate_payload({"fn": object()})


class TestMainRegistration:
    def test_main_import_registers_pipeline_handlers(self):
        """启动 worker 前必须已注册 durable handlers，否则重启恢复的任务会直接失败。"""
        import app.main  # noqa: F401
        from app.jobs.registry import registered_names

        names = registered_names()
        assert "mirror_revision" in names
        assert "scrape_revision" in names
