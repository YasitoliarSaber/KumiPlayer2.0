from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_related_navigation_prefetches_detail_data_and_backdrop_on_hover():
    """关联卡片悬停应预取详情与背景图，点击时复用已有请求/缓存。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "const prewarmDetailNavigation = (workId: string, imageUrl = '') =>" in page
    assert "void getWorkDetail(workId)" in page
    assert "preloadDetailImage" in page
    assert "prewarmDetailNavigation(related.work_id, previewImage)" in page
    assert "prewarmDetailNavigation(item.work_id" in page


def test_detail_backdrop_falls_back_to_poster_when_fanart_is_missing():
    """没有独立背景图的作品仍应使用海报承接连续背景，不能永久显示空舞台。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "const backdropPath = work.fanart_path || work.poster_path || '';" in page
    assert "const fanartImage = backdropPath ? assetUrl(backdropPath, 'detailBackdrop') : '';" in page
    assert "const posterBackdropImage = work.poster_path ? assetUrl(work.poster_path, 'detailBackdrop') : '';" in page
    assert "onError={handleBackdropImageError}" in page
    assert page.count("onError={handleBackdropImageError}") == 1
    assert "'--detail-backdrop-image'" not in page
