# -*- coding: utf-8 -*-
"""KumiPlayer 品牌资源及桌面壳接入回归。"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_brand_assets_share_the_approved_navy_coral_geometry():
    app_icon = (ROOT / "public" / "brand" / "kumiplayer-app-icon.svg").read_text(encoding="utf-8")
    mark = (ROOT / "public" / "brand" / "kumiplayer-mark.svg").read_text(encoding="utf-8")

    for asset in (app_icon, mark):
        assert "#1a1a1a" in asset
        assert "linearGradient" not in asset
        assert "aria-label=\"KumiPlayer\"" in asset
    assert "#ffffff" in app_icon.lower()


def test_shell_uses_the_brand_once_in_the_titlebar():
    titlebar = (ROOT / "src" / "components" / "shell" / "DesktopTitleBar.tsx").read_text(encoding="utf-8")
    sidebar = (ROOT / "src" / "components" / "shell" / "Sidebar.tsx").read_text(encoding="utf-8")

    assert "/brand/kumiplayer-app-icon.svg" in titlebar
    assert "desktop-titlebar-wordmark" in titlebar
    assert "/brand/kumiplayer-app-icon.svg" not in sidebar
    assert "sidebar-home-brand" not in sidebar


def test_tauri_uses_generated_multisize_app_icons():
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    icons = config["bundle"]["icon"]

    assert "icons/icon.ico" in icons
    assert "icons/32x32.png" in icons
    assert (ROOT / "src-tauri" / "icons" / "icon.ico").stat().st_size > 1000
    assert (ROOT / "src-tauri" / "icons" / "128x128.png").stat().st_size > 1000


def test_development_release_build_skips_installers():
    script = (ROOT / "build_tauri.bat").read_text(encoding="utf-8")

    assert "--no-bundle" in script
    assert "Raw executable only; MSI and NSIS installers are skipped." in script
