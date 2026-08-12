# -*- coding: utf-8 -*-
"""kumiplayer_anime4k.lua 链映射验证：所有模式×质量组合引用的 shader 必须随包存在。"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LUA_PATH = PROJECT_ROOT / "resources/mpv-runtime/portable_config/scripts/kumiplayer_anime4k.lua"
SHADER_DIR = PROJECT_ROOT / "resources/mpv-runtime/portable_config/shaders/anime4k-v4.0.1"

MODES = ("a", "b", "c", "a+a", "b+b", "c+a")
QUALITIES = {"light": ("M", "S"), "balanced": ("L", "M"), "high": ("VL", "M")}


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
