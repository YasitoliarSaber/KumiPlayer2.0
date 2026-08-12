# -*- coding: utf-8 -*-
"""模块 1 Review Fix（R2）：discovery_scan 冷却延后与风险类传播测试。

OpenList 连接处于 cooling_down 时，``handle_discovery_scan`` 必须：
- 不构造扫描器、不跑 DiscoveryEngine、不发任何远程请求；
- **抛出 JobDeferredError**（job 回到 queued + not_before=cooldown_until，
  不标 succeeded/failed、不消耗 attempt）；
- 保留 Source Catalog 已有数据（不删除）。

扫描中途 405 风控：整棵扫描立即中止，下一个目录零请求（回归）。
"""

import pytest

from app.catalog import source_health
from app.db.database import close_connection, init_db
from app.integrations.openlist.governor import governor_connection_key
from app.jobs.models import JobDeferredError


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """临时 SQLite（隔离 source_health 表与 catalog 表）。"""
    db_path = tmp_path / "discovery_cooldown.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


class _FakeConfig:
    openlist_server_url = "https://ol.example.com"
    openlist_username = "quark-user"
    openlist_password = "p@ss"


def _make_root():
    from app.catalog import store as catalog_store

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
    return root, generation


class _FakeEngine:
    """不产生任何网络行为的最小引擎替身。"""

    def run(self, should_cancel=None, progress_callback=None, on_unit=None, rate_limiter=None):
        return []


def _patch_config(monkeypatch):
    """让 handler 冷却检查读取到假配置（与 _build_scanner 同源）。"""
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: _FakeConfig(),
    )


