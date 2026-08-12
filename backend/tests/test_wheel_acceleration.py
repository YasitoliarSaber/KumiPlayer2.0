# -*- coding: utf-8 -*-
"""桌面内容区必须使用 WebView 原生合成滚动。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_app_shell_keeps_the_main_scroller_native():
    shell = (ROOT / "src" / "components" / "shell" / "AppShell.tsx").read_text(encoding="utf-8")

    assert "useSmoothWheelScroll" not in shell
    assert "onWheel=" not in shell


def test_legacy_main_thread_wheel_animation_is_removed():
    hook = ROOT / "src" / "hooks" / "useSmoothWheelScroll.ts"

    assert not hook.exists()


def test_detail_page_does_not_own_a_second_wheel_handler():
    detail = (ROOT / "src" / "pages" / "WorkDetailPage.tsx").read_text(encoding="utf-8")

    assert "addEventListener('wheel'" not in detail
    assert "onWheel=" not in detail
