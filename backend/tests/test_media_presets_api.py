# -*- coding: utf-8 -*-
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _tree(*episodes: int) -> bytes:
    lines = ["|——动画", "| |-[测试字幕组] 示例动画"]
    lines.extend(f"| | |-[测试字幕组] 示例动画 [{episode:02d}].mkv" for episode in episodes)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _create(client: TestClient, episodes=(1,), *, filename="动画_目录树.txt"):
    return client.post(
        "/api/media-presets",
        data={
            "source": "pan115",
            "source_root": "H:/115open",
            "import_family": "anime",
            "import_scope": "",
        },
        files={"tree_file": (filename, _tree(*episodes), "text/plain")},
    )


def _write_local_episode(root: Path, episode: int) -> Path:
    media_file = root / "示例动画" / "Season 1" / f"示例动画.S01E{episode:02d}.mkv"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(f"episode-{episode}".encode("ascii"))
    return media_file


def test_local_scan_creates_a_path_scoped_media_library_card(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "本地动画"
    _write_local_episode(media_root, 1)

    with TestClient(app) as client:
        response = client.post(
            "/api/sources/local/scan",
            json={"root_path": str(media_root), "import_family": "anime", "import_scope": ""},
        )
        repeated = client.post(
            "/api/sources/local/scan",
            json={"root_path": str(media_root), "import_family": "live", "import_scope": ""},
        )
        listed = client.get("/api/media-presets")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source"] == "local"
    assert body["preset"]["source_root"] == str(media_root.resolve())
    assert body["preset"]["update_mode"] == "local_scan"
    assert body["preset"]["version_count"] == 1
    assert body["version"]["archive_path"] == ""
    assert body["version"]["snapshot_id"] == body["snapshot_id"]
    assert repeated.json()["preset"]["preset_id"] == body["preset"]["preset_id"]
    assert repeated.json()["preset"]["import_family"] == "anime"
    assert repeated.json()["unchanged"] is True
    assert [item["preset_id"] for item in listed.json()["presets"]] == [body["preset"]["preset_id"]]


def test_local_media_library_card_rescans_saved_path_incrementally(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "本地动画"
    _write_local_episode(media_root, 1)

    with TestClient(app) as client:
        created = client.post(
            "/api/sources/local/scan",
            json={"root_path": str(media_root), "import_family": "anime", "import_scope": ""},
        ).json()
        _write_local_episode(media_root, 2)

        response = client.post(
            f"/api/media-presets/{created['preset']['preset_id']}/rescan-local",
        )
        listed = client.get("/api/media-presets").json()["presets"]

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["preset_id"] == created["preset"]["preset_id"]
    assert body["preset"]["version_count"] == 2
    assert body["diff"]["added_count"] == 1
    assert body["diff"]["missing_count"] == 0
    assert body["unchanged"] is False
    assert body["preview"]["status"] == "draft"
    assert len(listed) == 1


@pytest.mark.parametrize(
    ("content", "encoding", "expected_source"),
    [
        ("|——动画\n| |-[字幕组] 示例动画\n| | |-[字幕组] 示例动画 [01].mkv\n", "utf-8", "pan115"),
        ("|——动画\n| |-[字幕组] 示例动画\n| | |-[字幕组] 示例动画 [01].mkv\n", "utf-16", "pan115"),
        ("├── 示例动画\n│   └── 示例动画 [01].mkv\n", "utf-8", "baidu"),
        ("├── 示例动画\n│   └── 示例动画 [01].mkv\n", "gbk", "baidu"),
    ],
)
def test_detects_dropped_tree_source_from_content(tmp_path, content, encoding, expected_source):
    from app.media_presets.service import detect_tree_source

    tree_path = tmp_path / "目录树.txt"
    tree_path.write_bytes(content.encode(encoding))

    assert detect_tree_source(tree_path) == expected_source


def test_import_local_tree_path_detects_baidu_and_uses_existing_preset_flow(tmp_path):
    from app.core.config import AppConfig, save_config

    mount_root = tmp_path / "百度网盘"
    seasonal_root = mount_root / "新番"
    media_file = seasonal_root / "示例动画" / "示例动画 [01].mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    save_config(AppConfig(baidu_root=str(mount_root)))

    # 新根路径合同：TXT 绝对路径是权威，TXT 父目录即 source_root。
    # TXT 放在媒体实际根目录内（即挂载盘对应目录）。
    tree_path = seasonal_root / "新番_文件目录_20260722174035.txt"
    original = "├── 示例动画\n│   └── 示例动画 [01].mkv\n".encode("utf-8")
    tree_path.write_bytes(original)

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/import-local-tree",
            json={
                "tree_path": str(tree_path),
                "expected_source": "baidu",
                "import_family": "anime",
                "import_scope": "",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source"] == "baidu"
    assert body["preset"]["source_root"] == str(seasonal_root)
    assert body["version"]["source_tree_path"] == str(tree_path.resolve())
    assert body["preset"]["import_scope"] == "seasonal"
    assert body["preset"]["lifecycle_status"] == "draft"
    assert body["version"]["path_validation"]["ok"] is True
    assert body["preview"]["status"] == "draft"
    archive = Path(body["version"]["archive_path"])
    assert (tmp_path / "data" / archive).read_bytes() == original
    assert tree_path.read_bytes() == original


def test_dropped_baidu_seasonal_tree_reuses_multi_level_animation_recognition(tmp_path):
    """新番 TXT 与已完结动画共用多层目录识别，不限定作品所在层级。"""
    from app.core.config import AppConfig, save_config

    mount_root = tmp_path / "百度网盘"
    seasonal_root = mount_root / "新番"
    direct_video = seasonal_root / "根目录作品 (2026) {tmdb-1001}" / "Season 1" / "根目录作品.S01E01.mkv"
    nested_video = (
        seasonal_root / "挂载好的动画" / "分类内作品 (2026) {tmdb-1002}" / "Season 1" / "分类内作品.S01E01.mkv"
    )
    for media_file in (direct_video, nested_video):
        media_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.write_bytes(b"video")
    save_config(AppConfig(baidu_root=str(mount_root)))

    tree_path = seasonal_root / "新番_文件目录_20260722174035.txt"
    tree_path.write_text(
        "├── 根目录作品 (2026) {tmdb-1001}\n"
        "│   ├── Season 1\n"
        "│   │   ├── 根目录作品.S01E01.mkv\n"
        "├── 挂载好的动画\n"
        "│   ├── 分类内作品 (2026) {tmdb-1002}\n"
        "│   │   ├── Season 1\n"
        "│   │   │   ├── 分类内作品.S01E01.mkv\n",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/import-local-tree",
            json={
                "tree_path": str(tree_path),
                "expected_source": "baidu",
                "import_family": "anime",
                "import_scope": "",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source_root"] == str(seasonal_root)
    assert body["version"]["source_tree_path"] == str(tree_path.resolve())
    assert body["preset"]["import_scope"] == "seasonal"
    assert body["preset"]["work_count"] == 2
    assert body["preset"]["video_count"] == 2
    assert body["version"]["path_validation"]["ok"] is True
    assert body["version"]["path_validation"]["checked_count"] == 2


@pytest.mark.parametrize(
    ("name", "content", "expected_detail"),
    [
        ("说明.txt", b"this is not a directory tree", "无法识别目录树来源"),
        ("目录树.log", b"|--not-supported", "仅支持 .txt"),
        ("空目录树.txt", b"", "目录树文件为空"),
    ],
)
def test_import_local_tree_path_rejects_invalid_input_without_creating_preset(
    tmp_path,
    name,
    content,
    expected_detail,
):
    tree_path = tmp_path / name
    tree_path.write_bytes(content)

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_path), "import_family": "anime"},
        )
        presets = client.get("/api/media-presets").json()["presets"]

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]
    assert presets == []


def test_import_local_tree_path_rejects_directory_and_oversized_txt(tmp_path, monkeypatch):
    from app.media_presets import service

    directory = tmp_path / "目录树.txt"
    directory.mkdir()
    oversized = tmp_path / "过大目录树.txt"
    oversized.write_bytes(b"|" * 17)
    monkeypatch.setattr(service, "_MAX_UPLOAD_BYTES", 16)

    with TestClient(app) as client:
        directory_response = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(directory), "import_family": "anime"},
        )
        oversized_response = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(oversized), "import_family": "anime"},
        )

    assert directory_response.status_code == 400
    assert "不是可读取文件" in directory_response.json()["detail"]
    assert oversized_response.status_code == 413
    assert "超过 64 MB" in oversized_response.json()["detail"]


