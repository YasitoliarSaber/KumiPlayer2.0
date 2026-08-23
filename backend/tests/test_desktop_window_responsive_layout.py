"""桌面窗口和媒体管理页必须在紧凑尺寸下保持可用。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_tauri_window_has_a_workable_desktop_minimum_size():
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    window = config["app"]["windows"][0]

    assert window["minWidth"] <= 760
    assert window["minHeight"] <= 560


def test_poster_grid_uses_measured_columns_and_preserves_vertical_artwork():
    component = (ROOT / "src/components/library/VirtualizedPosterGrid.tsx").read_text(encoding="utf-8")
    metrics = (ROOT / "src/components/library/posterGridMetrics.ts").read_text(encoding="utf-8")
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert "effectiveColumns" in component
    assert "ResizeObserver" in component
    assert "MIN_CARD_WIDTH" in metrics
    assert "safeColumns" not in component
    assert ".poster-media-vertical img" in css
    assert "object-fit: contain" in css
    assert ".poster-card:hover .poster-media-vertical img" in css
    assert "transform: none" in css
    assert css.index(".poster-card:hover .poster-media-vertical img") > css.index(".poster-card:hover .poster-media img")
    assert "--sidebar-width: 56px !important" in css
    navigation_layer = css.split("/* Three-stage NavigationView layout: hidden, compact icon rail, expanded pane. */", 1)[1]
    assert "@media (max-width: 760px)" in navigation_layer
    assert ".app-shell.sidebar-expanded {\n    --sidebar-width: 56px !important;\n  }" in navigation_layer
    assert ".app-shell.sidebar-expanded .app-sidebar {\n    width: 56px !important;\n  }" in navigation_layer


def test_responsive_css_does_not_override_virtual_poster_grid_columns():
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert "grid-template-columns: repeat(var(--category-columns, 5), minmax(0, 1fr));" in css
    assert ".category-grid {\n    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));" not in css
    assert ".category-grid {\n    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));" not in css


def test_first_run_setup_explains_current_runtime_requirements():
    page = (ROOT / "src/pages/FirstRunSetup.tsx").read_text(encoding="utf-8")

    assert "Windows 10 / 11" in page
    assert "WebView2" in page
    assert "后端、内置播放器和功能插件随软件安装" in page
    assert "内置干净 MPV" in page
    assert "安装器会联网补齐" in page
    assert "Python 3.12" not in page


def test_media_management_collapses_before_controls_can_overlap():
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")
    page = (ROOT / "src" / "pages" / "MediaManagementPage.tsx").read_text(encoding="utf-8")

    assert "Media management responsive workbench layout" in css
    assert "container-type: inline-size" in css
    assert "container-name: media-flow" in css
    assert "@container media-flow (max-width: 1420px)" in css
    assert "@container media-flow (max-width: 1080px)" in css
    assert "@container media-flow (max-width: 900px)" in css
    assert "@container media-flow (max-width: 760px)" in css
    assert ".media-v4-settings-list" in css
    assert ".media-v4-setting-row" in css
    assert ".media-v4-select-control" in css
    assert ".media-v4-path-row" in css
    assert "@container media-flow (max-width: 620px) {\n  .media-v4-source-options { grid-template-columns: 1fr; }" in css
    assert ".media-v4-source-options small {" in css
    assert "font-size: 12px" in css.split(".media-v4-source-options small {", 1)[1].split("}", 1)[0]
    assert "font-size: 12px" in css.split(".media-v4-setting-copy span {", 1)[1].split("}", 1)[0]
    assert 'className="media-flow-page media-v4-page"' in page
    assert 'className="media-stage-shell media-v4-stage-panel media-v4-source-card"' in page
    assert 'className="media-stage-shell media-v4-stage-panel media-v4-review-card"' in page
