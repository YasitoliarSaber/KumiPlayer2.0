# -*- coding: utf-8 -*-
from pathlib import Path

from app.raw.models import RawFile, RawSnapshot


def _snapshot(root: Path, relative_path: str) -> RawSnapshot:
    parts = list(Path(relative_path).parts)
    return RawSnapshot(
        source="baidu",
        source_root=str(root),
        files=[
            RawFile(
                source="baidu",
                source_root=str(root),
                source_path_parts=parts,
                relative_path=relative_path,
                real_path=str(root.joinpath(*parts)),
                resource_hint="video",
            )
        ],
        file_count=1,
        video_count=1,
    )


def test_baidu_root_validation_never_searches_nested_mount_directories(tmp_path):
    """目录树根配置错误时不得递归搜索挂载盘，应等待用户选择精确目录。"""
    from app.sources.path_validation import resolve_baidu_snapshot_root

    mount_root = tmp_path / "百度网盘"
    correct_root = mount_root / "媒体库" / "分类" / "01动画"
    relative_path = str(Path("已完结") / "测试作品" / "Season 1" / "测试作品.S01E01.mkv")
    media_file = correct_root / relative_path
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")

    snapshot = _snapshot(mount_root, relative_path)
    tree_file = tmp_path / "01动画_文件目录_20260718010101.txt"
    tree_file.write_text("tree", encoding="utf-8")

    result = resolve_baidu_snapshot_root(snapshot, str(tree_file), str(mount_root))

    assert result.ok is False
    assert result.status == "mismatch"
    assert result.resolved_root == str(mount_root)
    assert snapshot.source_root == str(mount_root)
    assert snapshot.files[0].real_path == str(mount_root / relative_path)


def test_baidu_root_validation_accepts_user_selected_exact_directory(tmp_path):
    """用户选择的精确内容目录应直接拼接相对路径并通过抽样验证。"""
    from app.sources.path_validation import resolve_baidu_snapshot_root

    selected_root = tmp_path / "百度网盘" / "01动画" / "新番"
    relative_path = str(Path("测试作品") / "Season 1" / "测试作品.S01E01.mkv")
    media_file = selected_root / relative_path
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    snapshot = _snapshot(selected_root, relative_path)

    result = resolve_baidu_snapshot_root(
        snapshot,
        str(tmp_path / "新番_文件目录.txt"),
        str(selected_root),
    )

    assert result.ok is True
    assert result.resolved_root == str(selected_root)
    assert result.example_path == str(media_file)


def test_baidu_root_does_not_claim_verified_without_matching_media(tmp_path):
    """只有目录名匹配但没有任何视频样本时，不得宣称路径已经验证。"""
    from app.sources.path_validation import resolve_baidu_snapshot_root

    mount_root = tmp_path / "百度网盘"
    mount_root.mkdir(parents=True)
    relative_path = str(Path("已完结") / "不存在" / "S01E01.mkv")
    snapshot = _snapshot(mount_root, relative_path)
    tree_file = tmp_path / "01动画_文件目录.txt"
    tree_file.write_text("tree", encoding="utf-8")

    result = resolve_baidu_snapshot_root(snapshot, str(tree_file), str(mount_root))

    assert result.ok is False
    assert result.status == "mismatch"
    assert result.checked_count == 1
    assert result.existing_count == 0


def test_baidu_root_does_not_claim_verified_when_only_part_of_samples_exist(tmp_path):
    """抽样仅部分命中时应显示路径不完整，不能允许用户继续生成镜像。"""
    from app.sources.path_validation import resolve_baidu_snapshot_root

    mount_root = tmp_path / "百度网盘"
    seasonal_root = mount_root / "01动画" / "新番"
    existing_relative = str(Path("作品甲") / "Season 1" / "作品甲.S01E01.mkv")
    missing_relative = str(Path("作品乙") / "Season 1" / "作品乙.S01E01.mkv")
    existing_file = seasonal_root / existing_relative
    existing_file.parent.mkdir(parents=True)
    existing_file.write_bytes(b"video")
    snapshot = RawSnapshot(
        source="baidu",
        source_root=str(mount_root),
        files=[
            _snapshot(mount_root, existing_relative).files[0],
            _snapshot(mount_root, missing_relative).files[0],
        ],
        file_count=2,
        video_count=2,
    )

    result = resolve_baidu_snapshot_root(
        snapshot,
        str(tmp_path / "新番_文件目录.txt"),
        str(seasonal_root),
    )

    assert result.ok is False
    assert result.status == "mismatch"
    assert result.resolved_root == str(seasonal_root)
    assert result.checked_count == 2
    assert result.existing_count == 1


def test_plan_path_validation_requires_every_sample_to_exist(tmp_path):
    """代表性样本只命中一部分时仍应失败，避免半错路径进入镜像。"""
    from app.import_plan.models import ImportPlanItem
    from app.sources.path_validation import validate_plan_media_paths

    existing = tmp_path / "01动画" / "新番" / "作品甲" / "S01E01.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"video")
    missing = tmp_path / "01动画" / "新番" / "作品乙" / "S01E01.mkv"
    items = [
        ImportPlanItem(
            id="existing", resource_type="video", action="generate_strm",
            real_path=str(existing), availability="available",
        ),
        ImportPlanItem(
            id="missing", resource_type="video", action="generate_strm",
            real_path=str(missing), availability="available",
        ),
    ]

    ok, checked, found = validate_plan_media_paths(items)

    assert ok is False
    assert checked == 2
    assert found == 1
