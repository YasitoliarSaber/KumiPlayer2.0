# -*- coding: utf-8 -*-
"""错误日志删除与清空测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_error_log_delete_and_purge_core(tmp_path, monkeypatch):
    from app.core import error_log

    monkeypatch.setattr(error_log, "_get_error_log_dir", lambda: tmp_path)

    pan_id = error_log.log_error(
        stage="scrape",
        category="scrape_failed",
        message="CLANNAD / execute_scrape: failed",
        source="pan115",
        context={"target": {"query": "CLANNAD"}},
    )
    error_log.log_error(
        stage="mirror",
        category="mirror_failed",
        message="百度镜像失败",
        source="baidu",
        context={"file_name": "01.mkv"},
    )

    assert error_log.delete_error(pan_id) is True
    remaining = error_log.load_recent_errors()
    assert len(remaining) == 1
    assert remaining[0]["source"] == "baidu"

    assert error_log.purge_errors(source="baidu") == 1
    assert error_log.load_recent_errors() == []


def test_error_log_delete_and_purge_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core import error_log
    from app.main import app

    monkeypatch.setattr(error_log, "_get_error_log_dir", lambda: tmp_path)
    pan_id = error_log.log_error(
        stage="scrape",
        category="scrape_failed",
        message="115 刮削失败",
        source="pan115",
        context={"file_name": "CLANNAD.S01E01.mkv"},
    )
    error_log.log_error(
        stage="scrape",
        category="needs_review",
        message="百度需要人工确认",
        source="baidu",
        context={"file_name": "Movie.mkv"},
    )

    client = TestClient(app)
    deleted = client.delete(f"/api/error-log/{pan_id}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    purged = client.post("/api/error-log/purge", json={"source": "baidu"})
    assert purged.status_code == 200
    assert purged.json()["deleted_count"] == 1
    assert error_log.load_recent_errors() == []


def test_error_log_export_returns_full_text(tmp_path, monkeypatch):
    """导出端点返回完整错误日志文本（含 context），不依赖前端展示。"""
    from app.core import error_log

    monkeypatch.setattr(error_log, "_get_error_log_dir", lambda: tmp_path)
    from fastapi.testclient import TestClient
    from app.main import app

    error_log.log_error(
        "scrape", "scrape_failed", "刮削产物不完整：海报缺失",
        source="baidu", context={"work_id": "w1", "target_id": "t1"},
    )
    with TestClient(app) as client:
        response = client.get("/api/error-log/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "刮削产物不完整" in body
    assert "baidu" in body
    assert "context" in body
    assert "t1" in body
