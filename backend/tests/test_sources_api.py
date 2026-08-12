# -*- coding: utf-8 -*-
"""来源 API 测试"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"


def _cleanup():
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR)


def _write_temp(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    f.write(content)
    f.close()
    return f.name


def test_get_sources():
    """GET /api/sources"""
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert "sources" in data
    source_ids = {s["source"] for s in data["sources"]}
    assert "pan115" in source_ids
    assert "baidu" in source_ids
    assert "local" in source_ids


def test_samples_uses_configured_directory_tree_dir(tmp_path):
    """目录树选择器只列出配置目录中的支持文件。"""
    from app.core.config import AppConfig, save_config

    tree_dir = tmp_path / "trees"
    nested = tree_dir / "nested"
    nested.mkdir(parents=True)
    (tree_dir / "115.txt").write_text("115", encoding="utf-8")
    (nested / "baidu.tree").write_text("baidu", encoding="utf-8")
    (tree_dir / "ignore.json").write_text("{}", encoding="utf-8")
    save_config(AppConfig(directory_tree_dir=str(tree_dir)))

    resp = client.get("/api/sources/samples")

    assert resp.status_code == 200
    data = resp.json()
    assert data["sample_dir"] == str(tree_dir)
    assert [item["name"] for item in data["files"]] == ["115.txt", "baidu.tree"]


def test_samples_returns_empty_when_directory_tree_dir_is_not_configured():
    """首次安装未配置目录树目录时，不能把后端工作目录当成目录树。"""
    from app.core.config import AppConfig, save_config

    save_config(AppConfig(directory_tree_dir=""))

    resp = client.get("/api/sources/samples")

    assert resp.status_code == 200
    assert resp.json() == {"sample_dir": "", "files": []}


def test_parse_baidu():
    """POST /api/sources/baidu/parse"""
    _cleanup()
    try:
        content = """\
├── 动画
│   ├── CLANNAD
│   │   ├── Season 1
│   │   │   ├── S01E01.mkv
│   │   │   ├── S01E02.mkv
"""
        path = _write_temp(content)
        resp = client.post("/api/sources/baidu/parse", json={
            "input_path": path,
            "source_root": "D:/BaiduNetdisk",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "baidu"
        assert data["file_count"] == 2
        assert data["video_count"] == 2
        assert data["plan_status"] == "draft"
        Path(path).unlink()
    finally:
        _cleanup()


def test_parse_baidu_seasonal_uses_user_selected_exact_directory(tmp_path):
    """单独导出的新番目录树只使用用户选择的精确目录。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    mount_root = tmp_path / "百度网盘" / "01动画"
    seasonal_root = mount_root / "新番"
    seasonal_root.mkdir(parents=True)
    tree_file = tmp_path / "新番_文件目录.txt"
    tree_file.write_text(
        "├── 测试新番 (2026)\n│   ├── Season 1\n│   │   ├── 测试新番.S01E01.mkv\n",
        encoding="utf-8",
    )

    resp = client.post("/api/sources/baidu/parse", json={
        "input_path": str(tree_file),
        "source_root": str(seasonal_root),
        "import_family": "anime",
        "import_scope": "seasonal",
    })

    assert resp.status_code == 200
    snapshot = load_raw_snapshot(resp.json()["snapshot_id"])
    plan = load_import_plan(plan_id=resp.json()["plan_id"])
    assert snapshot is not None
    assert snapshot.source_root == str(seasonal_root)
    assert snapshot.import_scope == "seasonal"
    assert snapshot.files[0].real_path == str(seasonal_root / "测试新番 (2026)" / "Season 1" / "测试新番.S01E01.mkv")
    assert plan is not None
    assert plan.import_scope == "seasonal"


