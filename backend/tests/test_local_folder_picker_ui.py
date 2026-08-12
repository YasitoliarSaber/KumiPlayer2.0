# -*- coding: utf-8 -*-
"""本地导入应优先使用桌面原生文件夹选择器。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_local_import_exposes_native_folder_picker_with_manual_fallback():
    """本地导入入口在 MediaManagementPage，不在 SettingsPage。

    docs/PROJECT.md 第 8 节把导入流程归给 MediaManagementPage；SettingsPage 只配置
    local_root 这一项路径，本身不发起扫描。原生选择器与手动兜底两条路径都必须在导入页成立。
    """
    management = (ROOT / "src" / "pages" / "MediaManagementPage.tsx").read_text(encoding="utf-8")
    picker = (ROOT / "src" / "platform" / "folderPicker.ts").read_text(encoding="utf-8")

    assert "选择文件夹" in picker  # pickFolder 的默认对话框标题
    assert "directory: true" in picker
    assert "@tauri-apps/plugin-dialog" in picker
    assert "if (!isTauri()) return null;" in picker  # 非桌面环境兜底：不抛错，保留手动输入
    assert "pickFolder" in management
    assert "选择目录" in management
    assert "value={entry.path}" in management  # 浏览器开发模式仍允许手动输入