def test_create_preset_archives_tree_and_survives_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = _create(client)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["preset"]["name"] == "动画一"
        assert body["preset"]["version_count"] == 1
        assert body["preset"]["lifecycle_status"] == "draft"
        assert body["preset"]["is_library_indexed"] is False
        archive = tmp_path / body["version"]["archive_path"]
        assert archive.exists()
        assert archive.read_bytes() == _tree(1)

        listed = client.get("/api/media-presets").json()["presets"]
        assert listed[0]["preset_id"] == body["preset"]["preset_id"]
        assert listed[0]["current_snapshot_id"] == body["preset"]["current_snapshot_id"]


def test_baidu_preset_infers_timestamped_seasonal_root_from_tree_name(tmp_path, monkeypatch):
    """新番目录树按文件名前缀解析配置根下的同级新番目录，并完成路径验证。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    mount_root = tmp_path / "百度网盘"
    expected_root = mount_root / "新番"
    media_file = expected_root / "测试动画" / "测试动画 [01].mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = "├── 测试动画\n│   └── 测试动画 [01].mkv\n".encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets",
            data={
                "source": "baidu",
                "source_root": str(mount_root),
                "import_family": "anime",
                "import_scope": "seasonal",
            },
            files={
                "tree_file": (
                    "新番_文件目录_20260718235356.txt",
                    tree,
                    "text/plain",
                )
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    validation = body["version"]["path_validation"]
    assert body["preset"]["source_root"] == str(expected_root)
    assert validation["ok"] is True
    assert validation["status"] == "verified"
    assert validation["scope_name"] == "新番"
    assert validation["resolved_root"] == str(expected_root)
    assert validation["example_path"] == str(media_file)


def test_baidu_seasonal_preset_uses_user_selected_exact_root(tmp_path, monkeypatch):
    """新番目录树必须以用户选择的真实文件夹为根并完成抽样验证。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    selected_root = tmp_path / "百度网盘" / "01动画" / "新番"
    media_file = selected_root / "测试动画" / "测试动画 [01].mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = "├── 测试动画\n│   └── 测试动画 [01].mkv\n".encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets",
            data={
                "source": "baidu",
                "source_root": str(selected_root),
                "import_family": "anime",
                "import_scope": "seasonal",
            },
            files={"tree_file": ("新番_文件目录.txt", tree, "text/plain")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source_root"] == str(selected_root)
    assert body["version"]["path_validation"]["ok"] is True
    assert body["version"]["path_validation"]["example_path"] == str(media_file)


def test_baidu_seasonal_preset_uses_configured_mount_root_when_source_root_is_empty(tmp_path, monkeypatch):
    """未显式选择目录时，按配置根和目录树文件名前缀推断新番目录。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    from app.core.config import AppConfig, save_config

    mount_root = tmp_path / "百度网盘"
    seasonal_root = mount_root / "新番"
    media_file = seasonal_root / "测试动画" / "测试动画 [01].mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    save_config(AppConfig(baidu_root=str(mount_root)))
    tree = "├── 测试动画\n│   └── 测试动画 [01].mkv\n".encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets",
            data={
                "source": "baidu",
                "source_root": "",
                "import_family": "anime",
                "import_scope": "seasonal",
            },
            files={"tree_file": ("新番-文件目录-20260722174035.txt", tree, "text/plain")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source_root"] == str(seasonal_root)
    assert body["version"]["path_validation"]["ok"] is True
    assert body["version"]["path_validation"]["example_path"] == str(media_file)


def test_baidu_regular_preset_keeps_all_subdirs_without_seasonal_filter(tmp_path, monkeypatch):
    """普通动画预设不按子目录名排除内容，动画与新番由用户选择独立来源根。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    selected_root = tmp_path / "百度网盘" / "01动画"
    regular_file = selected_root / "普通作品" / "Season 1" / "普通作品.S01E01.mkv"
    named_subdir_file = selected_root / "新番" / "归档作品" / "Season 1" / "归档作品.S01E01.mkv"
    regular_file.parent.mkdir(parents=True)
    named_subdir_file.parent.mkdir(parents=True)
    regular_file.write_bytes(b"video")
    named_subdir_file.write_bytes(b"video")
    tree = (
        "├── 普通作品\n"
        "│   ├── Season 1\n"
        "│   │   ├── 普通作品.S01E01.mkv\n"
        "├── 新番\n"
        "│   ├── 归档作品\n"
        "│   │   ├── Season 1\n"
        "│   │   │   ├── 归档作品.S01E01.mkv\n"
    ).encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets",
            data={
                "source": "baidu",
                "source_root": str(selected_root),
                "import_family": "anime",
                "import_scope": "",
            },
            files={"tree_file": ("01动画_文件目录.txt", tree, "text/plain")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["import_scope"] == ""
    assert body["preset"]["video_count"] == 2
    assert body["preview"]["summary"]["video_count"] == 2
    assert body["version"]["path_validation"]["ok"] is True


def test_baidu_seasonal_folder_scan_creates_preset_without_opening_video(tmp_path, monkeypatch):
    """真实文件夹首导必须复用元数据扫描，不能读取视频内容。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    selected_root = tmp_path / "百度网盘" / "01动画" / "新番"
    media_file = selected_root / "测试动画" / "测试动画 [01].mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video-content-must-not-be-read")

    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if Path(path).suffix.lower() == ".mkv":
            raise AssertionError("元数据扫描不应打开视频文件")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/scan-folder",
            json={
                "source": "baidu",
                "source_root": str(selected_root),
                "import_family": "anime",
                "import_scope": "seasonal",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source"] == "baidu"
    assert body["preset"]["source_root"] == str(selected_root)
    assert body["preset"]["import_scope"] == "seasonal"
    assert body["preset"]["lifecycle_status"] == "draft"
    assert body["version"]["archive_path"] == ""
    assert body["version"]["path_validation"]["ok"] is True
    assert body["preview"]["summary"]["video_count"] == 1


def test_baidu_seasonal_folder_scan_rejects_unreachable_root(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/scan-folder",
            json={
                "source": "baidu",
                "source_root": str(tmp_path / "missing"),
                "import_family": "anime",
                "import_scope": "seasonal",
            },
        )

    assert response.status_code == 400
    assert "不存在或不可访问" in response.json()["detail"]


def test_invalid_preset_can_rebind_to_user_selected_root_and_revalidate(tmp_path, monkeypatch):
    """自动验证失败后可用手动目录重新解析当前归档并重建草稿计划。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    wrong_root = tmp_path / "百度网盘"
    correct_root = wrong_root / "01动画" / "新番"
    media_file = correct_root / "测试动画" / "测试动画 [01].mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = "├── 测试动画\n│   └── 测试动画 [01].mkv\n".encode("utf-8")

    with TestClient(app) as client:
        created = client.post(
            "/api/media-presets",
            data={
                "source": "baidu",
                "source_root": str(wrong_root),
                "import_family": "anime",
                "import_scope": "",
            },
            files={"tree_file": ("动画_目录树.txt", tree, "text/plain")},
        ).json()
        assert created["version"]["path_validation"]["ok"] is False

        response = client.post(
            f"/api/media-presets/{created['preset']['preset_id']}/source-root",
            json={"source_root": str(correct_root)},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source_root"] == str(correct_root)
    assert body["preset"]["lifecycle_status"] == "draft"
    assert body["version"]["path_validation"]["ok"] is True
    assert body["version"]["path_validation"]["example_path"] == str(media_file)
    assert body["preview"]["status"] == "draft"


def test_failed_preset_can_revalidate_current_source_root_after_mount_recovers(tmp_path, monkeypatch):
    """历史路径失败不应永久卡住：当前根目录恢复可达后可刷新验证状态。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    source_root = tmp_path / "百度网盘" / "01动画"
    media_file = source_root / "测试动画" / "测试动画 [01].mkv"
    tree = "├── 测试动画\n│   └── 测试动画 [01].mkv\n".encode("utf-8")

    with TestClient(app) as client:
        created_response = client.post(
            "/api/media-presets",
            data={
                "source": "baidu",
                "source_root": str(source_root),
                "import_family": "anime",
                "import_scope": "",
            },
            files={"tree_file": ("根目录_目录树.txt", tree, "text/plain")},
        )
        assert created_response.status_code == 200, created_response.text
        created = created_response.json()
        assert created["preset"]["source_root"] == str(source_root)
        assert created["version"]["path_validation"]["ok"] is False

        media_file.parent.mkdir(parents=True)
        media_file.write_bytes(b"video")

        response = client.post(
            f"/api/media-presets/{created['preset']['preset_id']}/revalidate",
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source_root"] == str(source_root)
    assert body["preset"]["current_version_id"] == created["preset"]["current_version_id"]
    assert body["version"]["version_id"] == created["version"]["version_id"]
    assert body["version"]["path_validation"]["ok"] is True
    assert body["version"]["path_validation"]["example_path"] == str(media_file)
    assert body["preview"]["status"] == "draft"


def test_confirm_plan_updates_preset_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = _create(client).json()
        preset_id = created["preset"]["preset_id"]
        plan_id = created["preset"]["current_plan_id"]

        response = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": plan_id},
        )

        assert response.status_code == 200, response.text
        preset = client.get(f"/api/media-presets/{preset_id}").json()["preset"]
        assert preset["lifecycle_status"] == "confirmed"
        assert preset["is_library_indexed"] is False

        from app.media_presets.service import mark_preset_lifecycle
        mark_preset_lifecycle(plan_id, "ready")
        indexed = client.get(f"/api/media-presets/{preset_id}").json()["preset"]
        assert indexed["lifecycle_status"] == "ready"
        assert indexed["is_library_indexed"] is True


def test_direct_preset_delete_requires_complete_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = _create(client).json()
        preset_id = created["preset"]["preset_id"]
        archive = tmp_path / created["version"]["archive_path"]
        mirror_file = tmp_path / "mirror" / "keep.strm"
        mirror_file.parent.mkdir(parents=True)
        mirror_file.write_text("H:/media/keep.mkv", encoding="utf-8")

        response = client.delete(f"/api/media-presets/{preset_id}")

        assert response.status_code == 409, response.text
        assert archive.exists()
        assert mirror_file.exists()
        assert len(client.get("/api/media-presets").json()["presets"]) == 1
        assert client.get(f"/api/media-presets/{preset_id}").status_code == 200


def test_reimporting_same_tree_reuses_existing_media_library_without_new_version(tmp_path, monkeypatch):
    """同来源根、分类和 TXT 内容不能重复创建媒体库卡片或版本。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))

    with TestClient(app) as client:
        first = _create(client, (1, 2), filename="动画_目录树.txt")
        second = _create(client, (1, 2), filename="动画_目录树.txt")
        presets = client.get("/api/media-presets").json()["presets"]

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["reused_preset"] is True
    assert second.json()["preset"]["preset_id"] == first.json()["preset"]["preset_id"]
    assert second.json()["preset"]["version_count"] == 1
    assert len(presets) == 1


def test_reimporting_same_root_and_media_with_different_txt_name_keeps_one_version(tmp_path, monkeypatch):
    """TXT 文件名和换行变化不代表媒体库内容变化。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))

    with TestClient(app) as client:
        first = _create(client, (1, 2), filename="第一次导出.txt").json()
        second_response = client.post(
            "/api/media-presets",
            data={
                "source": "pan115",
                "source_root": "H:/115open",
                "import_family": "anime",
                "import_scope": "",
            },
            files={
                "tree_file": (
                    "完全不同的文件名.txt",
                    _tree(1, 2).replace(b"\n", b"\r\n"),
                    "text/plain",
                ),
            },
        )
        presets = client.get("/api/media-presets").json()["presets"]

    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["reused_preset"] is True
    assert second["unchanged"] is True
    assert second["preset"]["preset_id"] == first["preset"]["preset_id"]
    assert second["preset"]["version_count"] == 1
    assert len(presets) == 1


def test_reimporting_changed_media_under_same_root_updates_existing_card(tmp_path, monkeypatch):
    """实际根路径相同但视频集合变化时，更新原卡片并新增版本。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))

    with TestClient(app) as client:
        first = _create(client, (1,), filename="旧目录.txt").json()
        second_response = _create(client, (1, 2), filename="新版目录.txt")
        presets = client.get("/api/media-presets").json()["presets"]

    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["reused_preset"] is True
    assert second["unchanged"] is False
    assert second["preset"]["preset_id"] == first["preset"]["preset_id"]
    assert second["preset"]["version_count"] == 2
    assert len(presets) == 1