def test_parse_baidu_timestamped_seasonal_tree_infers_configured_mount_directory(tmp_path):
    """带时间戳的新番目录树按文件名前缀解析配置根下的同级目录。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    mount_root = tmp_path / "百度网盘"
    seasonal_root = mount_root / "新番"
    media_file = seasonal_root / "测试新番 (2026)" / "Season 1" / "测试新番.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree_file = tmp_path / "新番_文件目录_20260712005344.txt"
    tree_file.write_text(
        "├── 测试新番 (2026)\n│   ├── Season 1\n│   │   ├── 测试新番.S01E01.mkv\n",
        encoding="utf-8",
    )

    resp = client.post("/api/sources/baidu/parse", json={
        "input_path": str(tree_file),
        "source_root": str(mount_root),
        "import_family": "anime",
        "import_scope": "seasonal",
    })

    assert resp.status_code == 200
    snapshot = load_raw_snapshot(resp.json()["snapshot_id"])
    plan = load_import_plan(plan_id=resp.json()["plan_id"])
    assert snapshot is not None
    assert snapshot.source_root == str(seasonal_root)
    assert snapshot.import_scope == "seasonal"
    assert snapshot.files[0].real_path == str(media_file)
    assert plan is not None
    assert plan.import_scope == "seasonal"
    validation = resp.json()["path_validation"]
    assert validation["ok"] is True
    assert validation["resolved_root"] == str(seasonal_root)


def test_parse_baidu_does_not_treat_regular_timestamped_tree_as_seasonal(tmp_path):
    """普通动画目录树不能因为带时间戳而误判成新番追更。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    mount_root = tmp_path / "百度网盘" / "01动画"
    (mount_root / "新番").mkdir(parents=True)
    tree_file = tmp_path / "动画_文件目录_20260712005344.txt"
    tree_file.write_text(
        "├── 普通番剧 (2020)\n│   ├── Season 1\n│   │   ├── 普通番剧.S01E01.mkv\n",
        encoding="utf-8",
    )

    resp = client.post("/api/sources/baidu/parse", json={
        "input_path": str(tree_file),
        "source_root": str(mount_root),
        "import_family": "anime",
    })

    assert resp.status_code == 200
    snapshot = load_raw_snapshot(resp.json()["snapshot_id"])
    plan = load_import_plan(plan_id=resp.json()["plan_id"])
    assert snapshot is not None
    assert snapshot.source_root == str(mount_root)
    assert snapshot.import_scope == ""
    assert plan is not None
    assert plan.import_scope == ""


def test_parse_baidu_keeps_new_release_scope_when_mount_subdirectory_is_unavailable(tmp_path):
    """文件名写明新番时，挂载盘临时不可枚举也不能丢失分类意图。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    mount_root = tmp_path / "百度网盘" / "01动画"
    mount_root.mkdir(parents=True)
    tree_file = tmp_path / "新番_文件目录_20260712005344.txt"
    tree_file.write_text(
        "├── 测试新番 (2026)\n│   ├── Season 1\n│   │   ├── 测试新番.S01E01.mkv\n",
        encoding="utf-8",
    )

    resp = client.post("/api/sources/baidu/parse", json={
        "input_path": str(tree_file),
        "source_root": str(mount_root),
        "import_family": "anime",
    })

    assert resp.status_code == 200
    snapshot = load_raw_snapshot(resp.json()["snapshot_id"])
    plan = load_import_plan(plan_id=resp.json()["plan_id"])
    assert snapshot is not None
    assert snapshot.source_root == str(mount_root)
    assert snapshot.import_scope == "seasonal"
    assert plan is not None
    assert plan.import_scope == "seasonal"


def test_parse_baidu_treats_root_name_containing_new_release_as_seasonal(tmp_path):
    """有效导入根目录只要含“新番”即可进入追更范围，不要求目录名完全相等。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    mount_root = tmp_path / "2026新番动画"
    mount_root.mkdir()
    tree_file = tmp_path / "目录树.txt"
    tree_file.write_text(
        "├── 测试作品\n│   ├── Season 1\n│   │   ├── 测试作品.S01E01.mkv\n",
        encoding="utf-8",
    )

    resp = client.post("/api/sources/baidu/parse", json={
        "input_path": str(tree_file),
        "source_root": str(mount_root),
        "import_family": "anime",
    })

    assert resp.status_code == 200
    snapshot = load_raw_snapshot(resp.json()["snapshot_id"])
    plan = load_import_plan(plan_id=resp.json()["plan_id"])
    assert snapshot is not None and snapshot.import_scope == "seasonal"
    assert plan is not None and plan.import_scope == "seasonal"


