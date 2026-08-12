# -*- coding: utf-8 -*-
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
    assert ".media-flow-grid.parse-stage" in css
    assert "grid-template-columns: 1fr !important" in css
    assert ".media-directory-row" in css
    assert "grid-template-columns: 30px minmax(280px, 560px) minmax(120px, 200px) 96px 156px" in css
    assert '"index path note status actions"' in css
    assert '"index path status"' in css
    assert '". note actions"' in css
    # “先折叠、不重叠”的真实实现是容器查询降级本身，不是某条视口相关的右侧留白：
    # 1) src/index.css 的 "Desktop main viewport boundary" 决策明确不为窗口控制预留列，
    #    .app-main 独占 sidebar 右边缘到 right:0 的全部空间；
    # 2) .desktop-titlebar 是 fixed / z-index 100，媒体管理的 .media-flow-header 是 .app-main
    #    (top:40px) 内的 sticky / z-index 8，两者分处不同定位上下文，结构上不可能重叠；
    # 3) Windows 响应式规范要求边距为 4 epx 的整数倍，而 2.5vw 在多数窗口宽度下会算出
    #    非整数、非 4 倍数的值，与项目 4/8px 几何规范冲突。
    # 因此这里改为直接锁住窄容器下的折叠行为：命令栏纵向堆叠、工具按钮撑满。
    narrow_start = css.index("@container media-flow (max-width: 760px)")
    narrow_block = css[narrow_start : css.index("@container media-flow (max-width: 480px)")]
    assert "flex-direction: column" in narrow_block
    assert ".media-flow-utilities .fui-Button { width: 100%; }" in narrow_block
    # 单一 command surface 在窄容器下按行堆叠，标题/导航/维护入口不重叠
    assert ".media-flow-command-bar {\n    grid-template-columns: 1fr;" in narrow_block
    assert ".media-source-controls > label:last-child:nth-child(odd)" in css
    assert 'className="media-directory-index"' in page
    assert 'className="media-directory-note"' in page
