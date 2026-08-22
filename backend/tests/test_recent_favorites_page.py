from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_recent_and_favorites_have_independent_pages_and_sidebar_entries():
    """收藏应是独立入口，最近观看页不能再混入收藏内容。"""
    page = (ROOT / "src" / "pages" / "RecentPage.tsx").read_text(encoding="utf-8")
    favorites_page = (ROOT / "src" / "pages" / "FavoritesPage.tsx").read_text(encoding="utf-8")
    sidebar = (ROOT / "src" / "components" / "shell" / "Sidebar.tsx").read_text(encoding="utf-8")
    titlebar = (ROOT / "src" / "components" / "shell" / "DesktopTitleBar.tsx").read_text(encoding="utf-8")
    app = (ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
    ui_store = (ROOT / "src" / "stores" / "ui.ts").read_text(encoding="utf-8")
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "const recentWorks = selectRecentWorks(sourceWorks, history);" in page
    assert "recentWorks.slice(0, RECENT_SECTION_LIMIT)" in page
    assert "recentViewingPriority" in page
    assert "selectFavoriteWorks" not in page
    assert "我的收藏" not in page
    assert "selectFavoriteWorks" in favorites_page
    assert "我的收藏" in favorites_page
    assert "goFavorites" in ui_store
    assert "'favorites'" in ui_store
    assert "FavoritesPage" in app
    assert "label: '我的收藏'" in sidebar
    assert "sidebar-nav-divider" in sidebar
    assert "sidebar-search" not in sidebar
    assert "desktop-titlebar-search" in titlebar
    assert "搜索作品" in titlebar
    assert ".sidebar-nav-divider" in css
    assert ".desktop-titlebar-search" in css


def test_recent_and_favorites_render_as_unframed_content_grids():
    """历史与收藏的非空状态只保留作品网格，不再重复区块标题或包裹卡片。"""
    recent_page = (ROOT / "src" / "pages" / "RecentPage.tsx").read_text(encoding="utf-8")
    favorites_page = (ROOT / "src" / "pages" / "FavoritesPage.tsx").read_text(encoding="utf-8")

    assert "recent-section-heading" not in recent_page
    assert "优先保留最近或高频打开的作品" not in recent_page
    assert "recent-section" not in recent_page
    assert "FAVORITE_GROUPS" not in favorites_page
    assert "收藏的番剧" not in favorites_page
    assert "recent-section" not in favorites_page
    assert "recent-card-grid" in recent_page
    assert "category-grid-wrap favorites-grid-wrap" in favorites_page
    assert "works={favoriteWorks}" in favorites_page
    assert "VirtualizedPosterGrid" in favorites_page


def test_sidebar_keeps_all_media_entries_visible_with_a_stable_library_heading_slot():
    """展开态显示媒体库标题，折叠态隐藏文字但保留同高标题槽。"""
    sidebar = (ROOT / "src" / "components" / "shell" / "Sidebar.tsx").read_text(encoding="utf-8")
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "sidebar-library-heading" in sidebar
    assert ">媒体库</span>" in sidebar
    assert "sidebar-nav-group-library" in sidebar
    assert ".sidebar-library-heading-slot" in css
    assert ".app-sidebar.collapsed .sidebar-library-heading" in css
    assert "--sidebar-row-height: 54px" in css


def test_global_source_filter_has_a_titlebar_menu_and_reaches_every_library_view():
    """来源筛选入口位于顶栏，首页、分类、历史与收藏均使用同一来源状态。"""
    titlebar = (ROOT / "src" / "components" / "shell" / "DesktopTitleBar.tsx").read_text(encoding="utf-8")
    home = (ROOT / "src" / "pages" / "HomePage.tsx").read_text(encoding="utf-8")
    category = (ROOT / "src" / "pages" / "CategoryPage.tsx").read_text(encoding="utf-8")
    recent = (ROOT / "src" / "pages" / "RecentPage.tsx").read_text(encoding="utf-8")
    favorites = (ROOT / "src" / "pages" / "FavoritesPage.tsx").read_text(encoding="utf-8")

    assert "titlebar-source-filter" in titlebar
    assert "setSource" in titlebar
    assert "115 网盘" in titlebar
    assert "百度网盘" in titlebar
    assert "本地" in titlebar
    # 六处内联筛选已收敛到 matchesSourceFilter（多来源命中语义）
    assert "matchesSourceFilter(work, source)" in home
    assert "matchesSourceFilter(work, source)" in category
    assert "matchesSourceFilter(work, source)" in recent
    assert "matchesSourceFilter(work, source)" in favorites


def test_favorite_toggle_updates_the_library_index_used_by_recent_page():
    """详情页收藏后无需重新加载，最近观看页即可读取新的收藏状态。"""
    detail_page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")
    library_store = (ROOT / "src" / "stores" / "library.ts").read_text(encoding="utf-8")

    assert "updateWorkWatchStatus" in detail_page
    assert "updateWorkWatchStatus: (workId, watchStatus)" in library_store
