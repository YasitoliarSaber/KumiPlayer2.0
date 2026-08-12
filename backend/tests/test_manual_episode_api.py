from fastapi.testclient import TestClient
from pathlib import Path
from types import SimpleNamespace

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.import_plan.store import load_import_plan, save_import_plan
from app.main import app
from app.tracking.models import TrackingBinding
from app.tracking.store import get_tracking_binding, upsert_tracking_binding


def _prepare_work(root):
    base = ImportPlan(
        plan_id="base-manual",
        source="local",
        source_snapshot_id="base-snapshot",
        status="executed",
        items=[ImportPlanItem(
            id="existing-e1", plan_id="base-manual", raw_file_id="raw-e1",
            source="local", relative_path="作品A/作品A.S01E01.mkv",
            real_path=str(root / "作品A.S01E01.mkv"), resource_type="video",
            action="generate_strm", work_id="series-manual", canonical_work_id="series-manual",
            work_title="作品A", series_group="作品A", card_type="main_series",
            media_type="tv", show_type="anime_series", group_type="season",
            season_number=1, episode_number=1,
        )],
    )
    save_import_plan(base)
    upsert_tracking_binding(TrackingBinding(
        work_id="series-manual", display_title="作品A", root_path=str(root),
        series_group="作品A", season_number=1, baseline_plan_id=base.plan_id,
    ))


def test_manual_episode_preview_reports_added_and_conflict(tmp_path):
    root = tmp_path / "作品A"
    root.mkdir()
    (root / "作品A.S01E01.mkv").write_bytes(b"old")
    added = root / "作品A.S01E02.mkv"
    added.write_bytes(b"new")
    conflict = root / "另一个版本.S01E01.mkv"
    conflict.write_bytes(b"conflict")

    with TestClient(app) as client:
        _prepare_work(root)
        response = client.post("/api/library/works/series-manual/episodes/preview", json={
            "paths": [str(added), str(conflict)],
            "season_number": 1,
        })

    assert response.status_code == 200, response.text
    statuses = {item["episode_number"]: item["status"] for item in response.json()["items"]}
    assert statuses[2] == "added"
    assert statuses[1] == "conflict"
    assert response.json()["can_commit"] is False


def test_manual_episode_preview_keeps_unrecognized_file_for_review(tmp_path):
    root = tmp_path / "作品A"
    root.mkdir()
    unknown = root / "最新话.mkv"
    unknown.write_bytes(b"unknown")

    with TestClient(app) as client:
        _prepare_work(root)
        response = client.post("/api/library/works/series-manual/episodes/preview", json={
            "paths": [str(unknown)], "season_number": 1,
        })

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "unrecognized"


def test_manual_episode_preview_expands_selected_directory(tmp_path):
    root = tmp_path / "作品A"
    root.mkdir()
    (root / "作品A.S01E01.mkv").write_bytes(b"old")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "作品A.S01E02.mkv").write_bytes(b"two")
    (incoming / "作品A.S01E03.mp4").write_bytes(b"three")
    (incoming / "readme.txt").write_text("ignore", encoding="utf-8")

    with TestClient(app) as client:
        _prepare_work(root)
        response = client.post("/api/library/works/series-manual/episodes/preview", json={
            "paths": [str(incoming)], "season_number": 1,
        })

    assert response.status_code == 200, response.text
    assert {item["episode_number"] for item in response.json()["items"]} == {2, 3}
    assert {item["status"] for item in response.json()["items"]} == {"added"}


def test_manual_episode_preview_reports_same_path_content_change_as_replaced(tmp_path):
    root = tmp_path / "作品A"
    root.mkdir()
    episode = root / "作品A.S01E01.mkv"
    episode.write_bytes(b"old-version")
    _prepare_work(root)
    base = __import__("app.import_plan.store", fromlist=["load_import_plan"]).load_import_plan(plan_id="base-manual")
    base.items[0].source_size = 3
    save_import_plan(base)

    with TestClient(app) as client:
        response = client.post("/api/library/works/series-manual/episodes/preview", json={
            "paths": [str(episode)], "season_number": 1,
        })

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["status"] == "replaced"
    assert response.json()["can_commit"] is True


def test_dropped_episode_keeps_its_detected_source(tmp_path, monkeypatch):
    """本地视频拖入网盘作品时，剧集来源应保留为本地，但仍归入当前作品。"""
    from app.library import manual_import

    episode = tmp_path / "作品A.S01E02.mkv"
    episode.write_bytes(b"episode")
    monkeypatch.setattr(manual_import, "configured_mount_source", lambda _path: "local", raising=False)

    snapshot = manual_import._snapshot_for_selected_files([episode], "pan115", "作品A")

    assert snapshot.source == "pan115"
    assert snapshot.files[0].source == "local"


def test_manual_episode_number_is_parsed_from_filename_only():
    """手动追加不应借助父目录或作品标题推断集数。"""
    from app.library.manual_import import _extract_manual_episode_number

    assert _extract_manual_episode_number("第8集.mkv") == 8
    assert _extract_manual_episode_number("作品名.S01E09.标题.mp4") == 9
    assert _extract_manual_episode_number("[字幕组] 作品 [EP10].mkv") == 10
    assert _extract_manual_episode_number("[11].mkv") == 11
    assert _extract_manual_episode_number("12.mkv") == 12
    assert _extract_manual_episode_number("最新话.mkv") is None