def test_parse_baidu_treats_seasonal_tree_root_as_seasonal_when_file_name_is_generic(tmp_path):
    """目录树文件名普通时，顶层目录为新番也必须保留为追更导入。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    tree_file = tmp_path / "目录树.txt"
    tree_file.write_text(
        "├── 新番\n│   ├── 测试作品\n│   │   ├── Season 1\n│   │   │   ├── 测试作品.S01E01.mkv\n",
        encoding="utf-8",
    )

    resp = client.post("/api/sources/baidu/parse", json={
        "input_path": str(tree_file),
        "source_root": str(tmp_path),
        "import_family": "anime",
    })

    assert resp.status_code == 200
    snapshot = load_raw_snapshot(resp.json()["snapshot_id"])
    plan = load_import_plan(plan_id=resp.json()["plan_id"])
    assert snapshot is not None and len(snapshot.files) == 1
    assert snapshot.import_scope == "seasonal"
    assert plan is not None and plan.import_scope == "seasonal"


def test_parse_not_confirm():
    """parse 后 plan_status 是 draft，不是 confirmed"""
    _cleanup()
    try:
        path = _write_temp("动画/test.mkv\n")
        resp = client.post("/api/sources/baidu/parse", json={
            "input_path": path,
            "source_root": "D:/BaiduNetdisk",
        })
        assert resp.status_code == 200
        assert resp.json()["plan_status"] == "draft"
        Path(path).unlink()
    finally:
        _cleanup()


def test_unknown_source():
    """未知 source 返回 404"""
    resp = client.post("/api/sources/unknown/parse", json={
        "input_path": "test.txt",
        "source_root": "D:/test",
    })
    assert resp.status_code == 404


def test_local_source_use_scan():
    """local 来源应使用 /api/sources/local/scan"""
    resp = client.post("/api/sources/local/parse", json={
        "input_path": "D:/test",
    })
    assert resp.status_code == 400


def test_scan_local():
    """POST /api/sources/local/scan"""
    _cleanup()
    try:
        test_dir = _DATA_DIR / "_test_scan"
        test_dir.mkdir(parents=True)
        (test_dir / "test.mkv").write_text("fake", encoding="utf-8")

        resp = client.post("/api/sources/local/scan", json={
            "root_path": str(test_dir),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "local"
        assert data["file_count"] >= 1
        assert data["plan_status"] == "draft"
        from app.raw.store import load_raw_snapshot
        from app.import_plan.store import load_import_plan
        snapshot = load_raw_snapshot(data["snapshot_id"])
        plan = load_import_plan(plan_id=data["plan_id"])
        assert snapshot is not None
        assert snapshot.source == "local"
        assert plan is not None
        assert plan.status == "draft"
    finally:
        _cleanup()


def test_scan_local_can_mark_anime_as_seasonal(tmp_path):
    """本地动画导入可显式进入新番范围，而非依赖目录名猜测。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    root = tmp_path / "本地新番" / "测试作品"
    root.mkdir(parents=True)
    (root / "测试作品.S01E01.mkv").write_bytes(b"episode")

    resp = client.post("/api/sources/local/scan", json={
        "root_path": str(root), "import_family": "anime", "import_scope": "seasonal",
    })

    assert resp.status_code == 200
    snapshot = load_raw_snapshot(resp.json()["snapshot_id"])
    plan = load_import_plan(plan_id=resp.json()["plan_id"])
    assert snapshot is not None and snapshot.import_scope == "seasonal"
    assert plan is not None and plan.import_scope == "seasonal"


def test_parse_does_not_generate_mirror():
    """parse/scan 默认只生成草稿预览，不生成 mirror，也不提交自动任务"""
    _cleanup()
    try:
        path = _write_temp("动画/test.mkv\n")
        resp = client.post("/api/sources/baidu/parse", json={
            "input_path": path,
            "source_root": "D:/BaiduNetdisk",
        })
        assert resp.status_code == 200
        assert "task_id" not in resp.json()
        assert not (_DATA_DIR / "mirror").exists()
        Path(path).unlink()
    finally:
        _cleanup()


def test_parse_baidu_keeps_all_subdirs_without_seasonal_filter(tmp_path):
    """百度普通目录树不按子目录名称做特殊排除，来源根由用户自行隔离。"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    content = """\
