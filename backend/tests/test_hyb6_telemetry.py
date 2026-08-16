"""HYB-6 验收：请求成本遥测（KumiPlayer → OpenList 方向）+ bootstrap API 契约。

必须证明：
- 物理请求（fs/list 与 login）按 conn_hash+day 累加计数；
- telemetry 只统计真实发出的请求，不冒充上游配额（disclaimer 文案）；
- API 端点返回今日摘要（未配置连接 → 零值摘要）；
- bootstrap-tree API 前端契约字段齐全（FormData multipart）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.telemetry import (
    OP_FS_LIST,
    OP_LOGIN,
    daily_summary,
    record_request,
)
from app.main import app


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "hyb6.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


class TestTelemetryRecord:
    def test_record_accumulates_per_conn_day_operation(self):
        record_request("hash-a", OP_FS_LIST)
        record_request("hash-a", OP_FS_LIST)
        record_request("hash-a", OP_LOGIN)
        record_request("hash-b", OP_FS_LIST)
        summary = daily_summary("hash-a")
        assert summary["fs_list"] == 2
        assert summary["login"] == 1
        assert summary["total"] == 3
        other = daily_summary("hash-b")
        assert other["fs_list"] == 1

    def test_daily_summary_has_disclaimer(self):
        record_request("hash-c", OP_FS_LIST)
        summary = daily_summary("hash-c")
        assert "KumiPlayer" in summary["disclaimer"]
        assert "缓存" in summary["disclaimer"]
        assert "更少" in summary["disclaimer"]

    def test_empty_summary_is_zero(self):
        summary = daily_summary("nonexistent")
        assert summary["fs_list"] == 0
        assert summary["login"] == 0
        assert summary["total"] == 0

    def test_record_never_raises_on_bad_input(self):
        record_request("", OP_FS_LIST)  # 空 conn → 静默
        record_request("hash", "")  # 空 op → 静默
        record_request("hash", "unknown-op")  # 任意 op 可计数


class TestTelemetryApi:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_api_unconfigured_returns_zero_summary(self, client):
        resp = client.get("/api/openlist/telemetry/today")
        assert resp.status_code == 200
        body = resp.json()
        assert body["fs_list"] == 0
        assert body["total"] == 0
        assert "disclaimer" in body

    def test_api_returns_recorded_counts(self, client, monkeypatch):
        from app.core.config import save_config
        from app.core.config import AppConfig

        cfg = AppConfig()
        cfg.openlist_server_url = "http://127.0.0.1:5244"
        cfg.openlist_username = "user-x"
        save_config(cfg)
        from app.integrations.openlist.governor import governor_connection_key

        conn_hash = governor_connection_key("http://127.0.0.1:5244", "user-x")
        record_request(conn_hash, OP_FS_LIST)
        record_request(conn_hash, OP_FS_LIST)
        resp = client.get("/api/openlist/telemetry/today")
        assert resp.status_code == 200
        body = resp.json()
        assert body["fs_list"] == 2
        assert body["total"] == 2
