# -*- coding: utf-8 -*-
"""本地扫描器测试"""

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_TEST_DIR = Path(__file__).parent.parent.parent / "data" / "_test_local"


def _setup():
    """创建测试目录结构"""
    if _TEST_DIR.exists():
        shutil.rmtree(_TEST_DIR)
    _TEST_DIR.mkdir(parents=True)

    (_TEST_DIR / "anime" / "AIR" / "Season 1").mkdir(parents=True)
    (_TEST_DIR / "anime" / "AIR" / "Season 1" / "S01E01.mkv").write_text("fake", encoding="utf-8")
    (_TEST_DIR / "anime" / "AIR" / "Season 1" / "S01E01.ass").write_text("fake", encoding="utf-8")
    (_TEST_DIR / "anime" / "AIR" / "poster.jpg").write_bytes(b"fake")
    (_TEST_DIR / "anime" / "AIR" / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")
    (_TEST_DIR / "anime" / "AIR" / "unknown.weird").write_text("fake", encoding="utf-8")

    # 隐藏目录
    (_TEST_DIR / ".hidden").mkdir()
    (_TEST_DIR / ".hidden" / "secret.mkv").write_text("fake", encoding="utf-8")

    # __pycache__
    (_TEST_DIR / "__pycache__").mkdir()
    (_TEST_DIR / "__pycache__" / "cache.pyc").write_text("fake", encoding="utf-8")


def _cleanup():
    if _TEST_DIR.exists():
        shutil.rmtree(_TEST_DIR)


def test_recursive_scan():
    """递归扫描本地目录"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        assert snapshot.source == "local"
        assert snapshot.file_count >= 3  # mkv, ass, jpg, nfo
    finally:
        _cleanup()


def test_relative_path():
    """relative_path 相对 root_path"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        paths = [f.relative_path for f in snapshot.files]
        assert any("anime/AIR/Season 1/S01E01.mkv" in p for p in paths)
    finally:
        _cleanup()


def test_real_path_absolute():
    """real_path 为绝对路径"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        for f in snapshot.files:
            assert Path(f.real_path).is_absolute()
    finally:
        _cleanup()


def test_records_size():
    """记录 size"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        mkv = [f for f in snapshot.files if f.ext == ".mkv"][0]
        assert mkv.size is not None
        assert mkv.size > 0
    finally:
        _cleanup()


def test_records_mtime():
    """记录 mtime"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        mkv = [f for f in snapshot.files if f.ext == ".mkv"][0]
        assert mkv.mtime is not None
        assert mkv.mtime > 0
    finally:
        _cleanup()


def test_skip_hidden_dirs():
    """跳过隐藏目录"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        names = [f.name for f in snapshot.files]
        assert "secret.mkv" not in names
    finally:
        _cleanup()


def test_skip_pycache():
    """跳过 __pycache__"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        names = [f.name for f in snapshot.files]
        assert "cache.pyc" not in names
    finally:
        _cleanup()


def test_resource_hint():
    """resource_hint 正确"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        hints = {f.name: f.resource_hint for f in snapshot.files}
        assert hints.get("S01E01.mkv") == "video"
        assert hints.get("S01E01.ass") == "subtitle"
        assert hints.get("poster.jpg") == "image"
        assert hints.get("tvshow.nfo") == "nfo"
    finally:
        _cleanup()


def test_unknown_file_kept_as_other():
    """未知扩展名文件仍生成 RawFile，resource_hint=other"""
    _setup()
    try:
        from app.sources.local import LocalScanner
        scanner = LocalScanner()
        snapshot = scanner.parse(str(_TEST_DIR))
        hints = {f.name: f.resource_hint for f in snapshot.files}
        assert hints.get("unknown.weird") == "other"
    finally:
        _cleanup()


def test_source_id_and_namespace():
    """source_id = local, mirror_namespace = local"""
    from app.sources.local import LocalScanner
    scanner = LocalScanner()
    assert scanner.source_id == "local"
    assert scanner.mirror_namespace == "local"


def test_metadata_only_scan_never_opens_media_content(tmp_path, monkeypatch):
    """挂载盘扫描只能读取目录元数据，不能打开视频计算摘要。"""
    video = tmp_path / "作品A.S01E01.mkv"
    video.write_bytes(b"remote-video-placeholder")

    def reject_content_read(self, *args, **kwargs):
        raise AssertionError(f"扫描不应打开文件内容: {self}")

    monkeypatch.setattr(Path, "open", reject_content_read)
    from app.sources.local import LocalScanner

    snapshot = LocalScanner().scan(str(tmp_path), include_root=True, metadata_only=True)

    assert snapshot.video_count == 1
    assert snapshot.files[0].size == len(b"remote-video-placeholder")
    assert snapshot.files[0].mtime > 0
    assert snapshot.files[0].content_fingerprint == ""


def test_local_scan_can_use_lightweight_content_fingerprint(tmp_path):
    video = tmp_path / "作品A.S01E01.mkv"
    video.write_bytes(b"local-video")
    from app.sources.local import LocalScanner

    snapshot = LocalScanner().scan(str(tmp_path))

    assert snapshot.files[0].content_fingerprint


def test_metadata_scan_stops_during_directory_walk(tmp_path):
    """取消信号必须进入目录遍历内部，不能等整部作品扫完才生效。"""
    for index in range(8):
        (tmp_path / f"S01E{index + 1:02d}.mkv").write_bytes(b"video")
    checks = 0

    def should_cancel():
        nonlocal checks
        checks += 1
        return checks >= 4

    from app.sources.local import LocalScanner

    with pytest.raises(RuntimeError, match="任务已停止"):
        LocalScanner().scan(
            str(tmp_path), metadata_only=True, should_cancel=should_cancel,
            directory_delay=0, retry_delays=(),
        )


def test_metadata_scan_stops_at_entry_safety_limit(tmp_path):
    """错误选中超大目录时应安全停止，避免无限枚举挂载盘。"""
    (tmp_path / "S01E01.mkv").write_bytes(b"1")
    (tmp_path / "S01E02.mkv").write_bytes(b"2")
    from app.sources.local import LocalScanner

    with pytest.raises(ValueError, match="安全上限"):
        LocalScanner().scan(
            str(tmp_path), metadata_only=True, max_entries=1,
            directory_delay=0, retry_delays=(),
        )


def test_metadata_scan_retries_transient_directory_error(tmp_path, monkeypatch):
    """挂载盘目录枚举的瞬时失败应退避重试，而不是立即中断更新。"""
    (tmp_path / "S01E01.mkv").write_bytes(b"video")
    original_scandir = os.scandir
    calls = 0

    def flaky_scandir(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary provider error")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)
    from app.sources.local import LocalScanner

    snapshot = LocalScanner().scan(
        str(tmp_path), metadata_only=True, directory_delay=0, retry_delays=(0,),
    )

    assert snapshot.video_count == 1
    assert calls >= 2


def test_scanner_does_not_follow_directory_links(tmp_path):
    """目录链接不能形成循环或越过用户选择的作品目录。"""
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "outside.mkv").write_bytes(b"video")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建目录链接")

    from app.sources.local import LocalScanner
    snapshot = LocalScanner().scan(str(tmp_path), metadata_only=True, directory_delay=0)

    assert "outside.mkv" not in {item.name for item in snapshot.files}


if __name__ == "__main__":
    tests = [
        test_recursive_scan,
        test_relative_path,
        test_real_path_absolute,
        test_records_size,
        test_records_mtime,
        test_skip_hidden_dirs,
        test_skip_pycache,
        test_resource_hint,
        test_unknown_file_kept_as_other,
        test_source_id_and_namespace,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
