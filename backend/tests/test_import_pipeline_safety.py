from app.import_pipeline.service import run_auto_import_pipeline
from app.import_plan.models import ImportPlan, ImportPlanItem


def test_automatic_pipeline_never_forces_needs_review(monkeypatch):
    plan = ImportPlan(
        plan_id="unsafe-plan",
        source="local",
        status="draft",
        items=[ImportPlanItem(
            id="episode-unknown",
            plan_id="unsafe-plan",
            source="local",
            resource_type="video",
            action="generate_strm",
            work_id="work-a",
            work_title="作品A",
            media_type="tv",
            show_type="anime_series",
            card_type="main_series",
            series_group="作品A",
            group_type="season",
            season_number=1,
            episode_number=None,
            needs_review=True,
        )],
    )
    monkeypatch.setattr("app.import_pipeline.service.load_import_plan", lambda plan_id: plan)
    monkeypatch.setattr(
        "app.import_pipeline.service.generate_mirror",
        lambda _plan: (_ for _ in ()).throw(AssertionError("不应生成镜像")),
    )

    result = run_auto_import_pipeline("local", "unsafe-plan", include_scrape=False)

    assert result["status"] == "blocked"
    assert result["stage"] == "confirm"
    # pipeline 日志统一为 time/kind/message，blocked 路径末条为 error
    logs = result["logs"]
    assert logs
    for entry in logs:
        assert {"time", "kind", "message"} <= set(entry), f"pipeline 日志 schema 不完整: {entry}"
    assert logs[-1]["kind"] == "error"
    assert logs[-1]["message"].startswith("自动确认失败")


def test_completed_automatic_pipeline_reconciles_stale_tracking(monkeypatch, tmp_path):
    """已完结自动导入也必须清理同作品旧追更标记，不能只在手动入口生效。"""
    from app.mirror.result import MirrorGenerateResult

    source_file = tmp_path / "作品A" / "S01E01.mkv"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"video")
    plan = ImportPlan(
        plan_id="completed-plan",
        source="pan115",
        status="confirmed",
        import_family="anime",
        import_scope="",
        items=[ImportPlanItem(
            id="episode-1",
            plan_id="completed-plan",
            source="pan115",
            resource_type="video",
            action="generate_strm",
            real_path=str(source_file),
            work_id="work-a",
            work_title="作品A",
            media_type="tv",
            show_type="anime_series",
            card_type="main_series",
            series_group="作品A",
            group_type="season",
            season_number=1,
            episode_number=1,
        )],
    )
    calls = []
    monkeypatch.setattr("app.import_pipeline.service.load_import_plan", lambda plan_id: plan)
    monkeypatch.setattr(
        "app.import_pipeline.service.generate_mirror",
        lambda _plan: MirrorGenerateResult(
            plan_id=plan.plan_id,
            source=plan.source,
            status="success",
            generated_count=1,
        ),
    )
    monkeypatch.setattr(
        "app.tracking.registration.register_seasonal_plan",
        lambda _plan: calls.append("register") or {"registered": 1},
    )
    monkeypatch.setattr(
        "app.tracking.registration.reconcile_tracking_bindings_for_plan",
        lambda _plan: calls.append("reconcile") or {"removed": 1},
    )

    result = run_auto_import_pipeline(
        "pan115",
        plan.plan_id,
        include_scrape=False,
    )

    assert result["status"] == "succeeded"
    assert calls == ["reconcile"]
    # 成功路径日志同样采用统一 schema，末条为完成（done）
    logs = result["logs"]
    assert logs
    for entry in logs:
        assert {"time", "kind", "message"} <= set(entry), f"pipeline 日志 schema 不完整: {entry}"
    assert logs[-1]["kind"] == "done"
    assert logs[-1]["message"] == "自动导入流水线完成"


def test_automatic_pipeline_does_not_report_success_when_scrape_indexing_fails(monkeypatch):
    """刮削阶段构建不到作品卡片时，整条自动流水线必须停在失败阶段。"""
    plan = ImportPlan(
        plan_id="scrape-index-failed",
        source="baidu",
        status="executed",
        import_family="anime",
        import_scope="seasonal",
        items=[],
    )
    monkeypatch.setattr("app.import_pipeline.service.load_import_plan", lambda plan_id: plan)
    monkeypatch.setattr(
        "app.import_pipeline.service.run_auto_scrape",
        lambda **kwargs: {
            "auto_scraped": 0,
            "review_queued": 0,
            "failed": 1,
            "results": [{"status": "failed", "error": "媒体库索引刷新失败"}],
        },
    )

    result = run_auto_import_pipeline("baidu", plan.plan_id, include_scrape=True)

    assert result["status"] == "blocked"
    assert result["stage"] == "scrape"
    assert "媒体库索引刷新失败" in result["error"]