class TestDiscoveryCooldown:
    def test_cooling_down_raises_deferred(self, monkeypatch):
        """冷却中：不构造扫描器（monkeypatch 抛异常反证），抛 JobDeferredError。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)

        def _must_not_build(*args, **kwargs):
            raise AssertionError("冷却中不应构造扫描器")

        monkeypatch.setattr("app.pipeline.discovery_handler._build_scanner", _must_not_build)

        key = governor_connection_key(
            _FakeConfig.openlist_server_url, _FakeConfig.openlist_username
        )
        source_health.enter_cooldown(
            key,
            reason_kind="risk_control",
            cooldown_seconds=3600,
        )

        with pytest.raises(JobDeferredError) as exc:
            handle_discovery_scan(
                {"root_id": root.root_id, "generation": generation},
                progress_callback=lambda *a, **k: None,
                should_cancel=lambda: False,
            )
        record = source_health.get_health(key)
        assert exc.value.until_unix == pytest.approx(record.cooldown_until)
        assert "访问保护" in exc.value.message

    def test_cooling_down_with_429_reason(self, monkeypatch):
        """429 触发的冷却同样延后 discovery（reason_kind 仅记录，不改变行为）。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应构造扫描器")),
        )
        key = governor_connection_key(_FakeConfig.openlist_server_url, _FakeConfig.openlist_username)
        source_health.enter_cooldown(
            key,
            reason_kind="rate_limit",
            cooldown_seconds=3600,
        )
        with pytest.raises(JobDeferredError) as exc:
            handle_discovery_scan(
                {"root_id": root.root_id, "generation": generation},
                progress_callback=lambda *a, **k: None,
                should_cancel=lambda: False,
            )
        assert "访问保护" in exc.value.message

    def test_healthy_connection_still_scans(self, monkeypatch):
        """无冷却记录时保持原行为：正常构造扫描器并跑引擎。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)

        built: list = []
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda root: built.append(1) or object(),
        )
        monkeypatch.setattr(
            "app.pipeline.discovery_handler.DiscoveryEngine",
            lambda *a, **k: _FakeEngine(),
        )

        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert built == [1]  # 扫描器确实被构造
        assert result["summary"].get("health") != "cooling_down"
        assert result["units"] == []

    def test_cooldown_expired_becomes_probe_and_scans(self, monkeypatch):
        """冷却期结束后 can_request 放行（probe），扫描正常执行。"""
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        built: list = []
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda root: built.append(1) or object(),
        )
        monkeypatch.setattr(
            "app.pipeline.discovery_handler.DiscoveryEngine",
            lambda *a, **k: _FakeEngine(),
        )
        key = governor_connection_key(_FakeConfig.openlist_server_url, _FakeConfig.openlist_username)
        # 冷却窗口 [1000, 1100)：真实时钟已过期。
        # 单探针语义：探针许可由 handler 内部的 can_request 原子抢占，
        # 测试不得预先调用 can_request（否则会消耗唯一探针许可，handler 被拒）。
        source_health.enter_cooldown(
            key, reason_kind="risk_control",
            cooldown_seconds=100, now=1000.0,
        )
        result = handle_discovery_scan(
            {"root_id": root.root_id, "generation": generation},
            progress_callback=lambda *a, **k: None,
            should_cancel=lambda: False,
        )
        assert built == [1]
        # 冷却到期后的第一个调用（handler 内部）抢占为 probe，扫描正常执行
        assert source_health.get_health(key).state == source_health.STATE_PROBE


class TestMidScanRiskControlPropagation:
    """扫描中途 405 风控：整棵扫描立即中止，不继续扫下一个目录。"""

    def test_risk_control_stops_next_directory(self, monkeypatch):
        """A 成功 → B 返回 405 风控 → C 的 client 调用数 == 0（关键回归）。"""
        from app.catalog import store as catalog_store
        from app.catalog.scanner import SourceCatalogScanner
        from app.integrations.openlist.governor import OpenListRequestGovernor
        from app.integrations.openlist.models import (
            OpenListEntry,
            OpenListRiskControlError,
        )
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        key = governor_connection_key(
            _FakeConfig.openlist_server_url, _FakeConfig.openlist_username
        )

        calls: list[str] = []

        class _RiskControlClient:
            # 模拟真实 OpenListClient：携带 governor/conn_key（scanner 不再
            # 回退实例级计时），物理请求前向 source_health 上报。
            _governor = OpenListRequestGovernor(rate_per_second=1000)
            _conn_key = key

            def login(self):
                return "t"

            def list_dir(self, path, page=1, per_page=100, refresh=False):
                calls.append(path)
                if path == "/动画":
                    entries = [
                        OpenListEntry(name="B", is_dir=True, remote_path="/动画/B"),
                        OpenListEntry(name="C", is_dir=True, remote_path="/动画/C"),
                    ]
                elif path == "/动画/B":
                    # 与真实 OpenListClient 一致：先上报风控失败（进入冷却）再抛出
                    source_health.record_failure(key, "risk_control")
                    raise OpenListRiskControlError()
                else:
                    entries = []
                return type(
                    "Page",
                    (),
                    {"entries": entries, "total": len(entries)},
                )()

        fake_client = _RiskControlClient()
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda root: SourceCatalogScanner(source="openlist", client=fake_client),
        )

        with pytest.raises(JobDeferredError) as exc:
            handle_discovery_scan(
                {"root_id": root.root_id, "generation": generation},
                progress_callback=lambda *a, **k: None,
                should_cancel=lambda: False,
            )
        # 关键回归：B 405 之后 C 未被请求（冷却准入在下一个目录的第一次
        # attempt 处拦截，而不是继续扫下一个目录）
        assert "/动画/C" not in calls
        assert calls == ["/动画", "/动画/B"]

        # 延后时间 = source_health 冷却结束时刻
        assert exc.value.until_unix == pytest.approx(
            source_health.get_health(key).cooldown_until
        )
        assert "访问保护" in exc.value.message

        # Catalog 事实：A(root) 已 complete，B 标记 failed，C 保持 queued，旧事实未删除
        directories = {
            item["remote_path"]: item
            for item in catalog_store.list_all_directories(root.root_id)
        }
        assert directories["/动画"]["state"] == "complete"
        assert directories["/动画/B"]["state"] == "failed"
        assert directories["/动画/B"]["last_error_kind"] == "risk_control"
        assert directories["/动画/C"]["state"] == "queued"

    def test_rate_limit_mid_scan_propagates(self, monkeypatch):
        """扫描中途 429：同样整棵中止并转 JobDeferredError。"""
        from app.catalog.scanner import SourceCatalogScanner
        from app.integrations.openlist.governor import OpenListRequestGovernor
        from app.integrations.openlist.models import (
            OpenListEntry,
            OpenListRateLimitedError,
        )
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        key = governor_connection_key(
            _FakeConfig.openlist_server_url, _FakeConfig.openlist_username
        )

        class _RateLimitedClient:
            _governor = OpenListRequestGovernor(rate_per_second=1000)
            _conn_key = key

            def login(self):
                return "t"

            def list_dir(self, path, page=1, per_page=100, refresh=False):
                if path == "/动画":
                    return type(
                        "Page",
                        (),
                        {
                            "entries": [
                                OpenListEntry(name="B", is_dir=True, remote_path="/动画/B")
                            ],
                            "total": 1,
                        },
                    )()
                source_health.record_failure(key, "rate_limit")
                raise OpenListRateLimitedError()

        fake_client = _RateLimitedClient()
        monkeypatch.setattr(
            "app.pipeline.discovery_handler._build_scanner",
            lambda root: SourceCatalogScanner(source="openlist", client=fake_client),
        )
        with pytest.raises(JobDeferredError) as exc:
            handle_discovery_scan(
                {"root_id": root.root_id, "generation": generation},
                progress_callback=lambda *a, **k: None,
                should_cancel=lambda: False,
            )
        assert exc.value.until_unix == pytest.approx(
            source_health.get_health(key).cooldown_until
        )


class TestDurableJobDefer:
    """冷却延后：真实 jobs store + runner，job 回到 queued 且不消耗 attempt。"""

    def test_deferred_discovery_job_queued_with_not_before(self, monkeypatch):
        from datetime import datetime

        from app.jobs import store as job_store
        from app.jobs.registry import register, unregister
        from app.jobs.runner import JobRunner
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        key = governor_connection_key(
            _FakeConfig.openlist_server_url, _FakeConfig.openlist_username
        )
        source_health.enter_cooldown(
            key, reason_kind="risk_control", cooldown_seconds=3600,
        )
        record = source_health.get_health(key)

        register("discovery_scan", handle_discovery_scan)
        try:
            job = job_store.create_job(
                job_type="discovery_scan",
                resource_key=f"scan:conn:{root.source_id}",
                payload={"root_id": root.root_id, "generation": generation},
            )
            claimed = job_store.claim_jobs("w1")
            assert len(claimed) == 1 and claimed[0].job_id == job.job_id
            assert claimed[0].status == "running"

            # runner 的 worker_id 必须与领取者一致，租约条件才匹配
            JobRunner(worker_id="w1")._execute(claimed[0])

            after = job_store.get_job(job.job_id)
            assert after.status == "queued"  # 不是 succeeded / failed / cancelled
            assert after.attempt == 0  # 不消耗 attempt
            assert after.not_before != ""
            not_before_ts = datetime.fromisoformat(after.not_before).timestamp()
            assert not_before_ts == pytest.approx(record.cooldown_until, abs=1.0)
            assert "访问保护" in after.message
            # 冷却结束前 claim 会跳过该任务（not_before 未到期）
            assert job_store.claim_jobs("w2") == []
        finally:
            unregister("discovery_scan")

    def test_deferred_job_reclaimable_after_cooldown(self, monkeypatch):
        """冷却结束后任务可再次领取（not_before 到期自动放行，不消耗 attempt）。"""
        from datetime import datetime, timedelta, timezone

        from app.db.database import get_connection
        from app.jobs import store as job_store
        from app.jobs.registry import register, unregister
        from app.jobs.runner import JobRunner
        from app.pipeline.discovery_handler import handle_discovery_scan

        root, generation = _make_root()
        _patch_config(monkeypatch)
        key = governor_connection_key(
            _FakeConfig.openlist_server_url, _FakeConfig.openlist_username
        )
        source_health.enter_cooldown(
            key, reason_kind="risk_control", cooldown_seconds=3600,
        )

        register("discovery_scan", handle_discovery_scan)
        try:
            job = job_store.create_job(
                job_type="discovery_scan",
                resource_key=f"scan:conn:{root.source_id}",
                payload={"root_id": root.root_id, "generation": generation},
            )
            claimed = job_store.claim_jobs("w1")
            JobRunner(worker_id="w1")._execute(claimed[0])
            after = job_store.get_job(job.job_id)
            assert after.status == "queued"
            assert after.attempt == 0
            # 模拟冷却窗口结束：把 not_before 拨到过去，claim 条件自动放行
            past = (
                datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=10)
            ).isoformat()
            conn = get_connection()
            conn.execute(
                "UPDATE jobs SET not_before = ? WHERE job_id = ?",
                (past, job.job_id),
            )
            conn.commit()
            claimed2 = job_store.claim_jobs("w2")
            assert len(claimed2) == 1 and claimed2[0].job_id == job.job_id
            assert claimed2[0].attempt == 0  # defer 不消耗 attempt
        finally:
            unregister("discovery_scan")