def test_same_tree_under_different_actual_roots_creates_distinct_cards(tmp_path, monkeypatch):
    """同一挂载来源下的不同实际媒体根必须保持为不同卡片。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))

    with TestClient(app) as client:
        first = _create(client, (1,), filename="目录.txt").json()
        second_response = client.post(
            "/api/media-presets",
            data={
                "source": "pan115",
                "source_root": "H:/115open/另一个媒体库",
                "import_family": "anime",
                "import_scope": "",
            },
            files={"tree_file": ("目录.txt", _tree(1), "text/plain")},
        )
        presets = client.get("/api/media-presets").json()["presets"]

    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["reused_preset"] is not True
    assert second["preset"]["preset_id"] != first["preset"]["preset_id"]
    assert len(presets) == 2


def test_media_preset_delete_only_removes_card_and_tree_archives(tmp_path, monkeypatch):
    """导入卡片删除不得触碰已生成媒体、刮削结果或媒体库索引。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    mirror_root = tmp_path / "mirror"
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror_root))

    from app.import_plan.store import load_import_plan, save_import_plan
    from app.library.index import _library_work_id
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    source_file = tmp_path / "source" / "示例动画.mkv"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"source-video")

    with TestClient(app) as client:
        created = _create(client).json()
        preset = created["preset"]
        archive = tmp_path / created["version"]["archive_path"]
        plan = load_import_plan(plan_id=preset["current_plan_id"])
        item = next(entry for entry in plan.items if entry.resource_type == "video")
        target_dir = mirror_root / "115" / "示例动画" / "Season 1"
        target_dir.mkdir(parents=True)
        strm_path = target_dir / "S01E01.strm"
        nfo_path = target_dir / "S01E01.nfo"
        poster_path = target_dir.parent / "poster.jpg"
        strm_path.write_text(str(source_file), encoding="utf-8")
        nfo_path.write_text("<episodedetails />", encoding="utf-8")
        poster_path.write_bytes(b"poster")
        item.real_path = str(source_file)
        item.target_dir = str(target_dir)
        item.target_strm_path = str(strm_path)
        save_import_plan(plan, update_latest=False)
        work_id = _library_work_id(item)
        save_library_index(LibraryIndex(works=[WorkIndex(
            work_id=work_id,
            source="pan115",
            episodes=[EpisodeIndex(
                episode_id="episode-1",
                work_id=work_id,
                source="pan115",
                strm_path=str(strm_path),
            )],
        )]))

        preview_response = client.post(
            f"/api/media-presets/{preset['preset_id']}/delete/preview",
            json={},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["blocked"] is False
        assert preview["archive_version_count"] == 1
        assert preview["preserved_generated_media"] is True
        assert preview["preserved_library_data"] is True
        assert "generated_file_count" not in preview
        assert "library_work_count" not in preview

        confirm_response = client.post(
            f"/api/media-presets/{preset['preset_id']}/delete/confirm",
            json={"preview_id": preview["preview_id"]},
        )
        assert confirm_response.status_code == 200, confirm_response.text
        result = confirm_response.json()

        assert result["deleted_preset"] is True
        assert result["deleted_archive_count"] == 1
        assert result["preserved_generated_media"] is True
        assert result["preserved_library_data"] is True
        assert "deleted_generated_count" not in result
        assert "deleted_library_work_count" not in result
        assert client.get(f"/api/media-presets/{preset['preset_id']}").status_code == 404

    assert not archive.exists()
    assert strm_path.exists()
    assert nfo_path.exists()
    assert poster_path.exists()
    assert source_file.exists()
    assert load_import_plan(plan_id=preset["current_plan_id"]) is not None
    from app.library.store import load_library_index
    assert load_library_index().works[0].work_id == work_id


def test_media_preset_delete_ignores_real_paths_after_config_changes(tmp_path, monkeypatch):
    """卡片删除不扫描计划中的真实路径，更不能删除真实文件。"""
    data_dir = tmp_path / "data"
    mirror_root = tmp_path / "mirror"
    source_root = mirror_root / "detached-mount"
    source_file = source_root / "示例动画" / "示例动画.mkv"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"real-video")
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror_root))

    from app.core.config import AppConfig, save_config
    from app.import_plan.store import load_import_plan, save_import_plan
    from app.media_presets.store import get_preset, save_preset

    save_config(AppConfig(pan115_root=str(tmp_path / "new-mount"), mirror_dir=str(mirror_root)))
    with TestClient(app) as client:
        created = _create(client).json()
        preset_id = created["preset"]["preset_id"]
        preset = get_preset(preset_id)
        assert preset is not None
        preset.source_root = str(source_root)
        save_preset(preset)

        plan = load_import_plan(plan_id=preset.current_plan_id)
        item = next(entry for entry in plan.items if entry.resource_type == "video")
        item.real_path = str(source_file)
        item.target_dir = str(source_file.parent)
        item.target_strm_path = str(source_file)
        save_import_plan(plan, update_latest=False)

        preview_response = client.post(f"/api/media-presets/{preset_id}/delete/preview", json={})
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["blocked"] is False
        assert preview["preserved_generated_media"] is True

        confirm_response = client.post(
            f"/api/media-presets/{preset_id}/delete/confirm",
            json={"preview_id": preview["preview_id"]},
        )
        assert confirm_response.status_code == 200
        assert client.get(f"/api/media-presets/{preset_id}").status_code == 404

    assert source_file.exists()


