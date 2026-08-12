from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PAGE_PATH = ROOT / "src" / "pages" / "WorkDetailPage.tsx"
CSS_PATH = ROOT / "src" / "index.css"
HOVER_LAYER_MARKER = "/* 详情页三类媒体卡片即时悬停："


def _rule_body(css: str, selector: str) -> str:
    prefix = f"{selector} {{"
    assert prefix in css
    return css.split(prefix, 1)[1].split("}", 1)[0]


def test_detail_removes_delayed_duplicate_image_preview_path():
    page = PAGE_PATH.read_text(encoding="utf-8")
    css = CSS_PATH.read_text(encoding="utf-8")

    for legacy_symbol in (
        "DetailHoverPreview",
        "HOVER_PREVIEW_DELAY_MS",
        "hoverPreview",
        "openHoverPreview",
        "closeHoverPreview",
        "clearHoverPreviewTimers",
        "switchSimilarPreview",
    ):
        assert legacy_symbol not in page

    for legacy_style in (
        ".detail-hover-preview",
        ".detail-preview-episode",
        ".detail-preview-stage",
        ".detail-preview-stage-actions",
        "detail-preview-enter",
    ):
        assert legacy_style not in css

    # 其他弹窗和右键菜单仍通过 Portal 渲染；作品详情预加载仍保留。
    assert "createPortal(" in page
    assert "prewarmDetailNavigation(related.work_id, previewImage)" in page
    assert "prewarmDetailNavigation(item.work_id, previewImage)" in page
    assert "onFocus={() => prewarmDetailNavigation(related.work_id, previewImage)}" in page
    assert "onFocus={() => prewarmDetailNavigation(item.work_id, previewImage)}" in page


def test_three_detail_regions_use_independent_layout_neutral_scales():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert HOVER_LAYER_MARKER in css
    hover_layer = css.split(HOVER_LAYER_MARKER, 1)[1]

    selectors_and_scales = {
        ".detail-page.detail-classic-page .episode-button:is(:hover, :focus-visible) .episode-thumb": "1.18",
        ".detail-page.detail-classic-page .detail-related-card:is(:hover, :focus-visible)": "1.04",
        ".detail-page.detail-classic-page .detail-similar-card:is(:hover, :focus-visible)": "1.05",
    }
    for selector, scale in selectors_and_scales.items():
        rule = _rule_body(hover_layer, selector)
        assert f"transform: scale({scale}) !important" in rule
        assert "width:" not in rule
        assert "height:" not in rule
        assert "margin:" not in rule

    episode_thumb = _rule_body(
        hover_layer,
        ".detail-page.detail-classic-page .episode-thumb",
    )
    related_card = _rule_body(
        hover_layer,
        ".detail-page.detail-classic-page .detail-related-card",
    )
    similar_card = _rule_body(
        hover_layer,
        ".detail-page.detail-classic-page .detail-similar-card",
    )
    assert "transform-origin: bottom center" in episode_thumb
    assert "transform-origin: bottom center" in related_card
    assert "transform-origin: bottom center" in similar_card
    assert "transition: transform 165ms var(--ease-out)" in episode_thumb
    assert "transition: transform 165ms var(--ease-out)" in related_card
    assert "transition: transform 165ms var(--ease-out)" in similar_card

    assert "aspect-ratio: 16 / 9" in css
    assert "aspect-ratio: 16 / 10" in css
    assert "object-fit: cover" in css
    assert ".detail-classic-page .episode-button:hover .episode-thumb img" not in css
    assert "transition-delay" not in hover_layer


def test_episode_thumbnail_strip_uses_larger_cards_and_hover_clearance():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert HOVER_LAYER_MARKER in css
    hover_layer = css.split(HOVER_LAYER_MARKER, 1)[1]

    strip_rule = _rule_body(
        hover_layer,
        ".detail-page.detail-classic-page .detail-episode-grid.thumbnail-strip",
    )
    episode_item_rule = _rule_body(
        hover_layer,
        ".detail-page.detail-classic-page .detail-episode-grid.thumbnail-strip .episode-item",
    )

    assert "padding-block: 36px 16px !important" in strip_rule
    assert "padding-inline: 32px !important" in strip_rule
    assert "flex: 0 0 clamp(252px, 18.5vw, 335px) !important" in episode_item_rule


def test_episode_thumbnail_strip_uses_wider_desktop_viewport():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert HOVER_LAYER_MARKER in css
    hover_layer = css.split(HOVER_LAYER_MARKER, 1)[1]

    assert """@media (min-width: 1101px) {
  .detail-page.detail-classic-page .detail-episode-grid.thumbnail-strip {
    width: auto !important;
    max-width: none !important;
    margin-inline: clamp(-40px, -1.8vw, -18px) !important;
  }
}""" in hover_layer


def test_episode_thumbnail_hover_has_no_native_tooltip_overlay():
    """剧集图片悬停时不得用 title 原生提示遮挡画面。"""
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "title={isWatched ? '已看完' : '未观看'}" not in page
    assert "title={isWatched ? '已看完' : '播放这一集'}" not in page
    assert "'播放这一集'" not in page
    assert "aria-label={`${isWatched ? '已看完，' : ''}播放第 ${episode.episode_number} 集：${episodeTitle}`}" in page


def test_edge_origins_and_reduced_motion_prevent_clipping_or_residual_motion():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert HOVER_LAYER_MARKER in css
    hover_layer = css.split(HOVER_LAYER_MARKER, 1)[1]

    for selector_fragment in (
        ".detail-related-card:nth-child(5n + 1)",
        ".detail-related-card:nth-child(5n)",
        ".detail-related-card:nth-child(3n + 1)",
        ".detail-related-card:nth-child(3n)",
        ".detail-related-card:nth-child(odd)",
        ".detail-related-card:nth-child(even)",
        ".detail-similar-card:nth-child(6n + 1)",
        ".detail-similar-card:nth-child(6n)",
        ".detail-similar-card:nth-child(3n + 1)",
        ".detail-similar-card:nth-child(3n)",
        ".detail-similar-card:nth-child(odd)",
        ".detail-similar-card:nth-child(even)",
    ):
        assert selector_fragment in hover_layer

    assert "transform-origin: bottom left" in hover_layer
    assert "transform-origin: bottom right" in hover_layer
    assert hover_layer.count(".detail-related-card:nth-child(n) { transform-origin: bottom center; }") >= 2
    assert hover_layer.count(".detail-similar-card:nth-child(n) { transform-origin: bottom center; }") >= 2
    assert "@media (hover: hover) and (pointer: fine)" in hover_layer
    assert ":root[data-motion='reduced']" in hover_layer
    assert "@media (prefers-reduced-motion: reduce)" in hover_layer
    assert "transform: none !important" in hover_layer
    assert "filter: brightness(1.04)" in hover_layer
