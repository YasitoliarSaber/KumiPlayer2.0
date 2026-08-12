# -*- coding: utf-8 -*-
"""锁定用户确认的详情页连续背景，防止旧材质规则再次覆盖。"""

import re
from pathlib import Path


CSS_PATH = Path(__file__).resolve().parents[2] / "src" / "index.css"


VISUAL_LOCK_MARKER = "/* Detail page visual lock - user-approved continuous backdrop."


def rule_body(css: str, selector: str) -> str:
    prefix = f"{selector} {{"
    assert prefix in css
    return css.split(prefix, 1)[1].split("}", 1)[0]


def test_themes_only_define_page_fallback_not_drawer_material():
    css = CSS_PATH.read_text(encoding="utf-8")

    for theme in ("fluent", "cinema", "mica"):
        theme_block = css.split(f":root[data-theme='{theme}']", 1)[1]
        assert "--detail-page-fallback:" in theme_block
    assert "--detail-drawer-material-" not in css


def test_detail_page_keeps_fixed_artwork_visible_behind_all_content():
    css = CSS_PATH.read_text(encoding="utf-8")

    assert "var(--detail-backdrop-image)" not in css
    assert ".detail-classic-page .detail-hero-bg { display: none !important; }" not in css
    assert ".detail-page.detail-classic-page .detail-hero-art {\n  opacity: 0 !important;" not in css
    assert VISUAL_LOCK_MARKER in css
    locked_and_tail = css.split(VISUAL_LOCK_MARKER, 1)[1]
    locked_block, tail = locked_and_tail.split("/* Desktop content scroll boundary", 1)
    hero_background = rule_body(locked_block, ".detail-page.detail-classic-page .detail-hero-bg")
    hero_artwork = rule_body(locked_block, ".detail-page.detail-classic-page .detail-hero-art")
    hero = rule_body(locked_block, ".detail-page.detail-classic-page .detail-hero")
    drawer = rule_body(locked_block, ".detail-page.detail-classic-page .detail-content-drawer")
    drawer_overlay = rule_body(locked_block, ".detail-page.detail-classic-page .detail-content-drawer::before")
    for declaration in (
        "display: block !important;",
        "width: 100% !important;",
        "height: 100% !important;",
        "object-fit: cover !important;",
        "opacity: 1 !important;",
    ):
        assert declaration in hero_background
    for declaration in ("display: block !important;", "object-fit: cover !important;", "opacity: 1 !important;"):
        assert declaration in hero_artwork
    assert "height: calc(100vh - 40px) !important;" in hero
    for declaration in ("background: transparent !important;", "box-shadow: none !important;", "backdrop-filter: none !important;"):
        assert declaration in drawer
    assert "content: none !important;" in drawer_overlay
    assert "display: none !important;" in drawer_overlay
    assert ".detail-page.detail-classic-page .detail-content-drawer > section" in locked_block
    assert "padding-top: 0 !important;" not in locked_block
    assert "object-fit: contain !important;" not in locked_block
    for selector in (
        ".detail-page.detail-classic-page .detail-hero-bg",
        ".detail-page.detail-classic-page .detail-hero-art",
        ".detail-page.detail-classic-page .detail-hero {",
        ".detail-page.detail-classic-page .detail-content-drawer {",
    ):
        assert selector not in tail
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", tail):
        if "detail-classic-page" not in selector:
            continue
        if "detail-hero-bg" in selector or "detail-hero-art" in selector:
            assert "display: none" not in body
            assert "opacity: 0" not in body
            assert "object-fit: contain" not in body
        if "detail-content-drawer" in selector:
            for value in re.findall(r"(?:^|;)\s*background\s*:\s*([^;]+)", body):
                assert value.strip().startswith("transparent")
            for value in re.findall(r"(?:^|;)\s*(?:-webkit-)?backdrop-filter\s*:\s*([^;]+)", body):
                assert value.strip().startswith("none")
