# -*- coding: utf-8 -*-
"""全局滚动进度与返回顶部控件的结构和视觉契约。"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_SHELL_PATH = ROOT / "src" / "components" / "shell" / "AppShell.tsx"
COMPONENT_PATH = ROOT / "src" / "components" / "shell" / "ScrollProgressButton.tsx"
CSS_PATH = ROOT / "src" / "index.css"
STYLE_MARKER = "/* Global scroll progress and back-to-top control. */"


def _scroll_progress_layer(css: str) -> str:
    """提取 scroll-progress 模块自身的 CSS 块。

    STYLE_MARKER 后整段 CSS 会延伸到文件末尾，可能包含无关模块（如 .sidebar-rail-toggle、
    .virtual-poster-grid 等）。把 layer 截止到 STYLE_MARKER 之后第一个独立 `/* ... */`
    模块注释开头的位置，避免负向断言误伤其它模块合法的 translateY/translateY(-50%) 等。
    """
    section = css.split(STYLE_MARKER, 1)[1]
    match = re.search(r"\n/\* ", section[1:])
    if match:
        return section[: match.start() + 1]
    return section


def test_app_shell_mounts_scroll_progress_on_the_native_main_scroller():
    shell = APP_SHELL_PATH.read_text(encoding="utf-8")

    assert "const mainRef = useRef<HTMLElement>(null);" in shell
    assert '<main ref={mainRef} className="app-main flex-1">' in shell
    assert "<ScrollProgressButton scrollContainerRef={mainRef} />" in shell


def test_scroll_progress_uses_real_scroll_extent_without_react_frame_renders():
    component = COMPONENT_PATH.read_text(encoding="utf-8")

    assert "scrollHeight - clientHeight" in component
    assert "scrollTop / maxScroll" in component
    assert "addEventListener('scroll', scheduleUpdate, { passive: true })" in component
    assert "new ResizeObserver(scheduleUpdate)" in component
    assert "buttonRef.current" in component
    assert "ringRef.current" in component
    assert "setProgress" not in component
    assert "setVisible" not in component
    assert "addEventListener('wheel'" not in component
    assert "preventDefault" not in component


def test_scroll_progress_button_returns_to_top_and_respects_reduced_motion():
    component = COMPONENT_PATH.read_text(encoding="utf-8")

    assert "ArrowUp" in component
    assert "pathLength=\"100\"" in component
    assert 'ref={ringRef}' in component
    assert 'strokeDashoffset="100"' in component
    assert "document.documentElement.dataset.motion === 'reduced'" in component
    assert "window.matchMedia('(prefers-reduced-motion: reduce)').matches" in component
    assert "scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' })" in component
    assert "返回顶部，当前滚动进度" in component
    assert 'role="progressbar"' in component
    assert "aria-valuemin={0}" in component
    assert "aria-valuemax={100}" in component
    assert 'aria-valuenow={0}' in component
    assert '<svg className="scroll-progress-ring" viewBox="0 0 44 44"' in component
    assert 'r="18"' in component
    # 箭头从 16px 放大到 18px；SVG viewBox/cx/cy/r 保留不变，由 CSS 按比例放大圆环。
    assert '<ArrowUp size={18} strokeWidth={1.8}' in component


def test_scroll_progress_control_uses_theme_aware_acrylic_styling():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert STYLE_MARKER in css
    layer = _scroll_progress_layer(css)

    assert ".scroll-progress-button" in layer
    assert "position: fixed" in layer
    assert "top: auto" in layer
    # 按钮移到左下角：水平位置基于 --sidebar-width 计算，不再使用 right: 32px。
    assert "left: calc(var(--sidebar-width, 56px) + 18px)" in layer
    assert "right: auto" in layer
    # 旧的右下角定位词在此前的 .scroll-progress-button 块里已被新值取代；
    # 不在整层 grep，以避免误伤文件里其他控件的合法 right:32px / bottom:34px。
    button_block = layer.split(".scroll-progress-button {\n", 1)[1].split("}", 1)[0]
    assert "right: 32px" not in button_block
    assert "bottom: 34px" not in button_block
    assert "bottom: 30px" in button_block
    # 按钮 + 圆环从 44px 放大到 52px，提升点击范围。
    assert "width: 52px" in button_block
    assert "height: 52px" in button_block
    assert "width: 44px" not in button_block
    assert "height: 44px" not in button_block
    assert ".scroll-progress-button::before" in layer
    assert "inset: 6px" in layer
    assert "border-radius: 50%" in layer
    assert "backdrop-filter: blur(16px) saturate(1.08)" in layer
    assert "#ff654f" not in layer.lower()
    assert ".scroll-progress-button.is-visible" in layer
    assert ".scroll-progress-ring-value" in layer
    assert "stroke-width: 1" in layer
    assert "stroke-width: 2.25" in layer
    assert "filter: drop-shadow(0 0 4px var(--scroll-progress-ring-glow))" in layer
    assert "translateY(-2px)" in layer
    assert "translateY(-50%)" not in layer
    assert "rgb(31 31 31 / .78)" not in layer
    assert "@media (max-width: 760px)" in layer
    # 小窗口只缩短侧栏间距，不缩小点击区域；同样基于侧栏宽度动态定位。
    media_block = layer.split("@media (max-width: 760px)", 1)[1].split(":root[data-motion='reduced']", 1)[0]
    assert "left: calc(var(--sidebar-width, 56px) + 12px)" in media_block
    assert "right: auto" in media_block
    assert "bottom: 22px" in media_block
    assert "@media (prefers-reduced-motion: reduce)" in layer
    # 进度环与按钮同步放大到 52px，保留现有 viewBox/cx/cy/r 让浏览器按比例放大。
    assert ".scroll-progress-ring {" in layer
    ring_block = layer.split(".scroll-progress-ring {\n", 1)[1].split("}", 1)[0]
    assert "width: 52px" in ring_block
    assert "height: 52px" in ring_block

    expected_rings = {
        "fluent": "rgb(49 59 65 / .68)",
        "cinema": "rgb(255 255 255 / .66)",
        # 纯白主题使用冷中性深灰进度环。
        "mica": "rgb(43 48 53 / .66)",
    }
    for theme in ("fluent", "cinema", "mica"):
        theme_selector = f":root[data-theme='{theme}']"
        assert theme_selector in layer
        theme_rules = layer.split(theme_selector, 1)[1].split("}", 1)[0]
        assert "--scroll-progress-surface:" in theme_rules
        assert "--scroll-progress-border:" in theme_rules
        assert "--scroll-progress-ink:" in theme_rules
        assert "--scroll-progress-surface-hover:" in theme_rules
        assert "--scroll-progress-ring-glow:" in theme_rules
        assert f"--scroll-progress-ring: {expected_rings[theme]}" in theme_rules
