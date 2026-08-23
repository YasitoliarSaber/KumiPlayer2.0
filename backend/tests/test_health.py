# -*- coding: utf-8 -*-
"""健康检查与基础模型测试"""

import sys
from pathlib import Path

# 确保可以导入 app 模块
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_health_endpoint():
    """测试健康检查端点"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["app"] == "KumiPlayer"


def test_health_endpoint_reports_desktop_runtime_identity(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("KUMIPLAYER_RUNTIME_KIND", "bundled")
    monkeypatch.setenv("KUMIPLAYER_RUNTIME_ID", "runtime-a1b2")
    monkeypatch.setenv("KUMIPLAYER_INSTANCE_ID", "instance-c3d4")

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "KumiPlayer",
        "runtime_kind": "bundled",
        "runtime_id": "runtime-a1b2",
        "instance_id": "instance-c3d4",
    }


def test_config_load():
    """测试配置加载"""
    from app.core.config import AppConfig, load_config

    config = load_config()
    assert isinstance(config, AppConfig)
    assert config.tmdb_language == "zh-CN"
    assert config.heartbeat_timeout == 30
    assert config.series_card_image_mode in {"poster", "fanart"}


def test_config_mask():
    """测试敏感字段脱敏"""
    from app.core.config import AppConfig

    config = AppConfig(tmdb_bearer_token="abcdefghijklmnop")
    public = config.to_public_dict()
    assert public["tmdb_bearer_token"] == "abcdefgh..."
    assert "ijklmnop" not in public["tmdb_bearer_token"]


def test_config_mask_short_value():
    """测试短敏感值也脱敏（长度 <= 8 时显示前 4 位 + ...）"""
    from app.core.config import AppConfig

    config = AppConfig(tmdb_bearer_token="short")
    public = config.to_public_dict()
    # 短值：显示前 4 位 + ...
    assert public["tmdb_bearer_token"] == "shor..."
    assert public["tmdb_bearer_token"] != "short"


def test_config_mask_deepseek_key():
    """测试 deepseek_api_key 脱敏"""
    from app.core.config import AppConfig

    config = AppConfig(deepseek_api_key="sk-1234567890abcdef")
    public = config.to_public_dict()
    assert public["deepseek_api_key"] == "sk-12345..."
    assert "567890" not in public["deepseek_api_key"]


def test_config_mask_bangumi_token():
    """测试 bangumi_access_token 脱敏"""
    from app.core.config import AppConfig

    config = AppConfig(bangumi_access_token="bgm-token-abcdef")
    public = config.to_public_dict()
    assert public["bangumi_access_token"] == "bgm-toke..."
    assert "abcdef" not in public["bangumi_access_token"]


def test_config_series_card_image_mode_public():
    """测试系列卡片图片展示偏好公开返回"""
    from app.core.config import AppConfig

    config = AppConfig(series_card_image_mode="fanart")
    public = config.to_public_dict()
    assert public["series_card_image_mode"] == "fanart"


def test_sanitize_filename():
    """测试文件名清洗"""
    from app.core.paths import sanitize_filename

    assert sanitize_filename("正常文件名") == "正常文件名"
    assert sanitize_filename("包含/斜杠") == "包含_斜杠"
    assert sanitize_filename("包含\\反斜杠") == "包含_反斜杠"
    assert sanitize_filename("包含:冒号") == "包含_冒号"
    assert sanitize_filename("包含*星号") == "包含_星号"
    assert sanitize_filename("包含?问号") == "包含_问号"
    assert sanitize_filename('包含"引号') == "包含_引号"
    assert sanitize_filename("包含<小于号>") == "包含_小于号_"
    assert sanitize_filename("  空格  ") == "空格"
    assert sanitize_filename("") == "unnamed"
    assert sanitize_filename("...") == "unnamed"


def test_reject_path_traversal():
    """测试路径遍历检测"""
    from app.core.paths import reject_path_traversal

    # 正常路径
    assert reject_path_traversal("动画/冰菓/video.mkv") == "动画/冰菓/video.mkv"

    # 路径遍历
    try:
        reject_path_traversal("../evil")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass

    try:
        reject_path_traversal("动画/../../../etc/passwd")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass

    # 绝对路径
    try:
        reject_path_traversal("/etc/passwd")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass

    try:
        reject_path_traversal("C:\\Windows\\System32")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass


def test_safe_join():
    """测试安全路径拼接"""
    from pathlib import Path
    from app.core.paths import safe_join

    base = Path("/data/mirror")

    # 正常拼接
    result = safe_join(base, "115", "冰菓.2012", "video.mkv")
    assert "冰菓.2012" in str(result)

    # 路径遍历
    try:
        safe_join(base, "115", "../..", "evil")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    tests = [
        test_health_endpoint,
        test_config_load,
        test_config_mask,
        test_config_mask_short_value,
        test_config_mask_deepseek_key,
        test_sanitize_filename,
        test_reject_path_traversal,
        test_safe_join,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