def test_media_preset_delete_preserves_source_directories(tmp_path, monkeypatch):
    """卡片删除不收集或删除计划中的来源目录。"""
    data_dir = tmp_path / "data"
    mirror_root = tmp_path / "mirror"
    source_root = mirror_root / "detached-mount"
    source_season_dir = source_root / "示例动画" / "Season 1"
    source_season_dir.mkdir(parents=True)
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror_root))

    from app.core.config import AppConfig, save_config
    from app.import_plan.store import load_import_plan, save_import_plan
    from app.media_presets.store import get_preset, save_preset

    save_config(AppConfig(pan115_root=str(tmp_path / "new-mount"), mirror_dir=str(mirror_root)))
    with TestClient(app) as client:
        created = _create(client).json()
        preset_id = created["preset"]["preset_id"]
        preset = get_preset(preset_id)
        assert preset is not None
        preset.source_root = str(source_root)
        save_preset(preset)

        plan = load_import_plan(plan_id=preset.current_plan_id)
        item = next(entry for entry in plan.items if entry.resource_type == "video")
        item.target_dir = str(source_season_dir)
        item.target_strm_path = ""
        save_import_plan(plan, update_latest=False)

        preview_response = client.post(f"/api/media-presets/{preset_id}/delete/preview", json={})
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["blocked"] is False
        assert preview["preserved_generated_media"] is True

        confirm_response = client.post(
            f"/api/media-presets/{preset_id}/delete/confirm",
            json={"preview_id": preview["preview_id"]},
        )
        assert confirm_response.status_code == 200

    assert source_season_dir.is_dir()