def test_manual_episode_preview_ignores_parent_folder_and_keeps_real_path(tmp_path):
    """父目录即使含电影或季度词，也不能改变当前作品和目标季度。"""
    root = tmp_path / "作品A"
    root.mkdir()
    (root / "作品A.S01E01.mkv").write_bytes(b"old")
    incoming = tmp_path / "错误作品名 Movie Season 9"
    incoming.mkdir()
    episode = incoming / "第8集.mkv"
    episode.write_bytes(b"episode-8")

    with TestClient(app) as client:
        _prepare_work(root)
        response = client.post("/api/library/works/series-manual/episodes/preview", json={
            "paths": [str(episode)], "season_number": 2,
        })

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["season_number"] == 2
    assert item["episode_number"] == 8
    assert item["path"] == str(episode.resolve())
    assert item["status"] == "added"


def test_commit_endpoint_does_not_overwrite_preview_before_task_runs(tmp_path, monkeypatch):
    """提交接口只能只读校验预览，正式计划由后台任务转换一次。"""
    root = tmp_path / "作品A"
    root.mkdir()
    (root / "作品A.S01E01.mkv").write_bytes(b"old")
    episode = root / "第2集.mkv"
    episode.write_bytes(b"episode-2")

    class DeferredTaskManager:
        def submit(self, *_args, **_kwargs):
            return SimpleNamespace(task_id="manual-task", status="pending")

    import app.tasks.registry as task_registry
    monkeypatch.setattr(task_registry, "get_task_manager", lambda: DeferredTaskManager())

    with TestClient(app) as client:
        _prepare_work(root)
        preview_response = client.post("/api/library/works/series-manual/episodes/preview", json={
            "paths": [str(episode)], "season_number": 1,
        })
        plan_id = preview_response.json()["plan_id"]
        commit_response = client.post("/api/library/works/series-manual/episodes/commit", json={
            "plan_id": plan_id, "auto_scrape": True,
        })

    assert commit_response.status_code == 200, commit_response.text
    persisted_preview = load_import_plan(plan_id=plan_id)
    assert persisted_preview is not None
    assert persisted_preview.summary.get("plan_type") == "manual_episode_preview"


def test_manual_episode_real_path_is_written_to_strm(tmp_path):
    """拖入文件的真实绝对路径必须成为新增剧集的播放目标。"""
    from app.library.manual_import import build_manual_episode_commit, preview_manual_episodes
    from app.mirror.generator import generate_mirror

    root = tmp_path / "作品A"
    root.mkdir()
    (root / "作品A.S01E01.mkv").write_bytes(b"old")
    episode = tmp_path / "下载目录" / "第8集.mkv"
    episode.parent.mkdir()
    episode.write_bytes(b"episode-8")
    _prepare_work(root)

    preview = preview_manual_episodes("series-manual", [str(episode)], 1)
    plan = build_manual_episode_commit(preview["plan_id"])
    plan.status = "confirmed"
    result = generate_mirror(plan, mirror_root=str(tmp_path / "mirror"), update_latest=False)

    new_item = next(item for item in plan.items if item.episode_number == 8)
    assert result.status == "success"
    assert new_item.real_path == str(episode.resolve())
    assert Path(new_item.target_strm_path).read_text(encoding="utf-8").strip() == str(episode.resolve())


def test_manual_episode_commit_survives_metadata_failure(tmp_path, monkeypatch):
    """镜像已写入后，TMDB 补资料失败不能回滚追加或丢失新基线。"""
    from app.db.database import init_db
    from app.import_pipeline import service as pipeline_service
    from app.library import service as library_service
    from app.library.manual_import import commit_manual_episode_plan, preview_manual_episodes

    root = tmp_path / "作品A"
    root.mkdir()
    (root / "作品A.S01E01.mkv").write_bytes(b"old")
    episode = root / "第2集.mkv"
    episode.write_bytes(b"episode-2")
    init_db()
    _prepare_work(root)
    preview = preview_manual_episodes("series-manual", [str(episode)], 1)

    monkeypatch.setattr(pipeline_service, "run_auto_import_pipeline", lambda *_args, **_kwargs: {
        "status": "blocked",
        "stage": "scrape",
        "error": "TMDB 暂时不可用",
        "mirror": {"status": "success", "generated_count": 1},
    })
    monkeypatch.setattr(
        library_service,
        "refresh_tracking_library_work",
        lambda _plan, _work_id: {"mode": "tracking_work", "work_count": 1, "warnings": []},
    )

    result = commit_manual_episode_plan("series-manual", preview["plan_id"], include_scrape=True)
    binding = get_tracking_binding("series-manual")
    baseline = load_import_plan(plan_id=binding.baseline_plan_id)

    assert result["status"] == "succeeded"
    assert result["metadata_status"] == "degraded"
    assert "TMDB" in result["metadata_warning"]
    assert baseline is not None
    assert {item.episode_number for item in baseline.items if item.resource_type == "video"} == {1, 2}
