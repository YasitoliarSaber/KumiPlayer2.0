# -*- coding: utf-8 -*-
"""来源级媒体库重扫必须保留新番作品卡片。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_full_rescan_preserves_existing_seasonal_work_count(monkeypatch):
    from app.library import service
    from app.library.models import LibraryIndex, WorkIndex
    from app.import_plan.models import ImportPlan
    from app.library.scanner import MirrorScanResult
    from app.scrape.models import ScrapeMap

    ordinary = WorkIndex(work_id="ordinary", title="已完结作品", source="baidu")
    seasonal = WorkIndex(
        work_id="seasonal", title="当前新番", source="baidu",
        import_scope="seasonal",
    )
    existing = LibraryIndex(works=[ordinary, seasonal])
    plan = ImportPlan(plan_id="full", source="baidu", status="confirmed")
    saved = {}

    monkeypatch.setattr(service, "load_library_index", lambda: existing)
    monkeypatch.setattr(
        service,
        "load_latest_confirmed_import_plan",
        lambda source: plan if source == "baidu" else None,
    )
    monkeypatch.setattr(service, "load_scrape_map", lambda: ScrapeMap())
    monkeypatch.setattr(service, "scan_mirror", lambda source=None: MirrorScanResult())
    monkeypatch.setattr(service, "list_tracking_bindings", lambda state=None: [])
    monkeypatch.setattr(
        service,
        "build_library_index",
        lambda *_: LibraryIndex(works=[ordinary], source_summary={"baidu": {"work_count": 1}}),
    )
    monkeypatch.setattr(service, "save_library_index", lambda value: saved.setdefault("index", value) or "index.json")

    result = service.rescan_library()

    assert result["work_count"] == 2
    assert {work.work_id for work in saved["index"].works} == {"ordinary", "seasonal"}
    assert sum(work.import_scope == "seasonal" for work in saved["index"].works) == 1


def test_source_rescan_preserves_only_that_sources_existing_seasonal_works(monkeypatch):
    from app.library import service
    from app.library.models import LibraryIndex, WorkIndex

    existing = LibraryIndex(works=[
        WorkIndex(work_id="baidu-seasonal", source="baidu", import_scope="seasonal"),
        WorkIndex(work_id="local-seasonal", source="local", import_scope="seasonal"),
        WorkIndex(work_id="local-ordinary", source="local"),
    ])
    rebuilt = LibraryIndex(works=[WorkIndex(work_id="baidu-ordinary", source="baidu")])
    monkeypatch.setattr(service, "load_library_index", lambda: existing)

    merged = service._replace_source_in_existing_index("baidu", rebuilt)

    assert {work.work_id for work in merged.works} == {
        "baidu-seasonal", "baidu-ordinary", "local-seasonal", "local-ordinary",
    }


def test_rescan_prefers_explicit_nonseasonal_work_over_stale_seasonal_copy():
    """同一作品重新按已完结导入后，不得继续保留旧的新番身份。"""
    from app.library import service
    from app.library.models import LibraryIndex, WorkIndex

    rebuilt = LibraryIndex(works=[
        WorkIndex(work_id="air", title="AIR", source="pan115", import_scope=""),
    ])
    existing = LibraryIndex(works=[
        WorkIndex(work_id="air", title="AIR", source="pan115", import_scope="seasonal"),
    ])

    merged = service._preserve_existing_seasonal_works(rebuilt, existing, "pan115")

    assert len(merged.works) == 1
    assert merged.works[0].import_scope == ""


def test_rescan_restores_missing_seasonal_cards_from_saved_tracking_baselines(monkeypatch):
    """即使旧版本已丢卡，重扫也应从已保存追更基线恢复作品数量。"""
    from app.library import service
    from app.library.models import LibraryIndex, WorkIndex
    from app.import_plan.models import ImportPlan
    from app.library.scanner import MirrorScanResult
    from app.scrape.models import ScrapeMap

    ordinary = WorkIndex(work_id="ordinary", source="baidu")
    seasonal = WorkIndex(work_id="seasonal", source="baidu", import_scope="seasonal")
    current = {"index": LibraryIndex(works=[ordinary])}
    plan = ImportPlan(plan_id="full", source="baidu", status="confirmed")
    calls = {"repair": 0}

    monkeypatch.setattr(service, "load_library_index", lambda: current["index"])
    monkeypatch.setattr(
        service, "load_latest_confirmed_import_plan",
        lambda source: plan if source == "baidu" else None,
    )
    monkeypatch.setattr(service, "load_scrape_map", lambda: ScrapeMap())
    monkeypatch.setattr(service, "scan_mirror", lambda source=None: MirrorScanResult())
    monkeypatch.setattr(service, "build_library_index", lambda *_: LibraryIndex(works=[ordinary]))
    monkeypatch.setattr(
        service, "save_library_index",
        lambda value: current.__setitem__("index", value) or "index.json",
    )
    monkeypatch.setattr(service, "list_tracking_bindings", lambda state=None: [object()], raising=False)

    def repair(_bindings):
        calls["repair"] += 1
        current["index"].works.append(seasonal)
        return {"restored": 1, "warnings": []}

    monkeypatch.setattr(service, "rebuild_tracking_library_from_bindings", repair)

    result = service.rescan_library()

    assert calls["repair"] == 1
    assert result["work_count"] == 2
    assert result["seasonal_work_count"] == 1
