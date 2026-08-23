"""运行时切换后不能再暴露旧媒体执行入口。"""

from __future__ import annotations

from pathlib import Path


def test_main_registers_v4_media_routes_without_legacy_execution_routers():
    from app import main

    routes = {
        str(getattr(context.starlette_route, "path", getattr(context.original_route, "path", "")))
        for route in main.app.routes
        if hasattr(route, "effective_route_contexts")
        for context in route.effective_route_contexts()
    }
    routes.update(getattr(route, "path", "") for route in main.app.routes)
    assert "/api/v4/imports/preview" in routes
    assert "/api/v4/library" in routes
    assert "/api/imports/{source}/confirm" not in routes
    assert "/api/media-presets" not in routes
    assert "/api/mirror/generate" not in routes
    assert "app.pipeline.handlers" not in Path(main.__file__).read_text(encoding="utf-8")


def test_v4_execution_modules_do_not_reparse_or_write_legacy_plan_state():
    root = Path(__file__).resolve().parents[1] / "app" / "media_v4"
    mirror = (root / "jobs" / "mirror.py").read_text(encoding="utf-8")
    scrape = (root / "jobs" / "scrape.py").read_text(encoding="utf-8")
    assert "recognize_media" not in mirror
    assert "recognize_media" not in scrape
    assert "plan_recognizer" not in mirror
    assert "plan_recognizer" not in scrape


def test_legacy_execution_packages_and_routers_are_removed():
    app_root = Path(__file__).resolve().parents[1] / "app"
    for package in (
        "catalog",
        "db",
        "import_plan",
        "import_pipeline",
        "jobs",
        "library",
        "media_presets",
        "mirror",
        "pipeline",
        "raw",
        "sources",
        "tasks",
        "tracking",
    ):
        assert not list((app_root / package).glob("*.py")), package
    for router in (
        "imports.py",
        "library.py",
        "media_presets.py",
        "mirror.py",
        "openlist.py",
        "playback.py",
        "scrape.py",
        "sources.py",
        "tasks.py",
        "tracking.py",
    ):
        assert not (app_root / "api" / router).exists(), router
