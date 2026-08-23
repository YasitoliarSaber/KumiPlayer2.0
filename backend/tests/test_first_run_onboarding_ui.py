from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_app_gates_normal_shell_behind_first_run_setup():
    app = (ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "FirstRunSetup" in app
    assert "setup_completed" in app


def test_first_run_page_contains_required_zero_start_steps():
    page = (ROOT / "src" / "pages" / "FirstRunSetup.tsx").read_text(encoding="utf-8")
    for text in ["内置播放器", "镜像目录", "媒体来源", "验证并完成", "SourceEvidence"]:
        assert text in page
    # 首次引导不再要求用户选择外部 MPV 路径，改为自动检测内置播放器
    assert "pickFolder" in page
    assert "getMpvRuntime" in page
    assert "mpv_path" not in page


def test_first_run_no_longer_asks_user_to_pick_external_mpv():
    page = (ROOT / "src" / "pages" / "FirstRunSetup.tsx").read_text(encoding="utf-8")
    assert "选择 MPV" not in page
    assert "安装包不包含 mpv.exe" not in page
    assert "请选择 mpv.exe" not in page
    assert "使用你选择的 MPV" not in page


def test_first_run_explains_and_links_official_credential_types():
    page = (ROOT / "src" / "pages" / "FirstRunSetup.tsx").read_text(encoding="utf-8")
    credentials = (ROOT / "src" / "config" / "credentials.ts").read_text(encoding="utf-8")

    assert "TMDB API 读取访问令牌" in page
    assert "不是下方的 API 密钥" in page
    assert "Bangumi 个人访问令牌" in page
    assert "bangumi_access_token" in page
    assert "https://www.themoviedb.org/settings/api" in credentials
    assert "https://next.bgm.tv/demo/access-token" in credentials


def test_first_run_reflects_v4_source_flow_without_runtime_metadata():
    page = (ROOT / "src" / "pages" / "FirstRunSetup.tsx").read_text(encoding="utf-8")

    assert "OpenList 可在完成后添加" in page
    assert "并不替代首次配置的可访问媒体根目录" in page
    assert "mpvStatus.version" not in page
    assert "mpvStatus.architecture" not in page
    assert "distribution_status" not in page


def test_settings_can_reenter_setup_without_resetting_first_run_state():
    app = (ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
    settings = (ROOT / "src" / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    setup = (ROOT / "src" / "pages" / "FirstRunSetup.tsx").read_text(encoding="utf-8")

    assert "setupOverride" in app
    assert "onOpenSetup" in app
    assert '"reconfigure"' in app
    assert "重新进入初始引导" in settings
    assert "不会清空现有配置" in settings
    assert "onCancel" in setup
    assert "退出引导" in setup


def test_settings_omits_support_and_build_metadata_from_regular_preferences():
    settings = (ROOT / "src" / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")

    assert "应用与支持" not in settings
    assert "支持与赞助" not in settings
    assert "KumiPlayer 构建标识" not in settings
    assert "__BUILD_SHA__" not in settings
