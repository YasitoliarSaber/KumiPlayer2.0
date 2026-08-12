# -*- coding: utf-8 -*-
"""宽屏设置大纲应在右侧留白内居中。"""

from pathlib import Path


def test_wide_settings_outline_is_centered_in_its_side_column():
    css = (Path(__file__).resolve().parents[2] / "src" / "index.css").read_text(encoding="utf-8")

    assert "Wide settings outline centering" in css
    assert "justify-self: center !important" in css
    assert "top: clamp(144px, calc(50vh - 200px), 420px) !important" in css
    assert "width: min(100%, 320px) !important" in css
