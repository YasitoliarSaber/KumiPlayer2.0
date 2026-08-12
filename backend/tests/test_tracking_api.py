from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app


def test_tracking_binding_api_create_list_and_update(tmp_path):
    root = tmp_path / "作品A"
    root.mkdir()

    with TestClient(app) as client:
        created = client.post("/api/tracking/works", json={
            "work_id": "series-existing",
            "display_title": "作品A",
            "logical_source": "local",
            "root_path": str(root),
            "season_number": 1,
        })
        assert created.status_code == 200, created.text
        assert created.json()["tracking_state"] == "tracking"

        listed = client.get("/api/tracking/works")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["work_id"] == "series-existing"

        updated = client.patch("/api/tracking/works/series-existing", json={
            "tracking_state": "paused",
        })
        assert updated.status_code == 200
        assert updated.json()["tracking_state"] == "paused"


def test_tracking_api_generates_stable_work_id_for_new_show(tmp_path):
    root = tmp_path / "无 TMDB 新番"
    root.mkdir()

    with TestClient(app) as client:
        response = client.post("/api/tracking/works", json={
            "display_title": "无 TMDB 新番",
            "root_path": str(root),
        })

    assert response.status_code == 200
    assert response.json()["work_id"].startswith("series_")
    assert response.json()["attention_state"] == "waiting_metadata"


def test_tracking_api_derives_title_and_source_from_selected_folder(tmp_path, monkeypatch):
    """新增新番只选择真实目录即可，名称与来源由后端自动确定。"""
    from app.db import database

    monkeypatch.setattr(database, "_db_path", tmp_path / "tracking.db")
    root = tmp_path / "01动画" / "新番" / "自动识别作品 (2026)"
    root.mkdir(parents=True)

    with TestClient(app) as client:
        response = client.post("/api/tracking/works", json={
            "root_path": str(root),
            "logical_source": "local",
            "season_number": 1,
        })

    assert response.status_code == 200
    body = response.json()
    assert body["display_title"] == "自动识别作品"
    assert body["series_group"] == "自动识别作品"
    assert body["logical_source"] == "local"


def test_manual_tracking_add_reuses_existing_directory_binding(tmp_path):
    root = tmp_path / "目录树作品A"
    root.mkdir()

    with TestClient(app) as client:
        first = client.post("/api/tracking/works", json={
            "work_id": "series-imported", "display_title": "目录树作品A",
            "logical_source": "local", "root_path": str(root), "season_number": 1,
        })
        second = client.post("/api/tracking/works", json={
            "display_title": "手动添加的作品A", "logical_source": "local",
            "root_path": str(root), "season_number": 1,
        })
        items = client.get("/api/tracking/works").json()["items"]

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["work_id"] == "series-imported"
    assert len([item for item in items if item["root_path"] == str(root)]) == 1


def test_tracking_api_rejects_unknown_source(tmp_path):
    root = tmp_path / "作品A"
    root.mkdir()

    with TestClient(app) as client:
        response = client.post("/api/tracking/works", json={
            "display_title": "作品A", "root_path": str(root), "logical_source": "unknown",
        })

    assert response.status_code == 400


def test_tracking_api_upserts_existing_work_without_duplicate(tmp_path):
    first_root = tmp_path / "旧目录"
    next_root = tmp_path / "新目录"
    first_root.mkdir()
    next_root.mkdir()

    with TestClient(app) as client:
        first = client.post("/api/tracking/works", json={
            "work_id": "series-upsert",
            "display_title": "旧标题",
            "root_path": str(first_root),
        })
        second = client.post("/api/tracking/works", json={
            "work_id": "series-upsert",
            "display_title": "新标题",
            "root_path": str(next_root),
        })
        items = client.get("/api/tracking/works").json()["items"]

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["binding_id"] == first.json()["binding_id"]
    assert second.json()["display_title"] == "新标题"
    assert second.json()["root_path"] == str(next_root)
    assert [item["work_id"] for item in items].count("series-upsert") == 1


def test_single_and_batch_tracking_scans_share_serial_mount_queue(monkeypatch):
    """单部与批量追更必须共用串行队列，避免同时枚举同一挂载盘。"""
    from app.api import tracking as tracking_api
    from app.tracking.models import TrackingBinding

    binding = TrackingBinding(
        binding_id="binding-a", work_id="work-a", display_title="作品A",
        logical_source="baidu", root_path="B:/作品A",
    )
    calls = []

    class CaptureManager:
        def submit_queued(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(task_id=f"task-{len(calls)}", status="pending")

    monkeypatch.setattr(tracking_api, "get_tracking_binding", lambda work_id: binding)
    monkeypatch.setattr("app.tasks.registry.get_task_manager", lambda: CaptureManager())

    tracking_api.scan_work("work-a", tracking_api.ScanRequest(include_scrape=False))
    tracking_api.scan_all(tracking_api.ScanRequest(include_scrape=False, source="baidu", work_ids=["work-a"]))

    assert len(calls) == 2
    assert {call[1]["queue_name"] for call in calls} == {"tracking_mount_scan"}
    assert calls[0][0][1] == "baidu"
    assert calls[1][0][1] == "baidu"


def test_tracking_root_import_submits_one_serial_batch_task(tmp_path, monkeypatch):
    """选择包含多部作品的根目录时，应走批量导入任务而不是创建一条错误的单部绑定。"""
    root = tmp_path / "新番根目录"
    (root / "作品A").mkdir(parents=True)
    (root / "作品B").mkdir()
    calls = []

    class CaptureManager:
        def submit_queued(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(task_id="task-root-import", status="pending")

    monkeypatch.setattr("app.tasks.registry.get_task_manager", lambda: CaptureManager())

    with TestClient(app) as client:
        response = client.post("/api/tracking/import-root", json={
            "root_path": str(root),
            "logical_source": "local",
            "include_scrape": True,
        })

    assert response.status_code == 200, response.text
    assert response.json() == {"task_id": "task-root-import", "status": "pending"}
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "tracking_import_root"
    assert args[1] == "local"
    assert args[3] == str(root)
    assert kwargs["queue_name"] == "tracking_mount_scan"
    assert kwargs["include_scrape"] is True
