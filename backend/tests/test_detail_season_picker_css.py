from pathlib import Path


def test_episode_section_does_not_paint_contain_season_picker_menu():
    """季度菜单向上展开，父容器不能用 paint containment 将其裁切掉。"""
    css = (Path(__file__).resolve().parents[2] / "src" / "index.css").read_text(encoding="utf-8")
    section_start = css.index(".detail-episode-section {\n  grid-column: 1 / -1")
    section_end = css.index("}\n", section_start)
    section_rules = css[section_start:section_end]

    assert "contain: layout paint style" not in section_rules


def test_season_picker_matches_detail_command_menu_typography_and_alignment():
    css = (Path(__file__).resolve().parents[2] / "src" / "index.css").read_text(encoding="utf-8")

    listbox = css.rsplit(".detail-season-listbox {", 1)[1].split("}", 1)[0]
    option = css.rsplit(".detail-season-listbox [role='option'] {", 1)[1].split("}", 1)[0]

    assert "background: var(--detail-command-flyout) !important;" in listbox
    assert "color: var(--detail-command-fg) !important;" in listbox
    assert "box-shadow: var(--detail-shadow);" in listbox
    assert "display: flex;" in option
    assert "align-items: center;" in option
    assert "font: inherit;" in option
    assert "font-size: var(--detail-command-font-size);" in option
    assert "font-weight: var(--detail-command-font-weight);" in option
    assert "line-height: var(--detail-command-line-height);" in option


def test_season_picker_sits_beside_episode_heading_instead_of_inside_pager():
    page = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "pages"
        / "WorkDetailPage.tsx"
    ).read_text(encoding="utf-8")

    heading_start = page.index('<div className="detail-episode-heading">')
    heading_end = page.index("</div>", heading_start)
    heading = page[heading_start:heading_end]
    toolbar_start = page.index(
        '<div className="detail-episode-toolbar"', heading_end
    )

    assert "<h2" in heading
    assert "<DetailSeasonPicker" in heading
    assert heading_start < page.index("<DetailSeasonPicker") < toolbar_start


def test_season_picker_trigger_and_options_share_the_same_font_metrics():
    css = (Path(__file__).resolve().parents[2] / "src" / "index.css").read_text(
        encoding="utf-8"
    )

    outer = css.rsplit(
        ".detail-page.detail-classic-page .detail-season-dropdown {", 1
    )[1].split("}", 1)[0]
    trigger = css.rsplit(
        ".detail-page.detail-classic-page .detail-season-dropdown > .detail-season-trigger {", 1
    )[1].split("}", 1)[0]
    option = css.rsplit(
        ".detail-season-listbox [role='option'] {", 1
    )[1].split("}", 1)[0]
    label = css.rsplit(".detail-season-label {", 1)[1].split("}", 1)[0]
    trigger_label = css.rsplit(".detail-season-trigger-label {", 1)[1].split("}", 1)[0]
    option_label = css.rsplit(".detail-season-option-label {", 1)[1].split("}", 1)[0]

    assert "font: inherit;" in trigger
    assert "font: inherit;" in option
    for rule in (outer, label):
        assert "font-family: var(--detail-command-font-family)" in rule
        assert "font-size: var(--detail-command-font-size)" in rule
        assert "font-weight: var(--detail-command-font-weight)" in rule
        assert "line-height: var(--detail-command-line-height)" in rule
    for rule in (trigger_label, option_label):
        assert "font-size:" not in rule
        assert "font-weight:" not in rule
        assert "line-height:" not in rule
