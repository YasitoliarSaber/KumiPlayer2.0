"""本地目录的新后台导入入口只创建 V3 durable 批次。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.database import close_connection, init_db
from app.main import app


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    import app.db.database as db_mod

    monkeypatch.setattr(db_mod, "_db_path", tmp_path / "local-background.db")
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def test_local_background_import_creates_source_catalog_batch(tmp_path):
    from app.jobs import store as job_store

    media_root = tmp_path / "媒体库"
    media_root.mkdir()
    client = TestClient(app)

    response = client.post(
        "/api/sources/local/import-batch",
        json={"root_path": str(media_root), "import_family": "anime"},
    )

    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["mode"] == "auto_safe"
    assert len(batch["roots"]) == 1
    root = batch["roots"][0]
    assert root["remote_locator"] == "/" + media_root.as_posix()
    assert root["local_locator"] == str(media_root.resolve())
    assert root["job_id"]
    job = job_store.get_job(root["job_id"])
    assert job is not None
    assert job.job_type == "discovery_scan"
    assert job.payload["root_id"] == root["root_id"]


def test_local_scanner_uses_posix_catalog_paths(tmp_path):
    from app.sources.local import LocalScanner

    media_root = tmp_path / "library"
    media_root.mkdir()
    (media_root / "Episode 01.mkv").write_bytes(b"")
    root_path = "/" + media_root.as_posix()

    page = LocalScanner().enumerate_directory(root_path)

    assert page.entries[0].parent_path == root_path
    assert page.entries[0].remote_path == f"{root_path}/Episode 01.mkv"


def test_batch_projection_exposes_downstream_unit_state(tmp_path):
    from app.catalog import store as catalog_store
    from app.jobs import store as job_store
    from app.pipeline.batch_status import refresh_batch_status

    catalog_store.create_source(source_id="local-status", source_type="local")
    batch = catalog_store.create_import_batch(
        source_id="local-status",
        roots=[{"remote_locator": "/C:/media", "local_locator": "C:\\media"}],
    )
    root = batch["roots"][0]
    job = job_store.create_job(
        job_type="discovery_scan",
        resource_key="scan:conn:local-status",
        payload={
            "root_id": root["root_id"],
            "generation": 0,
            "result": {"units": [{
                "unit_id": "unit-review", "boundary": "/C:/media/未知作品",
                "status": "needs_review", "video_count": 1,
            }]},
        },
    )
    catalog_store.update_import_batch_root(
        batch["batch_id"], root["root_id"], status="queued", generation=0,
    )
    from app.db.database import get_connection
    get_connection().execute(
        "UPDATE jobs SET status = 'succeeded', progress = 100 WHERE job_id = ?",
        (job.job_id,),
    )
    get_connection().commit()

    projected = refresh_batch_status(catalog_store.get_import_batch(batch["batch_id"]) or batch)

    unit = projected["roots"][0]["units"][0]
    assert unit["state"] == "needs_review"
    assert unit["work_title"] == "/C:/media/未知作品"
