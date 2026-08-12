"""Anime4K 右键菜单（kumiplayer_uosc_menu.lua）与 input.conf 绑定验证。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MENU_LUA = PROJECT_ROOT / "resources/mpv-runtime/kumiplayer/scripts/kumiplayer_uosc_menu.lua"
ANIME4K_LUA = PROJECT_ROOT / "resources/mpv-runtime/kumiplayer/scripts/kumiplayer_anime4k.lua"
INPUT_CONF = PROJECT_ROOT / "resources/mpv-runtime/portable_config/input.conf"
UOSC_CONF = PROJECT_ROOT / "resources/mpv-runtime/portable_config/script-opts/uosc.conf"
MPV_CONF = PROJECT_ROOT / "resources/mpv-runtime/portable_config/mpv.conf"


def test_menu_lua_exists_and_declares_open_entry():
    assert MENU_LUA.is_file()
    text = MENU_LUA.read_text(encoding="utf-8")
    assert "open-anime4k-menu" in text
    assert "open-menu" in text  # uosc 公开接口


def test_menu_lua_uses_uosc_callback_mode():
    text = MENU_LUA.read_text(encoding="utf-8")
    # 必须通过 uosc 的 open-menu 交互；JSON 顶层必须带 callback 数组进入
    # uosc 回调模式。否则 uosc 会把菜单项的 value（如 "mode:a"）当作 mpv
    # 命令执行，导致点击菜单项无任何反应（历史回归点）。
    assert "script-message-to" in text
    assert "script-message-to\", \"uosc\", \"open-menu" in text
    assert 'callback = { script_name, "menu-event" }' in text
    assert 'register_script_message("menu-event"' in text
    # 不得包含危险命令
    for danger in ("loadfile", "loadlist", "playlist-remove", "quit"):
        assert f'"{danger}"' not in text, f"菜单不应包含危险命令 {danger}"
    # 旧的按 script-message 直发模式已移除：callback 字段不再出现在菜单项里
    assert '"callback"' not in text


def test_menu_click_flow_forwards_value_to_anime4k_session():
    text = MENU_LUA.read_text(encoding="utf-8")
    # 回调模式事件契约：activate 事件按 value 前缀转发 set-session
    assert 'event.type ~= "activate"' in text
    assert 'value:sub(1, 5) == "mode:"' in text
    assert 'value:sub(1, 8) == "quality:"' in text
    assert '"set-session", mode, state.quality' in text
    assert '"set-session", state.mode, quality' in text
    # 激活后关闭菜单（主菜单类型为 kumiplayer-context）
    assert '"close-menu", "kumiplayer-context"' in text
    # 新增通用控制前缀
    assert 'value:sub(1, 6) == "speed:"' in text
    assert 'value:sub(1, 12) == "quality:add:"' in text
    assert 'value:sub(1, 4) == "cmd:"' in text


def test_menu_contains_only_compact_mode_and_quality_submenus():
    text = MENU_LUA.read_text(encoding="utf-8")
    # 右键菜单仅保留官方模式原名与三档质量，不能再塞入说明页、性能页或长提示。
    for title in (
        "Anime4K Mode A",
        "Anime4K Mode B",
        "Anime4K Mode C",
        "Anime4K Mode A+A",
        "Anime4K Mode B+B",
        "Anime4K Mode C+A",
    ):
        assert title in text
    for quality in ("light", "balanced", "high"):
        assert f'"{quality}"' in text
    for removed_entry in (
        "build_info_items",
        "open-mode-info",
        "open-quality-info",
        "open-enhanced-info",
        "open-performance-info",
        "性能检查说明",
        "模式与质量说明",
        "当前：",
    ):
        assert removed_entry not in text
    assert "hint =" not in text


def test_menu_does_not_render_extra_scale_hint_copy():
    text = MENU_LUA.read_text(encoding="utf-8")
    assert "below_2x" not in text
    assert "至少 2x" not in text


def test_uosc_timeline_is_a_high_contrast_bar_visible_in_fullscreen():
    text = UOSC_CONF.read_text(encoding="utf-8")
    assert "timeline_style=bar" in text
    assert "timeline_size=18" in text
    assert "progress=always" in text
    assert "progress_size=3" in text
    assert "progress_line_width=20" in text


def test_mpv_uses_safe_hardware_decode_fallback():
    text = MPV_CONF.read_text(encoding="utf-8")
    assert "hwdec=auto-safe" in text


def test_input_conf_binds_right_click_to_anime4k_menu():
    """右键菜单入口由 KumiPlayer 自有层 bindings.lua 弱绑定提供（分层架构）。"""
    from pathlib import Path as _Path
    bindings = _Path(__file__).resolve().parents[2] / "resources/mpv-runtime/kumiplayer/scripts/kumiplayer_bindings.lua"
    text = bindings.read_text(encoding="utf-8")
    assert "MBTN_RIGHT" in text
    assert "open-anime4k-menu" in text
    # 不得有绕过后端的入口
    for danger in ("loadfile", "loadlist", "playlist-remove"):
        assert danger not in text


def test_anime4k_lua_contracts_match_menu_usage():
    anime4k = ANIME4K_LUA.read_text(encoding="utf-8")
    menu = MENU_LUA.read_text(encoding="utf-8")
    # 菜单只使用临时会话契约；“关闭”也必须是当前视频的临时 off，
    # 不能清空覆盖后回退到永久默认；set-default 属于后端 IPC，不暴露在右键菜单中。
    for contract in ("set-session", "get-state"):
        assert contract in anime4k
        assert contract in menu
    assert "clear-session" not in menu
    assert "set-default" in anime4k
    assert "set-default" not in menu  # 右键临时状态不得写回永久配置


def test_anime4k_state_message_name_matches_broadcast():
    """发送方与监听方的状态消息名必须完全一致。

    右键菜单通过 get-state 请求后等待状态广播；若监听名与 Anime4K 脚本的
    广播名不一致（如 anime4k-state vs kumiplayer_anime4k-state），菜单会
    永远等不到回调、右键无反应。故意改任一端消息名都会让本测试失败。
    """
    anime4k = ANIME4K_LUA.read_text(encoding="utf-8")
    menu = MENU_LUA.read_text(encoding="utf-8")

    # 发送方：Anime4K 脚本广播状态
    broadcast_matches = [
        line for line in anime4k.splitlines()
        if "kumiplayer_anime4k-state" in line
    ]
    assert broadcast_matches, "Anime4K 脚本缺少 kumiplayer_anime4k-state 广播"

    # 监听方：菜单脚本注册同名消息
    listen_matches = [
        line for line in menu.splitlines()
        if "register_script_message" in line and "kumiplayer_anime4k-state" in line
    ]
    assert listen_matches, "菜单脚本未监听 kumiplayer_anime4k-state"

    # 不允许出现旧的不一致消息名
    assert "anime4k-state\"" not in menu.replace("kumiplayer_anime4k-state", "")


def test_uosc_window_scale_and_language():
    """窗口模式时间线可交互：scale 必须是 1（非全屏系数），语言用简体中文。"""
    text = UOSC_CONF.read_text(encoding="utf-8")
    assert "scale=1" in text
    assert "scale_fullscreen=1" in text
    assert "languages=zh-hans,en" in text
    # 时间线缓存与条形进度保留
    assert "timeline_cache=yes" in text
    assert "progress=always" in text


def test_menu_extends_common_controls():
    """右键菜单已扩展为 KumiPlayer 通用控制菜单（方案 C：uosc 能力）。

    主菜单必须包含播放控制、速度、画质、字幕、音轨与 Anime4K 入口，
    全部基于 mpv 内置命令，不引入额外插件或绕过受控队列的入口。
    """
    text = MENU_LUA.read_text(encoding="utf-8")
    # 主菜单类型
    assert 'type = "kumiplayer-context"' in text
    assert 'title = "KumiPlayer"' in text
    # 精简后：播放控制/全屏/截图/控制台/退出已移除
    for removed in ('cmd:cycle pause', 'cmd:playlist-prev', 'cmd:playlist-next',
                    'cmd:cycle fullscreen', 'cmd:screenshot video',
                    'cmd:script-binding console/enable', 'cmd:quit'):
        assert removed not in text, f"菜单不应包含已移除项: {removed}"
    # 速度/画质/字幕/音轨子菜单
    assert "build_speed_items" in text
    assert "build_quality_items" in text
    assert "build_subtitle_items" in text
    assert "build_audio_items" in text
    # 值前缀分发
    assert 'value:sub(1, 6) == "speed:"' in text
    assert 'value:sub(1, 12) == "quality:add:"' in text
    assert 'value == "quality:reset"' in text
    assert 'value:sub(1, 4) == "cmd:"' in text
    # Anime4K 模式/质量直接平铺为主菜单（不再嵌套子菜单，缩短展开路径）
    assert '"submenu:anime4k-modes"' in text
    assert '"submenu:anime4k-quality"' in text
    assert '"submenu:image-quality"' in text  # 画质调节使用独立子菜单 id，避免与 Anime4K 质量冲突
    assert "build_mode_items" in text
    assert "build_quality_items" in text
    # 修复回归：不得复用 submenu:quality 作为两个不同子菜单的 id
    assert text.count('"submenu:quality"') == 0
    # 无额外插件依赖：不出现对未接入脚本的 script-binding 调用
    for absent in ("input_plus", "shaders", "vapoursynth", "nvidia"):
        assert absent not in text, f"菜单不应依赖未接入脚本: {absent}"
