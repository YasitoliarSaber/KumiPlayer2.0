from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _css_block_after(css: str, anchor: str, selector: str) -> str:
    scoped = css.split(anchor, 1)[1]
    return scoped.split(selector, 1)[1].split("}", 1)[0]


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_cinema_core_palette_uses_fluent_cool_neutrals_and_blue_accent():
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")
    block = _css_block_after(
        css,
        "Final Fluent/Mica visual calibration - 2026-07-08",
        ":root[data-theme='cinema']",
    )

    expected_tokens = {
        "--app-bg": "#080808",
        "--mica-a": "#080808",
        "--mica-b": "#0c0c0c",
        "--mica-c": "#141414",
        "--surface-solid": "#141414",
        "--text": "#f5f5f5",
        "--text-muted": "#a3a3a3",
        "--accent": "#479ef5",
        "--accent-strong": "#62abf5",
        "--accent-contrast": "#071522",
    }
    for token, value in expected_tokens.items():
        assert f"{token}: {value}" in block

    for rejected_warm_token in ("#11100f", "#1d1b19", "#a67f5e", "#d3aa80"):
        assert rejected_warm_token not in block
    assert _contrast_ratio("#f5f5f5", "#080808") >= 4.5
    assert _contrast_ratio("#a3a3a3", "#080808") >= 4.5
    assert _contrast_ratio("#071522", "#62abf5") >= 4.5


def test_cinema_shell_controls_share_the_same_cool_surface_hierarchy():
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")
    command_block = _css_block_after(
        css,
        "Fluent command surface polish for category pages",
        ":root[data-theme='cinema']",
    )
    sidebar_block = _css_block_after(
        css,
        "Sidebar performance and Fluent/Mica harmony - 2026-07-08",
        ":root[data-theme='cinema']",
    )

    assert "--command-surface: rgba(31, 31, 31, .78)" in command_block
    assert "--command-muted: #a3a3a3" in command_block
    assert "--sidebar-material-a: rgba(20, 20, 20, .96)" in sidebar_block
    assert "--sidebar-material-c: rgba(71, 158, 245, .08)" in sidebar_block
    assert "--sidebar-accent-rail: #479ef5" in sidebar_block
    assert "166, 127, 94" not in command_block + sidebar_block


def test_cinema_detail_material_uses_neutral_glass_and_blue_chroma_without_changing_layout():
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")
    detail_block = _css_block_after(
        css,
        "Immersive Fluent detail layout - theme-aware rebuild from references",
        ":root[data-theme='cinema']",
    )

    assert "--detail-page-bg: #0a0a0a" in detail_block
    assert "--detail-stage-bg: #141414" in detail_block
    assert "--detail-ink: #f5f5f5" in detail_block
    assert "--detail-glass-strong: rgba(31, 31, 31, .72)" in detail_block
    assert "--detail-drawer-chroma-a: rgba(71, 158, 245, .16)" in detail_block
    assert "166, 127, 94" not in detail_block


def test_cinema_supporting_materials_do_not_reintroduce_amber_tints():
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")
    fallback_block = _css_block_after(
        css,
        "Theme fallback used only behind missing/transparent artwork",
        ":root[data-theme='cinema']",
    )
    scrollbar_block = _css_block_after(
        css,
        "Desktop overlay scrollbar - Fluent / Mica",
        ":root[data-theme='cinema']",
    )

    assert "--detail-page-fallback: #141414" in fallback_block
    assert "--scrollbar-thumb: rgba(173, 173, 173, .32)" in scrollbar_block
    assert "--scrollbar-thumb-hover: rgba(71, 158, 245, .66)" in scrollbar_block
    assert "190, 157, 126" not in scrollbar_block


def test_cinema_fluent_controls_and_boot_frame_use_the_project_palette():
    theme = (ROOT / "src/design/fluentTheme.ts").read_text(encoding="utf-8")
    document = (ROOT / "index.html").read_text(encoding="utf-8")

    assert "const cinemaTheme: Theme" in theme
    assert "colorNeutralBackground1: '#292929'" in theme
    assert "colorNeutralForeground1: '#f5f5f5'" in theme
    assert "colorBrandBackground: '#115ea3'" in theme
    assert "colorNeutralForegroundOnBrand: '#ffffff'" in theme
    assert "appearanceMode === 'cinema' ? cinemaTheme : webLightTheme" in theme
    assert "--kumi-boot-bg: #0a0a0a" in document
    assert "213 161 111" not in document
