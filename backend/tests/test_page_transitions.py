from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_detail_navigation_reuses_library_preview_without_full_page_loader():
    """打开作品时应立即承接卡片数据，不再闪回全屏胶片加载动画。"""
    detail = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "const initialWork = selectedWorkId" in detail
    assert "peekWorkDetail(selectedWorkId)" in detail
    assert "useState<any>(() => initialWork)" in detail
    assert "if (loading) return <LoadingState" not in detail
    assert "detail-load-progress" in detail


def test_app_uses_short_contextual_page_transition_without_forced_delay():
    """详情切换使用原生 View Transition，并在减少动效时直接切换。"""
    app = (ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
    library = (ROOT / "src" / "stores" / "library.ts").read_text(encoding="utf-8")
    styles = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "pageTransitionKey" not in app
    assert "<WorkDetailPage key={selectedWorkId || 'detail'} />" in app
    assert "documentWithTransition.startViewTransition(update)" in library
    assert "window.matchMedia('(prefers-reduced-motion: reduce)').matches" in library
    assert "document.querySelector<HTMLElement>('.app-main')?.scrollTo({ top: 0, behavior: 'auto' })" in library
    assert "::view-transition-group(app-page-content)" in styles
    assert "animation-duration: 160ms" in styles
    assert "@keyframes app-page-drill-in" not in styles
