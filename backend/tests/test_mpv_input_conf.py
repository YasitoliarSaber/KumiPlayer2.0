"""KumiPlayer 快捷键契约验证（分层架构，2026-08-12）。

覆盖：
- 可替换层 input.conf：通用播放键（SPACE/方向键/音量/画质等）；
- KumiPlayer 自有层 kumiplayer_bindings.lua：专属快捷键（MBTN_RIGHT 右键菜单、
  TAB 统计、` 控制台、F10/Alt+F10 截图）与 Ctrl+v 安全屏蔽（强绑定）；
- 不引入 loadfile / loadlist / playlist-remove 等绕过后端受控队列的命令。
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_CONF = PROJECT_ROOT / "resources/mpv-runtime/portable_config/input.conf"
BINDINGS_LUA = PROJECT_ROOT / "resources/mpv-runtime/kumiplayer/scripts/kumiplayer_bindings.lua"


def test_input_conf_exists_and_readable():
    assert INPUT_CONF.is_file()
    text = INPUT_CONF.read_text(encoding="utf-8")
    assert text.strip()


def test_bindings_lua_registers_kumiplayer_keys():
    """KumiPlayer 专属快捷键由自有层弱绑定提供（整合包 input.conf 同名键优先）。"""
    text = BINDINGS_LUA.read_text(encoding="utf-8")
    # 右键菜单入口
    assert 'add_key_binding("MBTN_RIGHT"' in text
    assert "open-anime4k-menu" in text
    # 统计/控制台
    assert 'add_key_binding("TAB"' in text
    assert 'add_key_binding("`"' in text
    # 截图
    assert 'add_key_binding("F10"' in text
    assert 'add_key_binding("Alt+F10"' in text


def test_clipboard_load_is_disabled_with_forced_binding():
    """Ctrl+v 剪贴板加载屏蔽：必须使用强绑定（add_forced_key_binding），
    保证受控队列安全边界不被整合包 input.conf 覆盖。"""
    text = BINDINGS_LUA.read_text(encoding="utf-8")
    assert 'add_forced_key_binding("Ctrl+v"' in text
    # 不得使用会被 input.conf 覆盖的弱绑定
    assert 'add_key_binding("Ctrl+v"' not in text


def test_no_dangerous_bypass_commands():
    """绑定行不得包含绕过后端受控队列的命令（注释除外）。"""
    for path in (INPUT_CONF, BINDINGS_LUA):
        text = path.read_text(encoding="utf-8")
        binding_lines = [
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for danger in ("loadfile", "loadlist", "playlist-remove", "playlist-shuffle"):
            for line in binding_lines:
                assert danger not in line, f"{path.name} 绑定行不应包含危险命令 {danger}: {line}"


def test_default_key_hints_documented():
    """默认套件保留通用播放键位。"""
    text = INPUT_CONF.read_text(encoding="utf-8")
    for marker in (
        "MBTN_LEFT",
        "SPACE",
        "LEFT",
        "RIGHT",
        "WHEEL_UP",
        "f",
        "F11",
        "F12",
        "BS",
        "add contrast",
        "add hue",
    ):
        assert marker in text, f"缺少通用播放键位: {marker}"


def test_mpv_conf_disables_builtin_bindings():
    """默认套件 mpv.conf 禁用二进制内置键位（可替换层默认方案）。"""
    mpv_conf = PROJECT_ROOT / "resources/mpv-runtime/portable_config/mpv.conf"
    text = mpv_conf.read_text(encoding="utf-8")
    assert "input-builtin-bindings=no" in text
    # 绝不能使用会连脚本弱绑定一起屏蔽的选项（只查实际配置行，注释警告允许出现）
    config_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for line in config_lines:
        assert "input-default-bindings=no" not in line, f"禁止使用: {line}"


def test_kumiplayer_forced_config_exists():
    """自有层强制配置（--include 追加）必须存在且含应用必需项。"""
    forced = PROJECT_ROOT / "resources/mpv-runtime/kumiplayer/mpv.conf"
    assert forced.is_file()
    text = forced.read_text(encoding="utf-8")
    assert "hwdec=auto-safe" in text
    assert "load-stats-overlay=no" in text
    # 禁用 mpv 原生 OSD 进度条（滚轮 seek 不弹粗条，进度由 uosc 时间轴提供）
    assert "osd-bar=no" in text
    assert "osd-on-seek=no" in text
