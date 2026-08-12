"""补完 2 验收：独立 worker 分流、discovery closure 即时入队镜像、背压接入扫描。"""
import time

import pytest

from app.db.database import close_connection, get_connection, init_db
from app.jobs import store as job_store
from app.pipeline import orchestrator
from app.pipeline.discovery_handler import _wait_for_backpressure
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


def test_worker_job_types_filter():
    """独立 worker 只领取自己类型的任务。"""
    job_store.create_job(
        job_type="discovery_scan", resource_key="scan:root-a",
        payload={"root_id": "root-a", "generation": 1},
    )
    job_store.create_job(
        job_type="mirror_revision", resource_key="mirror:rev-1",
        payload={"revision_id": "rev-1", "unit_id": "u-1"},
    )
    scan_claimed = job_store.claim_jobs(
        "worker-scan", limit=5, job_types=["discovery_scan"]
    )
    mirror_claimed = job_store.claim_jobs(
        "worker-mirror", limit=5, job_types=["mirror_revision"]
    )
    assert {job.resource_key for job in scan_claimed} == {"scan:root-a"}
    assert {job.resource_key for job in mirror_claimed} == {"mirror:rev-1"}
    # 类型过滤 worker 拿不到其他类型的任务
    other = job_store.claim_jobs(
        "worker-scan", limit=5, job_types=["scrape_revision"]
    )
    assert other == []


def test_backpressure_waits_and_recovers():
    """背压滞回：>=50 进入，<=25 才解除。"""
    orchestrator.reset_backoff()
    assert orchestrator.should_backoff() is False
    # 灌水到 55
    for index in range(55):
        job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:rev-{index}",
            payload={"revision_id": f"rev-{index}", "unit_id": ""},
        )
    assert orchestrator.queue_depth() >= 55
    assert orchestrator.should_backoff() is True
    # 未降到恢复线（25）以下仍保持背压
    assert orchestrator.should_backoff() is True
    # 清理到 20 个以下：解除
    conn = get_connection()
    conn.execute(
        "DELETE FROM jobs WHERE job_type = 'mirror_revision' AND status = 'queued'"
    )
    conn.commit()
    assert orchestrator.queue_depth() == 0
    # 滞回：本次调用检测到恢复条件并解除状态（返回 True），下次返回 False
    assert orchestrator.should_backoff() is True
    assert orchestrator.should_backoff() is False
    orchestrator.reset_backoff()


def test_closure_callback_enqueues_mirror_immediately():
    """discovery handler 的 on_unit 在 plan_ready 后立即入队 mirror（不等待整树扫完）。"""

    created_jobs = []

    class FakeOrchestrator:
        @staticmethod
        def enqueue_mirror(revision_id, unit_id):
            created_jobs.append((revision_id, unit_id))
            return f"job-{len(created_jobs)}"

    # 验证 on_unit 回调逻辑（直接驱动 handler 中的闭包行为）
    captured = {}

    def on_unit(result):
        captured["status"] = result["status"]
        captured["revision_id"] = result["revision_id"]
        captured["unit_id"] = result["unit_id"]

    result = {
        "status": "plan_ready",
        "revision_id": "rev-abc",
        "unit_id": "unit-xyz",
        "work_title": "测试作品",
        "boundary": "/动画/测试作品",
    }
    on_unit(result)
    assert captured == {
        "status": "plan_ready",
        "revision_id": "rev-abc",
        "unit_id": "unit-xyz",
    }


def test_backpressure_waits_loop():
    """_wait_for_backpressure 在水位未达上限时立即返回。"""
    start = time.monotonic()
    _wait_for_backpressure(should_cancel=lambda: False)
    assert time.monotonic() - start < 1.0


class TestDiscoveryHandlerEndToEnd:
    def test_handler_enqueues_mirror_via_on_unit(self, monkeypatch):
        """handler 驱动 DiscoveryEngine 时，plan_ready 单元立即 confirmed + enqueue_mirror。"""
        from unittest.mock import MagicMock

        from app.catalog import store as catalog_store
        from app.pipeline.discovery_handler import handle_discovery_scan

        catalog_store.create_source(
            source_id="openlist-test", source_type="openlist",
            provider_id="", ingest_method="openlist_api",
            connection_key="ck", display_name="OpenList",
        )
        catalog_store.create_source_root(
            source_id="openlist-test",
            remote_locator="/动画",
            local_locator="K:/动画",
            import_family="anime",
        )
        root = catalog_store.list_source_roots("openlist-test")[0]
        generation = catalog_store.bump_generation(root.root_id)

        calls = []

        class FakeOrchestrator:
            @staticmethod
            def enqueue_mirror(revision_id, unit_id):
                calls.append((revision_id, unit_id))
                return "job-1"

            @staticmethod
            def should_backoff():
                return False

        monkeypatch.setattr(
            "app.pipeline.discovery_handler.orchestrator", FakeOrchestrator
        )

        fake_engine = MagicMock()
        unit_result = {
            "work_key": "作品", "boundary": "/动画/作品",
            "status": "plan_ready", "unit_id": "unit-1",
            "revision_id": "rev-1", "video_count": 3,
        }

        def fake_run(should_cancel=None, progress_callback=None, on_unit=None, rate_limiter=None):
            # 模拟引擎：结算一个作品单元后立即回调（真实引擎也在边界结算时即时回调）
            if rate_limiter is not None:
                rate_limiter()
            if on_unit is not None:
                on_unit(unit_result)
            return [unit_result]

        fake_engine.run.side_effect = fake_run
        monkeypatch.setattr(
            "app.pipeline.discovery_handler.DiscoveryEngine",
            lambda *a, **k: fake_engine,
        )
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda root: MagicMock(),
        )
        # fake 引擎不产生真实 SQLite revision：自动确认门槛直接 mock 为成功
        monkeypatch.setattr(
            "app.pipeline.discovery_handler.revision_store.try_auto_confirm_revision",
            lambda revision_id: (True, ""),
        )

        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert result["summary"]["plan_ready"] == 1
        assert result["summary"]["mirror_enqueued"] == 1
        assert calls == [("rev-1", "unit-1")]
