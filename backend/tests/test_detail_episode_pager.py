from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PAGE_PATH = ROOT / "src" / "pages" / "WorkDetailPage.tsx"
CSS_PATH = ROOT / "src" / "index.css"


def _last_rule_body(css: str, selector: str) -> str:
    prefix = f"{selector} {{"
    assert prefix in css
    return css.rsplit(prefix, 1)[1].split("}", 1)[0]


def _rule_bodies(css: str, selector: str) -> list[str]:
    prefix = f"{selector} {{"
    return [chunk.split("}", 1)[0] for chunk in css.split(prefix)[1:]]


def test_episode_slider_tracks_scroll_without_rerendering_detail_page():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "const [episodeStripPosition, setEpisodeStripPosition]" not in page
    assert "const episodeStripSliderRef = useRef<HTMLInputElement>(null);" in page
    assert "ref={episodeStripSliderRef}" in page
    assert 'defaultValue="0"' in page
    assert "onInput={(event) => seekEpisodeStrip(Number(event.currentTarget.value))}" in page

    assert "strip.scrollLeft = maxScroll * (clamped / 100);" in page
    assert "slider.value = String(clamped);" in page
    assert (
        "slider.style.setProperty('--episode-slider-progress', `${clamped}%`);"
        in page
    )
    assert "setEpisodeStripPosition" not in page

    # 左右按钮仍是离散翻页，应保留短平滑滚动。
    assert "strip.scrollBy({ left: direction * Math.max(320, strip.clientWidth * 0.86), behavior: 'smooth' });" in page


def test_episode_strip_and_slider_use_direct_fluent_interaction_styles():
    css = CSS_PATH.read_text(encoding="utf-8")

    strip_rule = next(
        body
        for body in _rule_bodies(
            css,
            ".detail-page.detail-classic-page .detail-episode-grid.thumbnail-strip",
        )
        if "scroll-snap-type" in body
    )
    assert "scroll-behavior: smooth;" not in strip_rule
    assert "scroll-snap-type: x mandatory" not in strip_rule
    assert "scroll-behavior: auto;" in strip_rule
    assert "scroll-snap-type: x proximity;" in strip_rule

    slider = _last_rule_body(
        css,
        ".detail-page.detail-classic-page .episode-strip-slider",
    )
    assert "--episode-slider-progress: 0%;" in slider
    assert "width: clamp(92px, 7vw, 132px);" in slider
    assert "appearance: none;" in slider

    webkit_track = _last_rule_body(
        css,
        ".detail-page.detail-classic-page .episode-strip-slider::-webkit-slider-runnable-track",
    )
    moz_track = _last_rule_body(
        css,
        ".detail-page.detail-classic-page .episode-strip-slider::-moz-range-track",
    )
    for track in (webkit_track, moz_track):
        assert "height: 4px;" in track
        assert "var(--episode-slider-progress)" in track
        assert "var(--range-fill)" in track

    webkit_thumb = next(
        body
        for body in _rule_bodies(
            css,
            ".detail-page.detail-classic-page .episode-strip-slider::-webkit-slider-thumb",
        )
        if "width:" in body
    )
    moz_thumb = next(
        body
        for body in _rule_bodies(
            css,
            ".detail-page.detail-classic-page .episode-strip-slider::-moz-range-thumb",
        )
        if "width:" in body
    )
    assert "width: 14px;" in webkit_thumb
    assert "height: 14px;" in webkit_thumb
    assert "width: 14px;" in moz_thumb
    assert "height: 14px;" in moz_thumb

    assert ".episode-strip-slider:focus-visible" in css
    assert ".episode-strip-slider:is(:hover, :focus-visible)::-webkit-slider-thumb" in css
    assert ".episode-strip-slider:is(:hover, :focus-visible)::-moz-range-thumb" in css
    assert ":root[data-motion='reduced'] .detail-page.detail-classic-page .episode-strip-slider" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_detail_form_focus_styles_do_not_paint_the_episode_slider_background():
    css = CSS_PATH.read_text(encoding="utf-8")

    assert ".detail-page input:not([type='range'])," in css
    assert ".detail-page input:not([type='range']):focus," in css
    assert "\n.detail-page input,\n.detail-page select" not in css
    assert "\n.detail-page input:focus,\n.detail-page select:focus" not in css


def test_episode_sort_toggle_and_persisted_state_are_removed():
    page = PAGE_PATH.read_text(encoding="utf-8")
    store = (ROOT / "src" / "stores" / "ui.ts").read_text(encoding="utf-8")

    assert "episodeSortDirection" not in page
    assert "setEpisodeSortDirection" not in page
    assert "title={episodeSortDirection === 'asc' ? '正序' : '倒序'}" not in page
    assert "episodeSortDirection" not in store
    assert "setEpisodeSortDirection" not in store


def test_episode_command_bar_and_primary_play_button_are_compact():
    css = CSS_PATH.read_text(encoding="utf-8")

    season_dropdown = _last_rule_body(
        css,
        ".detail-page.detail-classic-page .detail-season-dropdown",
    )
    assert "width: 112px;" in season_dropdown
    assert "min-width: 112px;" in season_dropdown
    assert "height: 32px;" in season_dropdown

    pager_button = next(
        body
        for body in _rule_bodies(
            css,
            ".detail-page.detail-classic-page .detail-episode-pager button",
        )
        if "width:" in body
    )
    assert "width: 32px;" in pager_button
    assert "height: 32px;" in pager_button

    play_stack = _last_rule_body(
        css,
        ".detail-page.detail-classic-page .detail-hero-actions .detail-play-stack",
    )
    assert "width: 292px;" in play_stack
    assert "max-width: 100%;" in play_stack

    play_copy = _last_rule_body(
        css,
        ".detail-page.detail-classic-page .detail-continue-btn .detail-continue-copy",
    )
    assert "overflow: hidden;" in play_copy
