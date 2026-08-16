# -*- coding: utf-8 -*-
"""设置页大纲点击应直接定位，不能沿途激活中间菜单。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_settings_outline_click_locks_target_and_disables_smooth_scroll():
    page = (ROOT / "src/pages/SettingsPage.tsx").read_text(encoding="utf-8")

    assert "navigationTargetRef" in page
    assert "if (navigationTargetRef.current) return;" in page
    assert "root.style.scrollBehavior = 'auto';" in page
    assert "scrollIntoView({ behavior: 'auto', block: 'start' })" in page
    assert "scrollIntoView({ behavior: 'smooth'" not in page


def test_connection_essentials_are_visible_and_misleading_options_are_hidden():
    """OL-4 新 IA：元数据与图片页保留 TMDB 凭据；网络代理移到应用与支持。

    - 常用连接配置含 API 读取访问令牌与测试 TMDB 连接；
    - 代理不再与 TMDB 混排（移到 support 的网络区块）；
    - 设置页不出现 DeepSeek 与旧“连接高级设置”。
    """
    page = (ROOT / "src/pages/SettingsPage.tsx").read_text(encoding="utf-8")

    essentials_start = page.index('title="常用连接配置"')
    advanced_start = page.index('title="高级参数" collapsible')

    assert essentials_start < advanced_start
    essentials = page[essentials_start:advanced_start]
    assert 'label="后端端口"' not in essentials
    assert 'label="API 读取访问令牌"' in essentials
    assert "TMDB_API_SETTINGS_URL" in essentials
    assert "不是 API 密钥" in essentials
    assert "测试 TMDB 连接" in essentials
    # 代理已从元数据区移出（移到 support 网络区块）
    assert 'label="网络代理"' not in essentials
    assert 'title="连接高级设置"' not in page
    assert "DeepSeek" not in page


def test_proxy_moved_to_support_network_section():
    """OL-4：网络代理属于应用级网络行为，位于「应用与支持 → 网络」。"""
    page = (ROOT / "src/pages/SettingsPage.tsx").read_text(encoding="utf-8")

    support_start = page.index('title="网络"')
    assert 'label="网络代理"' in page
    proxy_pos = page.index('label="网络代理"')
    assert support_start < proxy_pos

def test_secret_config_inputs_do_not_reuse_masked_values_as_new_credentials():
    page = (ROOT / "src/pages/SettingsPage.tsx").read_text(encoding="utf-8")

    assert "const initialDraft = secret ? '' : value" in page
    assert "已配置；粘贴新凭据可替换" in page
    assert "disabled={secret && !draft.trim()}" in page


def test_support_page_keeps_only_actionable_cards_and_readme_lists_environment_details():
    page = (ROOT / "src/pages/SettingsPage.tsx").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert 'title="运行环境"' not in page
    assert 'title="账户与网络"' not in page
    assert 'title="初始设置引导"' in page
    assert 'title="支持与赞助"' in page
    assert "## 运行环境与本地配置" in readme
    assert "个人配置仅保存在本机" in readme
