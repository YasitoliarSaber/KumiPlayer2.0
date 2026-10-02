"""所有页面共享的外壳不再显示滚动进度或返回顶部悬浮控件。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_app_shell_removes_global_back_to_top_control():
    shell = (ROOT / "src/components/shell/AppShell.tsx").read_text(encoding="utf-8")
    assert '<main className="app-main flex-1">' in shell
    assert "ScrollProgressButton" not in shell
    assert not (ROOT / "src/components/shell/ScrollProgressButton.tsx").exists()


def test_removed_control_does_not_leave_floating_styles_or_theme_tokens():
    css = (ROOT / "src/index.css").read_text(encoding="utf-8")
    assert ".scroll-progress-" not in css
    assert "--scroll-progress-" not in css
    # 原共享主题块中的滑块与详情命令层仍保留。
    assert "--range-track:" in css
    assert "--detail-command-surface:" in css
