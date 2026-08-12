# -*- coding: utf-8 -*-
"""桌面标题栏必须与主内容滚动容器分层。"""

from pathlib import Path


def test_desktop_main_scroll_starts_below_titlebar():
    css = (Path(__file__).resolve().parents[2] / "src" / "index.css").read_text(encoding="utf-8")

    assert "Desktop content scroll boundary" in css
    assert "height: 100vh !important" in css
    assert "margin-top: 40px !important" in css
    assert "height: calc(100vh - 40px) !important" in css
    assert "overflow-y: auto !important" in css
    assert "Desktop main viewport boundary" in css
    assert "left: var(--sidebar-width) !important" in css
    assert "right: 0 !important" in css


def test_detail_fixed_backdrop_uses_the_same_sidebar_boundary_as_main_content():
    css = (Path(__file__).resolve().parents[2] / "src" / "index.css").read_text(encoding="utf-8")

    assert "--detail-fixed-left" not in css
    assert "--detail-fixed-width" not in css
    assert "left: var(--sidebar-width) !important" in css
    assert "scrollbar-gutter: auto" in css


def test_collapsed_sidebar_does_not_apply_a_second_main_offset():
    css = (Path(__file__).resolve().parents[2] / "src" / "index.css").read_text(encoding="utf-8")

    assert ".app-shell:has(.app-sidebar.collapsed) main" not in css


def test_virtual_poster_grid_tracks_the_app_main_scroll_container():
    component = (Path(__file__).resolve().parents[2] / "src" / "components" / "library" / "VirtualizedPosterGrid.tsx").read_text(encoding="utf-8")

    assert "closest<HTMLElement>('.app-main')" in component
    assert "scrollContainer.scrollTop" in component
    assert "if (scrollContainer)" in component
    assert "scrollContainer.addEventListener('scroll', updateScrollPosition" in component


def test_virtual_poster_grid_coalesces_scroll_updates_per_animation_frame():
    component = (Path(__file__).resolve().parents[2] / "src" / "components" / "library" / "VirtualizedPosterGrid.tsx").read_text(encoding="utf-8")

    assert "let scrollFrame = 0;" in component
    assert "scrollFrame = window.requestAnimationFrame" in component
    assert "window.cancelAnimationFrame(scrollFrame)" in component
    assert "if (!hasVisibleWindowChanged(current, scrollTop)) return current;" in component


def test_virtual_poster_grid_clamps_the_start_row_when_results_shrink():
    component = (Path(__file__).resolve().parents[2] / "src" / "components" / "library" / "VirtualizedPosterGrid.tsx").read_text(encoding="utf-8")

    assert "const maxStartRow = Math.max(0, totalRows - 1);" in component
    assert "const startRow = Math.min(maxStartRow, rawStartRow);" in component
    assert "}, [onColumnCapacityChange, requestedColumns, seriesCardImageMode, works.length]);" in component


def test_category_changes_reset_the_main_scroll_position():
    page = (Path(__file__).resolve().parents[2] / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")

    assert "main?.scrollTo({ top: 0, behavior: 'auto' });" in page
    assert "}, [activeCategory, source]);" in page
