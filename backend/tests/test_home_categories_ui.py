from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_home_hides_empty_category_bands():
    source = (ROOT / "src" / "pages" / "HomePage.tsx").read_text(encoding="utf-8")

    assert "if (categoryWorks.length === 0) return null" in source
    assert 'className="home-category-section"' in source
    assert "onClick={() => goCategory(category.key)}" in source
    assert "persistentHomeCategories" not in source
    assert 'className="home-category-empty"' not in source


def test_home_library_empty_state_remains_accessible_and_theme_driven():
    source = (ROOT / "src" / "pages" / "HomePage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert '<button type="button" className="home-library-empty-action" onClick={goManage}>' in source
    assert "还没有作品" in source
    assert "当前来源没有作品" in source
    assert "添加媒体" in source
    empty_styles = styles.split(".home-library-empty-content {", 1)[1].split("}", 1)[0]
    assert "var(--text)" in empty_styles
    assert "#fff" not in empty_styles.lower()
    assert "#000" not in empty_styles.lower()


def test_home_keeps_usable_library_content_after_a_partial_refresh_failure():
    store = (ROOT / "src" / "stores" / "library.ts").read_text(encoding="utf-8")
    home = (ROOT / "src" / "pages" / "HomePage.tsx").read_text(encoding="utf-8")

    assert "const libraryRequest = libraryApi.getLibrary" in store
    assert "const historyRequest = playbackApi.getHistory" in store
    assert "await libraryRequest" in store
    assert "播放历史是首页增强信息" in store
    assert (
        "if (error && works.length === 0) "
        "return <CenteredMessage>{error}</CenteredMessage>;"
    ) in home


def test_library_pages_keep_cached_content_during_refresh_failures():
    pages = [
        ROOT / "src" / "pages" / "CategoryPage.tsx",
        ROOT / "src" / "pages" / "FavoritesPage.tsx",
        ROOT / "src" / "pages" / "RecentPage.tsx",
        ROOT / "src" / "pages" / "SearchPage.tsx",
    ]

    for page in pages:
        source = page.read_text(encoding="utf-8")
        assert "if (error && works.length === 0)" in source, page.name
