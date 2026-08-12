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


def test_raw_file_model():
    """测试 RawFile 模型可导入和实例化"""
    from app.raw.models import RawFile, RawSnapshot

    f = RawFile(
        id="f1",
        source="pan115",
        relative_path="动画/冰菓.2012/视频.mkv",
        real_path="H:\\115open\\动画\\冰菓.2012\\视频.mkv",
        name="视频.mkv",
        stem="视频",
        ext=".mkv",
        is_file=True,
    )
    assert f.source == "pan115"
    assert f.ext == ".mkv"

    snap = RawSnapshot(
        snapshot_id="s1",
        source="pan115",
        files=[f],
    )
    assert len(snap.files) == 1


def test_import_plan_model():
    """测试 ImportPlan 模型可导入和实例化"""
    from app.import_plan.models import ImportPlan, ImportPlanItem

    item = ImportPlanItem(
        id="i1",
        raw_file_id="f1",
        resource_type="video",
        action="generate_strm",
        work_title="冰菓",
        confidence="high",
    )
    assert item.resource_type == "video"
    assert item.action == "generate_strm"

    plan = ImportPlan(
        plan_id="p1",
        source="pan115",
        status="draft",
        items=[item],
    )
    assert plan.status == "draft"
    assert len(plan.items) == 1


def test_scrape_map_model():
    """测试 ScrapeMap 模型可导入和实例化"""
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    item = ScrapeMapItem(
        scrape_target_id="t1",
        work_id="w1",
        source="pan115",
        local_title="冰菓",
        tmdb_id=12189,
        tmdb_type="tv",
        selected_by="auto",
    )
    sm = ScrapeMap(items=[item])
    assert sm.version == 1
    assert len(sm.items) == 1


def test_library_index_model():
    """测试 LibraryIndex 模型可导入和实例化"""
    from app.library.models import (
        LibraryIndex, WorkIndex, SeasonIndex,
        EpisodeIndex, RelatedWork,
    )

    ep = EpisodeIndex(
        work_id="w1",
        season_number=1,
        episode_number=1,
        title="重生",
    )
    season = SeasonIndex(
        work_id="w1",
        season_number=1,
        group_type="season",
        label="第1季",
    )
    work = WorkIndex(
        work_id="w1",
        title="冰菓",
        year=2012,
        seasons=[season],
        episodes=[ep],
    )
    lib = LibraryIndex(works=[work])
    assert len(lib.works) == 1
    assert lib.works[0].title == "冰菓"


def test_source_adapter_interface():
    """测试 SourceAdapter 抽象接口不可直接实例化"""
    from app.sources.base import SourceAdapter

    try:
        SourceAdapter()
        assert False, "应该抛出 TypeError"
    except TypeError:
        pass


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
        test_raw_file_model,
        test_import_plan_model,
        test_scrape_map_model,
        test_library_index_model,
        test_source_adapter_interface,
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
