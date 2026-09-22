"""kumiplayer_anime4k.lua 链映射验证：所有模式×质量组合引用的 shader 必须随包存在。"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LUA_PATH = PROJECT_ROOT / "resources/mpv-runtime/kumiplayer/scripts/kumiplayer_anime4k.lua"
SHADER_DIR = PROJECT_ROOT / "resources/mpv-runtime/portable_config/shaders/anime4k-v4.0.1"

MODES = ("a", "b", "c", "a+a", "b+b", "c+a")
QUALITIES = {"light": ("M", "S"), "balanced": ("L", "M"), "high": ("VL", "M")}


def strip_lua_comments(text: str) -> str:
    """剥离 Lua 行注释与块注释。

    契约断言只看可执行代码：注释里可能为了解释踩过的坑而提到被禁用的 API
    （例如 `mp.get_opt`），不应据此判定违规。
    """
    without_block = re.sub(r"--\[\[.*?\]\]", "", text, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in without_block.splitlines())


def _build_chain(mode: str, quality: str) -> list[str]:
    """与 kumiplayer_anime4k.lua 的 build_chain 逻辑保持一致的镜像实现。"""
    first, second = QUALITIES[quality]
    chain = ["Anime4K_Clamp_Highlights.glsl"]
    if mode in ("a", "b"):
        prefix = "Anime4K_Restore_CNN_Soft_" if mode == "b" else "Anime4K_Restore_CNN_"
        chain += [
            prefix + first + ".glsl",
            "Anime4K_Upscale_CNN_x2_" + first + ".glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Upscale_CNN_x2_" + second + ".glsl",
        ]
    elif mode in ("a+a", "b+b"):
        prefix = "Anime4K_Restore_CNN_Soft_" if mode == "b+b" else "Anime4K_Restore_CNN_"
        chain += [
            prefix + first + ".glsl",
            "Anime4K_Upscale_CNN_x2_" + first + ".glsl",
            prefix + second + ".glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Upscale_CNN_x2_" + second + ".glsl",
        ]
    elif mode == "c":
        chain += [
            "Anime4K_Upscale_Denoise_CNN_x2_" + first + ".glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Upscale_CNN_x2_" + second + ".glsl",
        ]
    elif mode == "c+a":
        chain += [
            "Anime4K_Upscale_Denoise_CNN_x2_" + first + ".glsl",
            "Anime4K_AutoDownscalePre_x2.glsl",
            "Anime4K_AutoDownscalePre_x4.glsl",
            "Anime4K_Restore_CNN_" + second + ".glsl",
            "Anime4K_Upscale_CNN_x2_" + second + ".glsl",
        ]
    return chain


def test_anime4k_lua_file_exists():
    assert LUA_PATH.is_file(), "kumiplayer_anime4k.lua 缺失"


def test_anime4k_all_mode_quality_combinations_reference_bundled_shaders():
    """6 模式 × 3 质量 = 18 种链，所有引用的 shader 必须随包存在。"""
    existing = {f.name for f in SHADER_DIR.iterdir() if f.is_file()}
    assert "LICENSE" in existing
    for mode in MODES:
        for quality in QUALITIES:
            chain = _build_chain(mode, quality)
            missing = [name for name in chain if name not in existing]
            assert not missing, f"{mode} x {quality} 引用的 shader 缺失: {missing}"


def test_anime4k_lua_chain_matches_official_structure():
    """链长度与官方模板一致：A/B=6，C=5，A+A/B+B=7，C+A=6。"""
    expected_lengths = {
        "a": 6, "b": 6, "c": 5,
        "a+a": 7, "b+b": 7, "c+a": 6,
    }
    for mode in MODES:
        chain = _build_chain(mode, "balanced")
        assert len(chain) == expected_lengths[mode], f"{mode} 链长度 {len(chain)} != {expected_lengths[mode]}"


def test_anime4k_lua_declares_script_message_contract():
    """脚本消息契约与施工说明一致。"""
    text = LUA_PATH.read_text(encoding="utf-8")
    for contract in ("set-session", "clear-session", "set-default", "get-state"):
        assert contract in text, f"缺少脚本消息 {contract}"
    for mode in MODES:
        assert mode in text, f"缺少模式 {mode}"
    for quality in QUALITIES:
        assert quality in text, f"缺少质量 {quality}"


def test_anime4k_lua_reads_defaults_through_mp_options():
    """永久默认值必须经 mp.options.read_options 读取，且键前缀带短横。

    两个已实测的坑：
    1. ``mp.get_opt("default_mode")`` 只做全键直查（mpv 的 defaults.lua 实现为
       ``opts[key]``），裸键永远返回 nil，脚本会一直停在硬编码的 off/balanced；
    2. mp.options 按 ``identifier.."-"`` 匹配键名，所以后端注入必须是
       ``--script-opt=kumiplayer_anime4k-default_mode=...``（短横），点号形式无效。

    这两点合起来就是「播放器调节页保存的 Anime4K 默认效果不生效」的根因。
    """
    text = LUA_PATH.read_text(encoding="utf-8")
    code = strip_lua_comments(text)
    assert "read_options" in code, "必须用 mp.options.read_options 读取脚本选项"
    assert '"kumiplayer_anime4k"' in code, "read_options 的 identifier 必须是 kumiplayer_anime4k"
    assert "mp.get_opt(" not in code, (
        "不得使用 mp.get_opt：它是全键直查，裸键取不到值（改用 mp.options.read_options）"
    )