├── 动画
│   ├── 普通作品
│   │   ├── Season 1
│   │   │   ├── 普通番剧.S01E01.mkv
│   │   │   ├── 普通番剧.S01E02.mkv
│   ├── 新番
│   │   ├── 追更作品
│   │   │   ├── Season 1
│   │   │   │   ├── 追更番剧.S01E01.mkv
"""
    path = _write_temp(content)
    try:
        resp = client.post("/api/sources/baidu/parse", json={
            "input_path": path,
            "source_root": str(tmp_path),
            "import_family": "anime",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "baidu"
        assert data["file_count"] == 3
        assert data["video_count"] == 3
        assert data.get("import_scope") == ""  # 范围为空
        snapshot = load_raw_snapshot(data["snapshot_id"])
        plan = load_import_plan(plan_id=data["plan_id"])
        assert snapshot is not None and snapshot.import_scope == ""
        assert plan is not None and plan.import_scope == ""
    finally:
        Path(path).unlink()
        _cleanup()


def test_parse_baidu_seasonal_tree_keeps_all_files(tmp_path):
    """百度新番目录树：文件名含新番，保留所有文件，得到 seasonal"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    mount_root = tmp_path / "百度网盘" / "01动画"
    mount_root.mkdir(parents=True)
    tree_file = tmp_path / "新番_文件目录_20260712005344.txt"
    tree_file.write_text(
        "├── 追更作品 (2026)\n│   ├── Season 1\n│   │   ├── 测试番剧.S01E01.mkv\n",
        encoding="utf-8",
    )
    resp = client.post("/api/sources/baidu/parse", json={
        "input_path": str(tree_file),
        "source_root": str(mount_root),
        "import_family": "anime",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["file_count"] == 1
    assert data["video_count"] == 1
    assert data.get("import_scope") == "seasonal"
    snapshot = load_raw_snapshot(data["snapshot_id"])
    plan = load_import_plan(plan_id=data["plan_id"])
    assert snapshot is not None and snapshot.import_scope == "seasonal"
    assert plan is not None and plan.import_scope == "seasonal"


def test_parse_baidu_normal_tree_with_manual_seasonal(tmp_path):
    """百度普通目录树手动 import_scope=seasonal：保留文件，不做固定排除，得到 seasonal"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    content = """\
├── 动画
│   ├── 普通作品 (2020)
│   │   ├── Season 1
│   │   │   ├── 普通番剧.S01E01.mkv
│   ├── 新番
│   │   ├── 追更作品 (2026)
│   │   │   ├── Season 1
│   │   │   │   ├── 追更番剧.S01E01.mkv
"""
    path = _write_temp(content)
    try:
        resp = client.post("/api/sources/baidu/parse", json={
            "input_path": path,
            "source_root": str(tmp_path),
            "import_family": "anime",
            "import_scope": "seasonal",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_count"] == 2
        assert data["video_count"] == 2
        assert data.get("import_scope") == "seasonal"
        snapshot = load_raw_snapshot(data["snapshot_id"])
        plan = load_import_plan(plan_id=data["plan_id"])
        assert snapshot is not None and snapshot.import_scope == "seasonal"
        assert plan is not None and plan.import_scope == "seasonal"
    finally:
        Path(path).unlink()
        _cleanup()


def test_parse_115_with_seasonal_has_no_subdir_filter(tmp_path):
    """115 目录树手动 import_scope=seasonal：得到 seasonal，不新增子目录过滤规则"""
    from app.raw.store import load_raw_snapshot
    from app.import_plan.store import load_import_plan

    content = """\
动画
├── 普通作品 (2020)
│   ├── Season 1
│   │   ├── S01E01.mkv
"""
    path = _write_temp(content)
    try:
        resp = client.post("/api/sources/pan115/parse", json={
            "input_path": path,
            "source_root": str(tmp_path),
            "import_family": "anime",
            "import_scope": "seasonal",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("import_scope") == "seasonal"
        snapshot = load_raw_snapshot(data["snapshot_id"])
        plan = load_import_plan(plan_id=data["plan_id"])
        assert snapshot is not None and snapshot.import_scope == "seasonal"
        assert plan is not None and plan.import_scope == "seasonal"
    finally:
        Path(path).unlink()
        _cleanup()


def test_parse_live_with_seasonal_returns_400(tmp_path):
    """import_family=live 且 import_scope=seasonal 返回 400"""
    content = "├── 剧集\n│   ├── 剧集.S01E01.mkv\n"
    path = _write_temp(content)
    try:
        resp = client.post("/api/sources/baidu/parse", json={
            "input_path": path,
            "source_root": str(tmp_path),
            "import_family": "live",
            "import_scope": "seasonal",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert "新番" in data.get("detail", "") or "动画" in data.get("detail", "")
    finally:
        Path(path).unlink()
        _cleanup()


def test_no_exclude_root_names_remaining():
    """确认 ParseRequest 不再有 exclude_root_names 字段"""
    from app.api.sources import ParseRequest
    assert "exclude_root_names" not in ParseRequest.model_fields


if __name__ == "__main__":
    tests = [
        test_get_sources,
        test_parse_baidu,
        test_parse_not_confirm,
        test_unknown_source,
        test_local_source_use_scan,
        test_scan_local,
        test_parse_does_not_generate_mirror,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
