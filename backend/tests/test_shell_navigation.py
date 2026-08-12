# -*- coding: utf-8 -*-
"""桌面标题栏返回与侧栏布局回归。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_ui_store_owns_real_back_and_forward_navigation_history():
    store = (ROOT / "src" / "stores" / "ui.ts").read_text(encoding="utf-8")

    assert "navigationHistory:" in store
    assert "forwardHistory:" in store
    assert "canGoBack:" in store
    assert "canGoForward:" in store
    assert "goBack: () =>" in store
    assert "goForward: () =>" in store
    assert "forwardHistory: []" in store
    assert "MAX_NAVIGATION_HISTORY" in store
    assert "selectedWorkId" in store
    assert "activeCategory" in store
    assert "query" in store


def test_titlebar_contains_back_forward_and_single_brand_lockup():
    titlebar = (ROOT / "src" / "components" / "shell" / "DesktopTitleBar.tsx").read_text(encoding="utf-8")

    assert "ArrowLeft" in titlebar
    assert "ArrowRight" in titlebar
    assert "<Menu aria-hidden" not in titlebar
    assert "goBack" in titlebar
    assert "goForward" in titlebar
    assert "toggleSidebarVisibility" in titlebar
    assert "PanelLeft" in titlebar
    assert 'className="titlebar-navigation-button titlebar-sidebar-visibility-button"' in titlebar
    assert "disabled={!canGoBack}" in titlebar
    assert "disabled={!canGoForward}" in titlebar
    assert 'aria-label="前进到下个界面"' in titlebar
    assert "titlebar-navigation-button" in titlebar
    assert titlebar.count("/brand/kumiplayer-app-icon.svg") == 1


def test_compact_titlebar_keeps_brand_visible_and_moves_it_left():
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert ".desktop-titlebar .desktop-titlebar-brand" in css
    assert "padding-left: 2px !important;" in css
    assert ".desktop-titlebar-wordmark { display: inline; }" in css


def test_sidebar_has_secondary_toggle_counts_and_no_duplicate_brand():
    sidebar = (ROOT / "src" / "components" / "shell" / "Sidebar.tsx").read_text(encoding="utf-8")

    assert "sidebar-library-heading-slot" in sidebar
    assert "sidebar-library-heading" in sidebar
    assert ">媒体库</span>" in sidebar
    assert "toggleSidebar" in sidebar
    assert 'className="sidebar-header sidebar-library-heading-slot"' in sidebar
    assert "aria-expanded={sidebarExpanded}" in sidebar
    assert 'className="sidebar-library-toggle"' not in sidebar
    assert 'className="sidebar-library-toggle-icon"' in sidebar
    assert "PanelLeftOpen" in sidebar
    assert "PanelLeftClose" in sidebar
    assert "sidebar-category-count" in sidebar
    assert "/brand/kumiplayer-app-icon.svg" not in sidebar


def test_expanded_and_collapsed_sidebar_share_row_geometry():
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "--sidebar-row-height: 54px" in css
    assert "--sidebar-heading-height: 64px" in css
    assert "height: var(--sidebar-row-height) !important" in css
    assert "height: var(--sidebar-heading-height) !important" in css
    assert ".app-sidebar.expanded .sidebar-nav-item,\n.app-sidebar.collapsed .sidebar-nav-item" in css
    assert ".app-sidebar.expanded .sidebar-library-heading-slot,\n.app-sidebar.collapsed .sidebar-library-heading-slot" in css
    assert ".app-sidebar.expanded .sidebar-bottom,\n.app-sidebar.collapsed .sidebar-bottom" in css
    assert ".sidebar-library-toggle" in css
    assert ".sidebar-library-heading-slot:hover" in css
    assert "cursor: pointer" in css
    assert ".sidebar-category-count" in css
    assert ".app-sidebar.expanded .sidebar-library-heading-slot {\n  justify-content: flex-start !important;" in css
    assert "padding: 0 46px 0 20px !important;" in css


def test_cinema_titlebar_brand_has_no_transparent_outer_ring():
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert ":root[data-theme='cinema'] .desktop-titlebar-mark" in css
    assert "background: #121b2d !important;" in css
    assert "border-radius: 6px !important;" in css


def test_titlebar_owns_full_sidebar_visibility_and_sidebar_only_owns_width():
    """完全显隐由标题栏负责，侧栏内部只切换完整栏与图标栏。"""
    titlebar = (ROOT / "src" / "components" / "shell" / "DesktopTitleBar.tsx").read_text(encoding="utf-8")
    sidebar = (ROOT / "src" / "components" / "shell" / "Sidebar.tsx").read_text(encoding="utf-8")

    assert "toggleSidebarVisibility" in titlebar
    assert "PanelLeft" in titlebar
    assert "aria-expanded={!sidebarHidden}" in titlebar
    assert "显示导航栏" in titlebar
    assert "完全隐藏导航栏" in titlebar
    assert "toggleSidebarVisibility" not in sidebar
    assert "sidebar-rail-toggle" not in sidebar
    assert "sidebar-edge-hover-zone" not in sidebar
    assert "收起为图标栏" in sidebar
    assert "展开完整侧栏" in sidebar
    assert "PanelLeftOpen" in sidebar
    assert "PanelLeftClose" in sidebar


def test_sidebar_visibility_store_restores_last_visible_mode():
    store = (ROOT / "src" / "stores" / "ui.ts").read_text(encoding="utf-8")

    assert "lastVisibleSidebarMode" in store
    assert "state.lastVisibleSidebarMode" in store
    assert "sidebarMode: 'hidden'" in store
    assert "version: 4" in store


def test_old_sidebar_rail_styles_are_removed():
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert ".titlebar-sidebar-visibility-button[aria-expanded='true']" in css
    assert "sidebar-rail-toggle" not in css
    assert "sidebar-edge-hover-zone" not in css
