from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_startup_uses_non_blocking_brand_motion_without_visible_config_copy():
    document = (ROOT / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "src/App.tsx").read_text(encoding="utf-8")
    component = (ROOT / "src/components/shell/StartupSplash.tsx").read_text(encoding="utf-8")
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert "正在读取本机配置" not in app
    assert document.index('id="kumi-boot-splash"') < document.index('id="root"')
    assert 'data-kumi-boot-styles' in document
    assert 'aria-hidden="true"' in document
    assert "kumiplayer-ui" in document
    assert "appearanceMode" in document
    assert "motionMode" in document
    assert "/brand/kumiplayer-app-icon.svg" in document
    assert "<StartupSplash />" in app
    assert "useLayoutEffect" in app
    assert "document.getElementById('kumi-boot-splash')?.remove()" in app
    assert 'role="status"' in component
    assert 'aria-label="正在启动 KumiPlayer"' in component
    assert "/brand/kumiplayer-app-icon.svg" in component
    assert "animation-delay: 120ms" not in css
    assert "setTimeout" not in document
    assert "setTimeout" not in component


def test_startup_motion_respects_reduced_motion_preferences():
    document = (ROOT / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")

    assert "@media (prefers-reduced-motion: reduce)" in document
    assert "html[data-motion='reduced']" in document
    assert ":root[data-motion='reduced'] .startup-splash-mark" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_startup_first_frame_matches_all_three_appearance_presets():
    document = (ROOT / "index.html").read_text(encoding="utf-8")

    assert "html[data-theme='fluent']" in document
    assert "html[data-theme='cinema']" in document
    assert "html[data-theme='mica']" in document
    assert "--kumi-boot-bg: #eef3f9" in document
    assert "--kumi-boot-bg: #0a0a0a" in document
    assert "--kumi-boot-bg: #eeeeec" in document
