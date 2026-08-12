from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = ROOT / "src" / "index.css"
DETAIL_PATH = ROOT / "src" / "pages" / "WorkDetailPage.tsx"
LIBRARY_CONTROLS_PATH = (
    ROOT / "src" / "components" / "library" / "LibraryViewControls.tsx"
)


def test_detail_and_library_sliders_share_the_modern_range_contract():
    detail = DETAIL_PATH.read_text(encoding="utf-8")
    library = LIBRARY_CONTROLS_PATH.read_text(encoding="utf-8")
    css = CSS_PATH.read_text(encoding="utf-8")

    assert 'className="modern-range episode-strip-slider"' in detail
    assert 'className="modern-range library-card-size-slider"' in library
    assert "'--range-progress':" in library

    assert ".modern-range {" in css
    assert ".modern-range::-webkit-slider-runnable-track {" in css
    assert ".modern-range::-moz-range-track {" in css
    assert ".modern-range::-webkit-slider-thumb {" in css
    assert ".modern-range::-moz-range-thumb {" in css
    assert "height: 4px;" in css
    assert "width: 14px;" in css
    assert "height: 14px;" in css


def test_each_preset_defines_readable_slider_and_detail_command_tokens():
    css = CSS_PATH.read_text(encoding="utf-8")
    required_tokens = (
        "--range-track:",
        "--range-fill:",
        "--range-thumb:",
        "--range-thumb-border:",
        "--detail-command-surface:",
        "--detail-command-fg:",
        "--detail-command-muted:",
        "--detail-command-border:",
    )

    for theme in ("fluent", "mica", "cinema"):
        theme_start = css.rindex(f":root[data-theme='{theme}']")
        theme_end = css.index("}", theme_start)
        theme_block = css[theme_start:theme_end]
        for token in required_tokens:
            assert token in theme_block


def test_modern_range_keeps_a_large_hit_area_and_keyboard_focus():
    css = CSS_PATH.read_text(encoding="utf-8")
    modern_range = css.rsplit(".modern-range {", 1)[1].split("}", 1)[0]

    assert "height: 28px;" in modern_range
    assert "touch-action: pan-y;" in modern_range
    assert ".modern-range:focus-visible {" in css
    assert ".modern-range:disabled {" in css
