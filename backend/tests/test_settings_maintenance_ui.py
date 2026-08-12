# -*- coding: utf-8 -*-
"""维护页关键交互的静态回归检查。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_maintenance_offers_clear_scope_and_visible_progress():
    page = (ROOT / "src" / "components" / "media" / "LibraryMaintenancePanel.tsx").read_text(encoding="utf-8")
    api = (ROOT / "src" / "api" / "library.ts").read_text(encoding="utf-8")

    assert "deleteScopeLabel" in page
    assert "deleteLibraryPreview(selectedSource)" in page
    assert "preview.source !== selectedSource" in page
    assert "网盘挂载文件、本地原视频和外部原始 TXT 始终不会被删除" in page
    assert "operation.phase" in page
    assert "selectedSource" in page
    assert "确认删除{deleteScopeLabel}" in page
    assert "需人工检查" in page
    assert "诊断项" in page
    assert "diagnosticItems.map" in page
    assert "item.scrape_title" in page
    assert "item.message" in page
    assert "LibraryDiagnosticsResponse" in api
