from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_related_work_navigation_keeps_prefetch_without_preview_cleanup_state():
    """移除复制预览后，相关作品仍要预热并沿用统一详情导航。"""
    page = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "const navigateToWorkDetail = (workId: string) =>" in page
    assert "prewarmDetailNavigation(workId);" in page
    assert "void openWorkDetail(workId);" in page
    assert "hoverPreview" not in page
    assert "onClick={() => navigateToWorkDetail(related.work_id)}" in page
    assert "onMouseEnter={() => prewarmDetailNavigation(related.work_id, previewImage)}" in page


def test_detail_navigation_commit_resets_the_native_main_scroller():
    """切换到另一部作品时，新详情页必须从顶部首屏开始显示。"""
    store = (ROOT / "src" / "stores" / "library.ts").read_text(encoding="utf-8")

    commit_start = store.index("function commitDetailNavigation(workId: string) {")
    commit_end = store.index("\n}\n\nfunction decodeDetailArtwork", commit_start)
    commit = store[commit_start:commit_end]

    assert "const update = () => {" in commit
    assert "useUiStore.getState().goDetail(workId)" in commit
    assert "document.querySelector<HTMLElement>('.app-main')" in commit
    assert "scrollTo({ top: 0, behavior: 'auto' })" in commit
    assert "behavior: 'smooth'" not in commit
    assert commit.index("goDetail(workId)") < commit.index("scrollTo({ top: 0")
