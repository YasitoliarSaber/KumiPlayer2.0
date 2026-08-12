# -*- coding: utf-8 -*-
"""M07 Mirror API 测试"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_plan_counter = 0


def _cleanup():
    import shutil
    import time
    from app.tasks.registry import reset_task_manager
    reset_task_manager()
    # 等待异步任务释放文件句柄
    time.sleep(1)
    data_dir = Path(__file__).parent.parent / "data"
    if data_dir.exists():
        try:
            shutil.rmtree(data_dir)
        except OSError:
            # Windows 文件锁，再试一次
            time.sleep(2)
            shutil.rmtree(data_dir, ignore_errors=True)


def _setup_plan(status="confirmed"):
    global _plan_counter
    _plan_counter += 1
    plan_id = f"plan-test-{_plan_counter}"
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    source_file = Path(__file__).parent.parent / "data" / "test_source" / f"video-{_plan_counter}.mkv"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(b"video")
    items = [
        ImportPlanItem(
            id=f"v{_plan_counter}", plan_id=plan_id, raw_file_id="r1", source="pan115",
            relative_path="动画/test/视频.mkv", real_path=str(source_file),
            resource_type="video", action="generate_strm",
            work_title="测试作品", group_type="season", season_number=1,
            episode_number=1, confidence="high",
        ),
    ]
    plan = ImportPlan(
        plan_id=plan_id, source="pan115", source_snapshot_id="snap-1",
        status=status, items=items,
    )
    save_import_plan(plan)
    return plan


def test_generate_returns_task_id():
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        plan = _setup_plan("confirmed")
        client = TestClient(app)
        response = client.post("/api/mirror/pan115/generate", json={"plan_id": plan.plan_id})
        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["status"] in ("pending", "running")
    finally:
        _cleanup()


def test_get_task_status():
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        plan = _setup_plan("confirmed")
        client = TestClient(app)
        resp = client.post("/api/mirror/pan115/generate", json={"plan_id": plan.plan_id})
        assert resp.status_code == 200, f"generate: {resp.status_code} {resp.text}"
        task_id = resp.json()["task_id"]
        time.sleep(5)
        resp = client.get(f"/api/mirror/tasks/{task_id}")
        assert resp.status_code == 200, f"query: {resp.status_code} {resp.text}"
        data = resp.json()
        assert data.get("task_id") == task_id, f"task_id: {data}"
        assert data.get("status") in ("succeeded", "failed"), f"status: {data}"
    finally:
        _cleanup()


def test_reject_draft_plan():
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        plan = _setup_plan("draft")
        client = TestClient(app)
        resp = client.post("/api/mirror/pan115/generate", json={"plan_id": plan.plan_id})
        assert resp.status_code == 400
        assert "confirmed" in resp.json()["detail"]
    finally:
        _cleanup()


def test_reject_executed_plan():
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        plan = _setup_plan("executed")
        client = TestClient(app)
        resp = client.post("/api/mirror/pan115/generate", json={"plan_id": plan.plan_id})
        assert resp.status_code == 400
    finally:
        _cleanup()


def test_reject_nonexistent_plan():
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        client = TestClient(app)
        resp = client.post("/api/mirror/pan115/generate", json={"plan_id": "nonexistent"})
        assert resp.status_code == 404
    finally:
        _cleanup()


def test_confirm_not_return_task_id():
    from fastapi.testclient import TestClient
    from app.main import app
    _cleanup()
    try:
        plan = _setup_plan("draft")
        client = TestClient(app)
        # patch needs_review
        items_resp = client.get(f"/api/imports/pan115/preview?plan_id={plan.plan_id}")
        items = items_resp.json()["items"]
        for item in items:
            if item.get("needs_review"):
                client.patch(
                    f"/api/imports/pan115/items/{item['id']}",
                    json={"plan_id": plan.plan_id, "patch": {"needs_review": False}},
                )
        resp = client.post("/api/imports/pan115/confirm", json={"plan_id": plan.plan_id})
        data = resp.json()
        assert "task_id" not in data
        assert data.get("status") == "confirmed"
    finally:
        _cleanup()


def test_task_not_found():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.get("/api/mirror/tasks/nonexistent")
    assert resp.status_code == 404


def test_worker_reports_generator_failure_as_failed_task(monkeypatch):
    """镜像生成器失败必须向任务层抛出，不能被前端误判成成功。"""
    from app.api.mirror import _run_mirror_generate
    from app.mirror.result import MirrorGenerateResult

    plan = type("Plan", (), {"plan_id": "bad-plan", "source": "baidu"})()
    monkeypatch.setattr(
        "app.api.mirror.generate_mirror",
        lambda _plan: MirrorGenerateResult(
            plan_id="bad-plan",
            source="baidu",
            status="failed",
            errors=["媒体路径验证失败：视频文件不可达"],
        ),
    )

    with pytest.raises(RuntimeError, match="媒体路径验证失败"):
        _run_mirror_generate(plan)


def test_worker_publishes_mirror_counts_before_reporting_failure(monkeypatch):
    """部分失败也必须把成功、失败、跳过和总数留在任务结果中。"""
    from app.api.mirror import _run_mirror_generate
    from app.mirror.result import MirrorGenerateResult, MirrorItemResult

    plan = type("Plan", (), {"plan_id": "partial-plan", "source": "baidu"})()
    monkeypatch.setattr(
        "app.api.mirror.generate_mirror",
        lambda _plan: MirrorGenerateResult(
            plan_id="partial-plan",
            source="baidu",
            status="partial_failed",
            generated_count=2,
            skipped_count=1,
            failed_count=1,
            items=[
                MirrorItemResult(status="generated"),
                MirrorItemResult(status="generated"),
                MirrorItemResult(status="skipped"),
                MirrorItemResult(status="failed", item_id="bad", message="写入失败"),
            ],
            errors=["写入失败"],
        ),
    )
    patches = []

    with pytest.raises(RuntimeError, match="写入失败"):
        _run_mirror_generate(
            plan,
            progress_callback=lambda progress, message, payload: patches.append(payload),
        )

    assert patches[-1]["generated_count"] == 2
    assert patches[-1]["failed_count"] == 1
    assert patches[-1]["skipped_count"] == 1
    assert patches[-1]["items_count"] == 4
    assert patches[-1]["failed_items"] == [{"item_id": "bad", "message": "写入失败"}]


def test_successful_mirror_publishes_plan_to_library_even_when_every_file_is_skipped(monkeypatch):
    """镜像已存在仍是成功完成，必须立即把当前计划发布到媒体库索引。"""
    from app.api.mirror import _run_mirror_generate
    from app.mirror.result import MirrorGenerateResult, MirrorItemResult

    plan = type(
        "Plan",
        (),
        {
            "plan_id": "seasonal-existing-plan",
            "source": "baidu",
            "import_scope": "seasonal",
        },
    )()
    monkeypatch.setattr(
        "app.api.mirror.generate_mirror",
        lambda _plan: MirrorGenerateResult(
            plan_id=plan.plan_id,
            source=plan.source,
            status="success",
            generated_count=0,
            skipped_count=2,
            failed_count=0,
            items=[
                MirrorItemResult(status="skipped"),
                MirrorItemResult(status="skipped"),
            ],
        ),
    )
    monkeypatch.setattr(
        "app.tracking.registration.register_seasonal_plan",
        lambda _plan: {"registered": 1, "skipped_loose": 0},
    )
    monkeypatch.setattr(
        "app.media_presets.service.mark_preset_lifecycle",
        lambda *_args: None,
    )
    refresh_calls = []
    monkeypatch.setattr(
        "app.library.service.publish_import_plan_to_library",
        lambda current_plan: refresh_calls.append(current_plan.plan_id)
        or {"mode": "plan_publish", "work_count": 1},
    )

    payload = _run_mirror_generate(plan)

    assert refresh_calls == [plan.plan_id]
    assert payload["generated_count"] == 0
    assert payload["skipped_count"] == 2
    assert payload["failed_count"] == 0
    assert payload["library_refresh"] == {"mode": "plan_publish", "work_count": 1}


def test_mirror_task_success_preserves_logs_and_current_target(monkeypatch):
    """成功任务最终 payload 保留累计日志，生成器上报的 current_target 进入任务结果。"""
    from app.api.mirror import _run_mirror_generate
    from app.mirror.result import MirrorGenerateResult, MirrorItemResult

    plan = type("Plan", (), {
        "plan_id": "logs-plan",
        "source": "baidu",
        "import_scope": "",
    })()

    def fake_generate(_plan, progress_callback=None):
        if progress_callback:
            progress_callback(30, "正在生成镜像 1/1", {
                "generated_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "items_count": 1,
                "processed_count": 1,
                "log_kind": "info",
                "current_target": "测试作品 · 第 1 季 · 第 1 集",
            })
        return MirrorGenerateResult(
            plan_id=plan.plan_id,
            source=plan.source,
            status="success",
            generated_count=1,
            skipped_count=0,
            failed_count=0,
            items=[MirrorItemResult(status="generated")],
        )

    monkeypatch.setattr("app.api.mirror.generate_mirror", fake_generate)
    monkeypatch.setattr(
        "app.tracking.registration.reconcile_tracking_bindings_for_plan",
        lambda _plan: {"removed": 0},
    )
    monkeypatch.setattr(
        "app.media_presets.service.mark_preset_lifecycle",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "app.library.service.publish_import_plan_to_library",
        lambda _plan: {"mode": "plan_publish", "work_count": 1},
    )

    patches = []
    payload = _run_mirror_generate(plan, progress_callback=lambda p, m, patch: patches.append(patch))

    assert payload["generated_count"] == 1
    assert "logs" in payload
    assert len(payload["logs"]) >= 4
    for entry in payload["logs"]:
        assert {"time", "kind", "message"} <= set(entry), f"日志 schema 不完整: {entry}"
    # 生成器上报的 current_target 保留到任务结果的增量 patch 中
    target_patch = next((patch for patch in patches if patch.get("current_target")), None)
    assert target_patch is not None
    assert target_patch["current_target"] == "测试作品 · 第 1 季 · 第 1 集"
    # 成功路径以索引刷新完成收尾
    assert payload["logs"][-1]["kind"] == "done"
    assert payload["logs"][-1]["message"] == "媒体库索引已刷新"
    # 每次增量 patch 都携带累计日志快照
    assert patches[-1]["logs"][-1]["message"] == "媒体库索引已刷新"


def test_mirror_task_failure_keeps_error_log_and_counts(monkeypatch):
    """失败任务抛错前保存完整计数，最终日志以 error 收尾。"""
    from app.api.mirror import _run_mirror_generate
    from app.mirror.result import MirrorGenerateResult, MirrorItemResult

    plan = type("Plan", (), {
        "plan_id": "fail-plan",
        "source": "baidu",
        "import_scope": "",
    })()
    monkeypatch.setattr(
        "app.api.mirror.generate_mirror",
        lambda _plan: MirrorGenerateResult(
            plan_id="fail-plan",
            source="baidu",
            status="failed",
            generated_count=1,
            skipped_count=1,
            failed_count=1,
            items=[
                MirrorItemResult(status="generated"),
                MirrorItemResult(status="skipped"),
                MirrorItemResult(status="failed", item_id="bad", message="写入失败"),
            ],
            errors=["写入失败"],
        ),
    )
    patches = []

    with pytest.raises(RuntimeError, match="写入失败"):
        _run_mirror_generate(plan, progress_callback=lambda p, m, patch: patches.append(patch))

    assert patches[-1]["generated_count"] == 1
    assert patches[-1]["failed_count"] == 1
    assert patches[-1]["skipped_count"] == 1
    assert patches[-1]["items_count"] == 3
    logs = patches[-1]["logs"]
    assert logs[-1]["kind"] == "error"
    assert logs[-1]["message"] == "镜像生成未通过完整性检查"
    assert {"time", "kind", "message"} <= set(logs[0])


def test_target_fields_saved_after_task():
    """任务完成后重新加载 plan，确认 target_dir/target_filename/target_strm_path 已写入"""
    import time
    from fastapi.testclient import TestClient
    from app.main import app
    from app.import_plan.store import load_import_plan

    _cleanup()
    try:
        plan = _setup_plan("confirmed")
        client = TestClient(app)

        resp = client.post("/api/mirror/pan115/generate", json={"plan_id": plan.plan_id})
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]

        # 等待完成
        time.sleep(5)

        # 确认任务成功
        resp = client.get(f"/api/mirror/tasks/{task_id}")
        assert resp.json()["status"] == "succeeded"

        # 重新加载 plan，检查 target 字段
        saved_plan = load_import_plan(plan_id=plan.plan_id)
        assert saved_plan is not None
        video_items = [i for i in saved_plan.items if i.resource_type == "video" and i.action == "generate_strm"]
        assert len(video_items) > 0
        for item in video_items:
            assert item.target_dir != "", f"target_dir 为空: {item.id}"
            assert item.target_filename != "", f"target_filename 为空: {item.id}"
            assert item.target_strm_path != "", f"target_strm_path 为空: {item.id}"
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_generate_returns_task_id,
        test_get_task_status,
        test_reject_draft_plan,
        test_reject_executed_plan,
        test_reject_nonexistent_plan,
        test_confirm_not_return_task_id,
        test_task_not_found,
        test_target_fields_saved_after_task,
    ]
    passed = failed = 0
    for t in tests:
        try:
            _cleanup()
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        finally:
            _cleanup()
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
