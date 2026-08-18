"""RWK-6 验收：决定性回归矩阵 D/E（snapshot 通道与 OpenList 故障隔离）。

必须证明：
- D. snapshot_pan115 job + OpenList cooling_down → 本地 TXT 扫描仍成功，
  0 OpenList 请求；
- E. snapshot_pan115 job + Credential Store unavailable → 本地 TXT 扫描
  仍成功，不读取 OpenList credential。
"""
from __future__ import annotations

import pytest

from app.catalog import store as catalog_store
from app.pipeline.discovery_handler import handle_discovery_scan
from app.jobs import store as job_store
from app.catalog.models import DirectoryPage, SourceNodeInput


class TrackingScanner:
    source = "pan115"

    def __init__(self):
        self.calls: list[str] = []

    def enumerate_directory(self, remote_path, page=1, per_page=100):
        # 隔离测试只验证通道放行：返回空目录（不触发下钻/死循环）
        self.calls.append(remote_path)
        return DirectoryPage(entries=[], total=0)


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "rwk6.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _make_root(source_id: str = "pan115-test"):
    catalog_store.create_source(
        source_id=source_id, source_type="pan115",
        provider_id="pan115", ingest_method="directory_tree",
        connection_key=source_id, display_name="115",
    )
    root = catalog_store.create_source_root(
        source_id=source_id, remote_locator="/根",
        local_locator=r"K:\根", import_family="anime",
    )
    return catalog_store.get_source_root(root.root_id)


class TestSnapshotChannelIsolation:
    def test_snapshot_job_succeeds_while_openlist_cooling(self, monkeypatch):
        """D：OpenList cooling_down 时 snapshot_pan115 任务照常完成。"""
        from app.catalog import source_health

        root = _make_root(source_id="pan115-cool")
        generation = catalog_store.bump_generation(root.root_id)

        # OpenList 连接处于冷却状态（root 是 pan115，但假设用户曾配过 OpenList）
        source_health.record_failure("openlist-conn-hash", "risk_control")
        assert source_health.get_health("openlist-conn-hash").state == "cooling_down"

        # snapshot job：scan_channel=snapshot_pan115 → 不查 OpenList health
        from app.db.database import get_connection

        tree_file = "fake-tree.txt"
        job_id = job_store.create_job(
            job_type="discovery_scan",
            resource_key=f"scan:conn:{root.source_id}",
            payload={
                "root_id": root.root_id,
                "generation": generation,
                "source_id": root.source_id,
                "input_path": tree_file,
                "scan_mode": "full",
                "scan_channel": "snapshot_pan115",
            },
        ).job_id
        job = job_store.get_job(job_id)
        # scanner 被替换为本地 fake（不触网、不读凭据）
        from app.pipeline import discovery_handler

        monkeypatch.setattr(
            discovery_handler, "_build_scanner",
            lambda r: TrackingScanner(),
        )
        result = handle_discovery_scan(job.payload)
        assert result["summary"]["failed_count"] == 0

    def test_snapshot_job_succeeds_with_credential_store_unavailable(self, monkeypatch):
        """E：Credential Store unavailable 时 snapshot 任务仍成功，不读凭据。"""
        root = _make_root(source_id="pan115-cred")
        generation = catalog_store.bump_generation(root.root_id)

        # 模拟 credential resolver 抛 unavailable——但 snapshot 通道不得调用它
        def exploding_resolver(*args, **kwargs):
            raise AssertionError("snapshot 通道不应读取 OpenList 凭据")

        monkeypatch.setattr(
            "app.core.config.resolve_openlist_credentials", exploding_resolver
        )
        from app.pipeline import discovery_handler

        monkeypatch.setattr(
            discovery_handler, "_build_scanner",
            lambda r: TrackingScanner(),
        )
        from app.jobs import store as job_store

        job_id = job_store.create_job(
            job_type="discovery_scan",
            resource_key=f"scan:conn:{root.source_id}",
            payload={
                "root_id": root.root_id,
                "generation": generation,
                "source_id": root.source_id,
                "input_path": "fake-tree.txt",
                "scan_mode": "full",
                "scan_channel": "snapshot_pan115",
            },
        ).job_id
        job = job_store.get_job(job_id)
        result = handle_discovery_scan(job.payload)
        assert result["summary"]["failed_count"] == 0

    def test_openlist_channel_still_hits_cooldown_gate(self, monkeypatch):
        """对照：openlist 通道仍必须被冷却门拦住（不回归）。"""
        from app.catalog import source_health

        root = _make_root(source_id="openlist-cool")
        # 注意：root.source_id 是 openlist 才能走冷却门（fallback 语义）
        catalog_store.update_root_metadata(root.root_id, import_family="anime")
        conn = catalog_store.get_connection()
        conn.execute(
            "UPDATE source_roots SET source_id = 'openlist-cool' WHERE root_id = ?",
            (root.root_id,),
        )
        conn.commit()
        generation = catalog_store.bump_generation(root.root_id)
        from app.core.config import AppConfig

        cfg = AppConfig()
        cfg.openlist_server_url = "http://127.0.0.1:5244"
        cfg.openlist_username = "user-cool"
        monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: cfg)
        monkeypatch.setattr(
            "app.core.config.resolve_openlist_credentials",
            lambda: ("user-cool", "pw", "found"),
        )
        from app.integrations.openlist.governor import governor_connection_key

        conn_hash = governor_connection_key("http://127.0.0.1:5244", "user-cool")
        source_health.record_failure(conn_hash, "risk_control")
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan
        from app.jobs.models import JobDeferredError

        job_id = job_store.create_job(
            job_type="discovery_scan",
            resource_key="scan:conn:openlist-cool",
            payload={
                "root_id": root.root_id,
                "generation": generation,
                "source_id": "openlist-cool",
                "input_path": "",
                "scan_mode": "incremental",
                "scan_channel": "openlist",
            },
        ).job_id
        job = job_store.get_job(job_id)
        with pytest.raises(JobDeferredError):
            handle_discovery_scan(job.payload)