def test_draft_preset_delete_only_owns_explicit_version_records(tmp_path, monkeypatch):
    """未继续执行的导入不能按宽泛来源根吞并旧快照和观看状态。"""
    data_dir = tmp_path / "data"
    source_root = tmp_path / "source" / "动画库"
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(data_dir / "mirror"))

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import load_import_plan, save_import_plan
    from app.library.watch_status import get_watch_status, set_watch_status
    from app.raw.models import RawSnapshot
    from app.raw.store import save_raw_snapshot

    with TestClient(app) as client:
        created_response = client.post(
            "/api/media-presets",
            data={
                "source": "pan115",
                "source_root": str(source_root),
                "import_family": "anime",
                "import_scope": "",
            },
            files={"tree_file": ("动画_目录树.txt", _tree(1), "text/plain")},
        )
        assert created_response.status_code == 200, created_response.text
        created = created_response.json()
        preset = created["preset"]
        owned_plan = load_import_plan(plan_id=preset["current_plan_id"])
        owned_work_id = next(
            item.work_id for item in owned_plan.items
            if item.resource_type == "video" and item.work_id
        )

        legacy_snapshot = RawSnapshot(
            snapshot_id="legacy-same-root-snapshot",
            source="pan115",
            source_root=str(source_root),
        )
        save_raw_snapshot(legacy_snapshot, update_latest=False)
        legacy_plan = ImportPlan(
            plan_id="legacy-same-root-plan",
            source="pan115",
            source_snapshot_id=legacy_snapshot.snapshot_id,
            status="confirmed",
            items=[ImportPlanItem(
                id="legacy-item",
                plan_id="legacy-same-root-plan",
                source="pan115",
                resource_type="video",
                action="generate_strm",
                work_id=owned_work_id,
                real_path=str(source_root / "旧动画" / "01.mkv"),
            )],
        )
        save_import_plan(legacy_plan, update_latest=False)
        set_watch_status(owned_work_id, "watching")

        preview_response = client.post(
            f"/api/media-presets/{preset['preset_id']}/delete/preview",
            json={},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["archive_version_count"] == 1
        assert preview["preserved_generated_media"] is True
        assert preview["preserved_library_data"] is True

        confirm_response = client.post(
            f"/api/media-presets/{preset['preset_id']}/delete/confirm",
            json={"preview_id": preview["preview_id"]},
        )
        assert confirm_response.status_code == 200, confirm_response.text
        assert confirm_response.json()["deleted_preset"] is True

    assert (data_dir / "raw_snapshots" / "legacy-same-root-snapshot.json").is_file()
    assert (data_dir / "import_plans" / "legacy-same-root-plan.json").is_file()
    assert (data_dir / "import_plans" / f"{preset['current_plan_id']}.json").is_file()
    assert get_watch_status(owned_work_id).status == "watching"


def test_media_preset_delete_preserves_all_downstream_records(tmp_path, monkeypatch):
    """卡片删除不得沿来源根追删刮削、媒体库、追更或播放数据。"""
    data_dir = tmp_path / "data"
    mirror_root = data_dir / "mirror"
    source_root = tmp_path / "source" / "新番"
    source_file = source_root / "示例动画" / "示例动画 [01].mkv"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"real-video")
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror_root))

    from app.core.config import AppConfig, save_config
    from app.db.database import close_connection, get_connection
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.overrides import set_title_override
    from app.library.store import load_library_index, save_library_index
    from app.library.watch_status import load_watch_statuses, set_watch_status
    from app.media_presets.service import mark_preset_lifecycle
    from app.playback.history import build_history_item, load_history, save_history
    from app.playback.progress import load_progress, save_progress
    from app.raw.models import RawFile, RawSnapshot
    from app.raw.store import save_raw_snapshot
    from app.scrape.models import ScrapeMap, ScrapeMapItem
    from app.scrape.review_queue import ReviewQueue, ReviewQueueItem, _save_review_item_to_db, save_review_queue
    from app.scrape.store import load_scrape_map, save_scrape_map
    from app.tracking.models import TrackingBinding
    from app.tracking.store import record_tracking_scan_run, upsert_tracking_binding

    save_config(AppConfig(pan115_root=str(source_root), mirror_dir=str(mirror_root)))
    tree = _tree(1)
    with TestClient(app) as client:
        created_response = client.post(
            "/api/media-presets",
            data={
                "source": "pan115",
                "source_root": str(source_root),
                "import_family": "anime",
                "import_scope": "seasonal",
            },
            files={"tree_file": ("新番目录树.txt", tree, "text/plain")},
        )
        assert created_response.status_code == 200, created_response.text
        created = created_response.json()
        preset_id = created["preset"]["preset_id"]
        mark_preset_lifecycle(created["preset"]["current_plan_id"], "mirrored")

        downstream_snapshot = RawSnapshot(
            snapshot_id="downstream-snapshot",
            source="pan115",
            source_root=str(source_root / "示例动画"),
            import_family="anime",
            import_scope="seasonal",
            video_count=1,
            file_count=1,
            files=[RawFile(
                id="raw-1",
                snapshot_id="downstream-snapshot",
                source="pan115",
                source_root=str(source_root / "示例动画"),
                real_path=str(source_file),
                relative_path=source_file.name,
                name=source_file.name,
                ext=".mkv",
            )],
        )
        save_raw_snapshot(downstream_snapshot, update_latest=False)
        target_dir = mirror_root / "115" / "示例动画" / "Season 1"
        target_dir.mkdir(parents=True)
        strm_path = target_dir / "示例动画.S01E01.strm"
        strm_path.write_text(str(source_file), encoding="utf-8")
        poster_path = target_dir.parent / "poster.jpg"
        poster_path.write_bytes(b"poster")
        downstream_plan = ImportPlan(
            plan_id="downstream-plan",
            source="pan115",
            source_snapshot_id=downstream_snapshot.snapshot_id,
            import_family="anime",
            import_scope="seasonal",
            status="executed",
            items=[ImportPlanItem(
                id="downstream-item",
                plan_id="downstream-plan",
                source="pan115",
                resource_type="video",
                action="generate_strm",
                real_path=str(source_file),
                work_id="raw-work",
                canonical_work_id="library-work",
                target_dir=str(target_dir),
                target_strm_path=str(strm_path),
            )],
        )
        save_import_plan(downstream_plan, update_latest=False)
        save_scrape_map(ScrapeMap(items=[ScrapeMapItem(
            scrape_target_id="target-owned",
            work_id="library-work",
            source="pan115",
            import_plan_id=downstream_plan.plan_id,
            poster_path=str(poster_path),
        )]))
        review_item = ReviewQueueItem(
            scrape_target_id="target-owned",
            source="pan115",
            import_plan_id=downstream_plan.plan_id,
            status="pending",
            added_at="2026-07-22T00:00:00+08:00",
        )
        save_review_queue(ReviewQueue(items=[review_item]))
        _save_review_item_to_db(review_item)
        save_library_index(LibraryIndex(works=[WorkIndex(
            work_id="library-work",
            title="示例动画",
            source="pan115",
            import_scope="seasonal",
            dir_path=str(target_dir),
            episodes=[EpisodeIndex(
                episode_id="episode-owned",
                work_id="library-work",
                source="pan115",
                strm_path=str(strm_path),
            )],
        )]))
        binding = upsert_tracking_binding(TrackingBinding(
            work_id="library-work",
            display_title="示例动画",
            logical_source="pan115",
            root_path=str(source_root / "示例动画"),
            import_family="anime",
            last_snapshot_id=downstream_snapshot.snapshot_id,
            baseline_plan_id=downstream_plan.plan_id,
        ))
        record_tracking_scan_run(binding, {"status": "succeeded"})
        save_history(build_history_item(
            work_id="library-work",
            work_title="示例动画",
            episode_id="episode-owned",
            episode_title="第 1 集",
            source="pan115",
            media_type="tv",
            group_type="season",
            season_number=1,
            episode_number=1,
            strm_path=str(strm_path),
        ))
        save_progress("library-work", "episode-owned", 60, 120)
        set_watch_status("library-work", "watching", favorite=True)
        set_title_override("library-work", "人工标题")

        preview_response = client.post(
            f"/api/media-presets/{preset_id}/delete/preview",
            json={},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["blocked"] is False
        assert preview["preserved_generated_media"] is True
        assert preview["preserved_library_data"] is True

        confirm_response = client.post(
            f"/api/media-presets/{preset_id}/delete/confirm",
            json={"preview_id": preview["preview_id"]},
        )
        assert confirm_response.status_code == 200, confirm_response.text

    assert source_file.exists()
    assert strm_path.exists()
    assert poster_path.exists()
    assert (data_dir / "raw_snapshots" / "downstream-snapshot.json").exists()
    assert (data_dir / "import_plans" / "downstream-plan.json").exists()
    assert not (data_dir / created["version"]["archive_path"]).exists()
    assert load_history()[0].work_id == "library-work"
    assert load_progress()[0].work_id == "library-work"
    assert "library-work" in load_watch_statuses()
    assert load_library_index().works[0].work_id == "library-work"
    assert load_scrape_map().items[0].scrape_target_id == "target-owned"

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM tracking_bindings WHERE work_id = ?", ("library-work",)).fetchone()[0]
    assert count == 1
    count = conn.execute("SELECT COUNT(*) FROM tracking_scan_runs WHERE work_id = ?", ("library-work",)).fetchone()[0]
    assert count == 1
    assert conn.execute("SELECT COUNT(*) FROM playback_history WHERE work_id = ?", ("library-work",)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM work_overrides WHERE work_id = ?", ("library-work",)).fetchone()[0] == 1
    count = conn.execute(
        "SELECT COUNT(*) FROM scrape_review_queue WHERE scrape_target_id = ?",
        ("target-owned",),
    ).fetchone()[0]
    assert count == 1
    close_connection()


def test_media_preset_delete_preserves_files_referenced_by_another_card(tmp_path, monkeypatch):
    """两个卡片意外映射到同一镜像路径时，删除一张不能破坏另一张。"""
    data_dir = tmp_path / "data"
    mirror_root = data_dir / "mirror"
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror_root))

    from app.import_plan.store import load_import_plan, save_import_plan

    def create_at(client: TestClient, source_root: str):
        response = client.post(
            "/api/media-presets",
            data={
                "source": "pan115",
                "source_root": source_root,
                "import_family": "anime",
                "import_scope": "",
            },
            files={"tree_file": ("目录.txt", _tree(1), "text/plain")},
        )
        assert response.status_code == 200, response.text
        return response.json()

    with TestClient(app) as client:
        first = create_at(client, "H:/115open/媒体库一")
        second = create_at(client, "H:/115open/媒体库二")
        shared_dir = mirror_root / "115" / "示例动画" / "Season 1"
        shared_dir.mkdir(parents=True)
        shared_strm = shared_dir / "示例动画.S01E01.strm"
        shared_strm.write_text("H:/115open/媒体库二/示例动画.mkv", encoding="utf-8")

        for created, real_root in ((first, "H:/115open/媒体库一"), (second, "H:/115open/媒体库二")):
            plan = load_import_plan(plan_id=created["preset"]["current_plan_id"])
            item = next(entry for entry in plan.items if entry.resource_type == "video")
            item.real_path = f"{real_root}/示例动画.mkv"
            item.target_dir = str(shared_dir)
            item.target_strm_path = str(shared_strm)
            save_import_plan(plan, update_latest=False)

        preview_response = client.post(
            f"/api/media-presets/{first['preset']['preset_id']}/delete/preview",
            json={},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["preserved_generated_media"] is True
        assert preview["preserved_library_data"] is True

        confirm_response = client.post(
            f"/api/media-presets/{first['preset']['preset_id']}/delete/confirm",
            json={"preview_id": preview["preview_id"]},
        )
        assert confirm_response.status_code == 200, confirm_response.text
        assert client.get(f"/api/media-presets/{first['preset']['preset_id']}").status_code == 404
        assert client.get(f"/api/media-presets/{second['preset']['preset_id']}").status_code == 200

    assert shared_strm.exists()


def test_media_preset_delete_does_not_depend_on_version_plan(tmp_path, monkeypatch):
    """卡片和目录树归档可以独立删除，不依赖后端 ImportPlan。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = _create(client).json()
        preset = created["preset"]
        (tmp_path / "import_plans" / f"{preset['current_plan_id']}.json").unlink()

        preview_response = client.post(
            f"/api/media-presets/{preset['preset_id']}/delete/preview",
            json={},
        )
        preview = preview_response.json()
        confirm_response = client.post(
            f"/api/media-presets/{preset['preset_id']}/delete/confirm",
            json={"preview_id": preview["preview_id"]},
        )

    assert preview_response.status_code == 200, preview_response.text
    assert preview["blocked"] is False
    assert confirm_response.status_code == 200, confirm_response.text


def test_update_uses_selected_preset_baseline_not_latest_same_source(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        first = _create(client, (1,), filename="动画一.txt").json()
        second_response = client.post(
            "/api/media-presets",
            data={
                "source": "pan115",
                "source_root": "H:/115open/第二库",
                "import_family": "anime",
                "import_scope": "",
            },
            files={"tree_file": ("动画二.txt", _tree(7, 8), "text/plain")},
        )
        assert second_response.status_code == 200, second_response.text
        second = second_response.json()
        assert second["preset"]["name"] == "动画二"

        response = client.post(
            f"/api/media-presets/{first['preset']['preset_id']}/updates",
            files={"tree_file": ("动画一新版.txt", _tree(1, 2), "text/plain")},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["diff"]["added_count"] == 1
        assert body["diff"]["missing_count"] == 0
        assert body["preset"]["current_snapshot_id"] == body["version"]["snapshot_id"]
        assert body["preset"]["current_snapshot_id"] != second["preset"]["current_snapshot_id"]
        assert body["preview"]["summary"]["video_count"] == 2


def test_unsafe_update_does_not_replace_current_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = _create(client, tuple(range(1, 11))).json()
        old_snapshot = created["preset"]["current_snapshot_id"]
        response = client.post(
            f"/api/media-presets/{created['preset']['preset_id']}/updates",
            files={"tree_file": ("危险更新.txt", _tree(1), "text/plain")},
        )
        assert response.status_code == 409
        current = client.get(f"/api/media-presets/{created['preset']['preset_id']}").json()["preset"]
        assert current["current_snapshot_id"] == old_snapshot


def test_rejects_unsupported_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets",
            data={"source": "pan115", "source_root": "H:/115open", "import_family": "anime"},
            files={"tree_file": ("目录树.exe", b"bad", "application/octet-stream")},
        )
        assert response.status_code == 400
        assert not list(Path(tmp_path).rglob("*.exe"))


def test_missing_preset_keeps_actionable_api_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/missing-preset/updates",
            files={"tree_file": ("新版目录树.txt", _tree(1, 2), "text/plain")},
        )
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["detail"] == "媒体库预设不存在"


def test_preset_list_exposes_scrape_queue_task_for_current_plan(tmp_path, monkeypatch):
    """卡片必须能按 plan_id 恢复自己的排队位置和进度。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    from app.tasks.registry import get_task_manager, reset_task_manager

    reset_task_manager()
    release = threading.Event()
    try:
        with TestClient(app) as client:
            created = _create(client).json()
            plan_id = created["preset"]["current_plan_id"]
            manager = get_task_manager()
            task = manager.submit_queued(
                "scrape_auto",
                "pan115",
                lambda: (release.wait(timeout=2) or {"ok": True}),
                queue_name="scrape",
                initial_result={"plan_id": plan_id},
                message="自动刮削",
            )

            preset = client.get("/api/media-presets").json()["presets"][0]
            assert preset["scrape_task"]["task_id"] == task.task_id
            assert preset["scrape_task"]["result"]["plan_id"] == plan_id
            assert preset["scrape_task"]["status"] in {"pending", "running"}
    finally:
        release.set()
        reset_task_manager()


# ============================================================
# 挂载 TXT 导入更新合同（CloudTreeImport-02-Fix）
# ============================================================

def _write_tree(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.write_bytes(content)
    return path


def _pan115_tree_bytes(*episodes: int, scope: str = "动画") -> bytes:
    lines = [f"|——{scope}", "| |-[测试字幕组] 示例动画"]
    lines.extend(f"| | |-[测试字幕组] 示例动画 [{ep:02d}].mkv" for ep in episodes)
    return ("\n".join(lines) + "\n").encode()


def _baidu_tree_bytes(*episodes: int, scope: str = "动画") -> bytes:
    lines = [f"├── {scope}", "│   ├── 示例动画"]
    lines.extend(f"│   │   ├── 示例动画 [{ep:02d}].mkv" for ep in episodes)
    return ("\n".join(lines) + "\n").encode()


def _write_115_media(root: Path, episode: int) -> Path:
    media = root / "示例动画" / f"示例动画 [{episode:02d}].mkv"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(f"episode-{episode}".encode())
    return media


def _create_from_path(client: TestClient, tree_path: Path, *, expected: str, family: str = "anime"):
    return client.post(
        "/api/media-presets/import-local-tree",
        json={
            "tree_path": str(tree_path),
            "expected_source": expected,
            "import_family": family,
            "import_scope": "",
        },
    )


# ============================================================
# 挂载盘 WinError 1005 兼容（目录树 TXT 导入）
# 与 OpenList 链路一致：只验证可访问性，不强制 resolve(strict=True)
# ============================================================

def test_import_local_tree_path_on_mount_without_realpath_resolution(tmp_path, monkeypatch):
    """115 挂载盘不支持真实路径解析（WinError 1005）时，导入仍成功。

    ``resolve(strict=True)`` 在挂载盘上会抛 WinError 1005，这里模拟该环境；
    archive_local_tree 改用 absolute() 后不应再触发 resolve。断言：
    归档副本内容等于原 TXT；source_root 是 TXT 父目录（无额外推导）；
    source_tree_path 是用户传入的原始路径。
    """
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "115网盘" / "动画"
    _write_115_media(media_root, 1)
    tree_path = _write_tree(media_root, "根目录20260810194521_目录树.txt", _pan115_tree_bytes(1))
    original = tree_path.read_bytes()
    original_resolve = Path.resolve

    def reject_mount_realpath(path: Path, strict: bool = False):
        if path == tree_path:
            raise OSError(1005, "挂载服务不支持 realpath")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", reject_mount_realpath)

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/import-local-tree",
            json={
                "tree_path": str(tree_path),
                "expected_source": "pan115",
                "import_family": "anime",
                "import_scope": "",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preset"]["source_root"] == str(media_root)
    assert body["version"]["source_tree_path"] == str(tree_path)
    archive = Path(body["version"]["archive_path"])
    assert (tmp_path / "data" / archive).read_bytes() == original
    assert tree_path.read_bytes() == original


def test_import_local_tree_path_reports_missing_file_without_1005(tmp_path, monkeypatch):
    """路径不存在时给出明确提示，而不是 WinError 1005 包装。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    missing = tmp_path / "不存在的目录树.txt"

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(missing), "import_family": "anime"},
        )

    assert response.status_code == 400
    assert "目录树文件不存在或路径错误" in response.json()["detail"]


def test_import_local_tree_path_reports_mount_read_failure_hint(tmp_path, monkeypatch):
    """路径存在但挂载层拒绝读取（WinError 1005）时提示挂载状态，不当作文件损坏。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "115网盘"
    media_root.mkdir(parents=True)
    tree_path = _write_tree(media_root, "根目录_目录树.txt", _pan115_tree_bytes(1))
    original_read_bytes = Path.read_bytes

    def reject_mount_read(path: Path):
        if path == tree_path:
            raise OSError(1005, "挂载服务不支持读取")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_mount_read)

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_path), "import_family": "anime"},
        )

    assert response.status_code == 400
    assert "挂载盘不支持文件系统解析" in response.json()["detail"]


