"""mpv-stats.lua 中文翻译版集成契约验证。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "resources/mpv-runtime/portable_config/scripts"
STATS_LUA = SCRIPTS_DIR / "stats.lua"
MPV_CONF = PROJECT_ROOT / "resources/mpv-runtime/portable_config/mpv.conf"
STATS_CONF = PROJECT_ROOT / "resources/mpv-runtime/portable_config/script-opts/stats.conf"


def test_stats_zh_installed():
    assert STATS_LUA.is_file()
    text = STATS_LUA.read_text(encoding="utf-8-sig")
    assert text.startswith("-- Display some stats.")  # 保留官方头部
    assert "自动翻译模块" in text or "auto_translate_text" in text
    assert "系统统计" in text  # 中文系统统计模块


def test_stats_zh_registers_default_key_bindings():
    """中文版必须注册 display-stats / display-stats-toggle（i/I 键由 mpv 默认绑定）。"""
    text = STATS_LUA.read_text(encoding="utf-8-sig")
    assert '"display-stats"' in text
    assert '"display-stats-toggle"' in text


def test_mpv_conf_disables_builtin_stats():
    """必须 load-stats-overlay=no，否则内置英文 stats 与中文版同名冲突（stats2）。"""
    text = MPV_CONF.read_text(encoding="utf-8")
    assert "load-stats-overlay=no" in text


def test_stats_conf_documents_source():
    text = STATS_CONF.read_text(encoding="utf-8")
    assert "yosh-wang" in text
    assert "2026-07-12" in text
    assert "TAB" in text  # 键位说明（已从默认 i/I 改为 input.conf 显式 TAB）
    assert "duration=" in text  # 官方选项记录
