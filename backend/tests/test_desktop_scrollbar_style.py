# -*- coding: utf-8 -*-
"""桌面端主滚动条的视觉回归检查。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_main_scrollbar_uses_themed_fluent_overlay_style():
    css = (ROOT / "src" / "index.css").read_text(encoding="utf-8")

    assert "Desktop overlay scrollbar - Fluent / Mica" in css
    assert ".app-main::-webkit-scrollbar" in css
    assert "background: transparent" in css
    assert "scrollbar-width: thin" in css
    assert "--scrollbar-thumb" in css
    assert ":root[data-theme='fluent']" in css
    assert ":root[data-theme='cinema']" in css
    assert ":root[data-theme='mica']" in css