def test_import_local_tree_path_only_reads_selected_txt(tmp_path, monkeypatch):
    """导入只读取用户选中的那一个 TXT，不枚举挂载盘其他文件。"""
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "115网盘" / "动画"
    _write_115_media(media_root, 1)
    other_txt = _write_tree(media_root, "其他_目录树.txt", _pan115_tree_bytes(1))
    tree_path = _write_tree(media_root, "根目录20260810194521_目录树.txt", _pan115_tree_bytes(1))

    read_paths = []
    original_read_bytes = Path.read_bytes

    def recording_read_bytes(path: Path):
        read_paths.append(str(path))
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)

    with TestClient(app) as client:
        response = client.post(
            "/api/media-presets/import-local-tree",
            json={
                "tree_path": str(tree_path),
                "expected_source": "pan115",
                "import_family": "anime",
                "import_scope": "",
            },
        )

    assert response.status_code == 200, response.text
    assert str(tree_path) in read_paths
    assert str(other_txt) not in read_paths


def test_create_from_path_rejects_source_mismatch_without_residue(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "媒体"
    _write_115_media(media_root, 1)
    tree_path = _write_tree(media_root, "动画_目录树.txt", _pan115_tree_bytes(1))

    with TestClient(app) as client:
        response = _create_from_path(client, tree_path, expected="baidu")
        assert response.status_code == 400
        assert "目录树来源不匹配" in response.json()["detail"]
        # 不允许创建残留预设/快照/归档
        assert client.get("/api/media-presets").json()["presets"] == []
        leftovers_dir = tmp_path / "data" / "media_presets"
        leftover = list(leftovers_dir.rglob("*.txt")) if leftovers_dir.exists() else []
        assert not leftover


def test_update_from_path_rejects_cross_source_without_changing_preset(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "媒体"
    _write_115_media(media_root, 1)
    tree_path = _write_tree(media_root, "动画_目录树.txt", _pan115_tree_bytes(1))

    with TestClient(app) as client:
        created = _create_from_path(client, tree_path, expected="pan115")
        assert created.status_code == 200, created.text
        preset_id = created.json()["preset"]["preset_id"]
        before = client.get(f"/api/media-presets/{preset_id}").json()["preset"]
        before_versions = before["version_count"]
        before_root = before["source_root"]
        before_current = before["current_version_id"]

        # 百度格式 TXT 更新到 115 预设：来源不匹配，应在解析前以 400 拒绝。
        baidu_tree = _write_tree(media_root, "百度_目录树.txt", _baidu_tree_bytes(1))
        response = client.post(
            f"/api/media-presets/{preset_id}/updates-from-path",
            json={"tree_path": str(baidu_tree), "expected_source": "pan115"},
        )
        assert response.status_code == 400
        assert "目录树来源不匹配" in response.json()["detail"]

        after = client.get(f"/api/media-presets/{preset_id}").json()["preset"]
        assert after["version_count"] == before_versions
        assert after["source_root"] == before_root
        assert after["current_version_id"] == before_current


def test_update_from_path_same_sha_returns_unchanged_without_new_version(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "媒体"
    _write_115_media(media_root, 1)
    tree_path = _write_tree(media_root, "动画_目录树.txt", _pan115_tree_bytes(1))

    with TestClient(app) as client:
        created = _create_from_path(client, tree_path, expected="pan115")
        assert created.status_code == 200, created.text
        preset_id = created.json()["preset"]["preset_id"]
        before_version_count = client.get(f"/api/media-presets/{preset_id}").json()["preset"]["version_count"]

        # 相同的 TXT 内容再次更新：SHA 相同，应 unchanged 且不新增版本。
        same_tree = _write_tree(media_root, "动画_重复.txt", _pan115_tree_bytes(1))
        response = client.post(
            f"/api/media-presets/{preset_id}/updates-from-path",
            json={"tree_path": str(same_tree), "expected_source": "pan115"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get("unchanged") is True
        after = client.get(f"/api/media-presets/{preset_id}").json()["preset"]
        assert after["version_count"] == before_version_count


def test_update_from_path_success_activates_new_version_and_root(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    media_root = tmp_path / "媒体"
    _write_115_media(media_root, 1)
    first_tree = _write_tree(media_root, "动画_目录树.txt", _pan115_tree_bytes(1))

    with TestClient(app) as client:
        created = _create_from_path(client, first_tree, expected="pan115")
        preset_id = created.json()["preset"]["preset_id"]
        before_version_count = client.get(f"/api/media-presets/{preset_id}").json()["preset"]["version_count"]

        # 新版本目录树（追加一集）：更新成功后新 TXT 父目录成为 source_root。
        media_root2 = tmp_path / "媒体新"
        _write_115_media(media_root2, 1)
        _write_115_media(media_root2, 2)
        second_tree = _write_tree(media_root2, "动画_目录树.txt", _pan115_tree_bytes(1, 2))
        response = client.post(
            f"/api/media-presets/{preset_id}/updates-from-path",
            json={"tree_path": str(second_tree), "expected_source": "pan115"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        after = client.get(f"/api/media-presets/{preset_id}").json()["preset"]
        assert after["version_count"] == before_version_count + 1
        assert after["source_root"] == str(media_root2)
        assert body["version"]["source_tree_path"] == str(second_tree.resolve())
